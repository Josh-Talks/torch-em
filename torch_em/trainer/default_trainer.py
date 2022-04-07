import contextlib
import os
import time
import warnings
from importlib import import_module
from typing import Any, Callable, Dict, Optional, Union

import numpy as np
import torch
import torch.cuda.amp as amp
from tqdm import tqdm

from .tensorboard_logger import TensorboardLogger
from .wandb_logger import WandbLogger
from ..util import get_constructor_arguments


class DefaultTrainer:
    """ Trainer class for 2d/3d training on a single GPU.
    """
    def __init__(
        self,
        name: Optional[str],
        train_loader=None,
        val_loader=None,
        model=None,
        loss=None,
        optimizer=None,
        metric=None,
        device=None,
        lr_scheduler=None,
        log_image_interval=100,
        mixed_precision=True,
        early_stopping=None,
        logger=TensorboardLogger,
        logger_kwargs: Optional[Dict[str, Any]] = None,
    ):
        if name is None and not issubclass(logger, WandbLogger):
            raise TypeError("Name cannot be None if not using the WandbLogger")

        if not all(hasattr(loader, "shuffle") for loader in [train_loader, val_loader]):
            raise ValueError(f"{self.__class__} requires any dataloader to have 'shuffle' attribute.")

        self._generate_name = name is None
        self.name = name
        self.train_loader = train_loader
        self.val_loader = val_loader
        self.model = model
        self.loss = loss
        self.optimizer = optimizer
        self.metric = metric
        self.device = device
        self.lr_scheduler = lr_scheduler
        self.log_image_interval = log_image_interval

        self._iteration = 0
        self._epoch = 0
        self._best_epoch = 0

        self.mixed_precision = mixed_precision
        self.early_stopping = early_stopping

        self.scaler = amp.GradScaler() if self.mixed_precision else None

        self.logger_class = logger
        self.logger_kwargs = logger_kwargs
        self.log_image_interval = log_image_interval

    @property  # because the logger may generate and set trainer.name on logger.__init__
    def checkpoint_folder(self):
        assert self.name is not None
        return os.path.join("./checkpoints", self.name)

    @property
    def iteration(self):
        return self._iteration

    @property
    def epoch(self):
        return self._epoch

    class Deserializer:
        """
        Determines how to deserialize the trainer kwargs from serialized 'init_data'

        Examples:
            To extend the initialization process you can inherite from this Deserializer in an inherited Trainer class.
            Note that `DefaultTrainer.Deserializer.load_generic()` covers most cases already.

            This example adds `the_answer` kwarg, which requires 'calculations' upon initialization
            >>> class MyTrainer(DefaultTrainer):
            >>>     def __init__(self, *args, the_answer: int, **kwargs):
            >>>         super().__init__(*args, **kwargs)
            >>>         self.the_answer = the_answer
            >>>
            >>>     class Deserializer(DefaultTrainer.Deserializer):
            >>>         def __call__(self, name, *args, **kwargs):
            >>>             if name == "the_answer":
            >>>                 return self.load_the_answer()
            >>>             else:
            >>>                 return super().__call__(name, *args, **kwargs)
            >>>
            >>>         def load_the_answer(self):
            >>>             if self.device.type == "cpu":
            >>>                 return ...  # complex 'calculation' to arrive at the answer
            >>>             else:
            >>>                 return 42
        """

        def __init__(self, init_data: dict, save_path: str, device: Union[str, torch.device]):
            self.init_data = init_data
            self.save_path = save_path
            self.device = torch.device(self.init_data["device"]) if device is None else torch.device(device)

        def __call__(
            self,
            name: str,
            *dynamic_args,
            optional=False,
            only_class=False,
            dynamic_kwargs: Optional[Dict[str, Any]] = None,
        ):
            if name.endswith("_loader"):
                return self.load_loader(name)
            if name == "device":
                return self.device
            else:
                return self.load_generic(
                    name, *dynamic_args, optional=optional, only_class=only_class, dynamic_kwargs=dynamic_kwargs
                )

        def load_generic(
            self, name: str, *dynamic_args, optional: bool, only_class: bool, dynamic_kwargs: Optional[Dict[str, Any]]
        ):
            if name in self.init_data:
                return self.init_data[name]

            this_cls = self.init_data.get(f"{name}_class", None)
            if this_cls is None and optional:
                return None
            elif this_cls is None and not optional:
                raise RuntimeError(f"Could not find init data for {name} in {self.save_path}")

            cls_p, cls_m = this_cls.rsplit(".", 1)
            this_cls = getattr(import_module(cls_p), cls_m)
            if only_class:
                return this_cls
            else:
                return this_cls(*dynamic_args, **self.init_data.get(f"{name}_kwargs", {}), **(dynamic_kwargs or {}))

        def load_loader(self, name):
            ds = self.init_data[f"{name.replace('_loader', '')}_dataset"]
            loader_kwargs = self.init_data[f"{name}_kwargs"]
            loader = torch.utils.data.DataLoader(ds, **loader_kwargs)
            # monkey patch shuffle attribute to the loader
            loader.shuffle = loader_kwargs.get("shuffle", False)
            return loader

    @staticmethod
    def _get_save_dict(save_path, device):
        if not os.path.exists(save_path):
            raise ValueError(f"Cannot find checkpoint {save_path}")

        return torch.load(save_path, map_location=device)

    @staticmethod
    def _get_trainer_kwargs(load: Deserializer):
        model = load("model")
        optimizer = load("optimizer", model.parameters())

        return dict(
            name=os.path.split(os.path.dirname(load.save_path))[1],
            train_loader=load("train_loader"),
            val_loader=load("val_loader"),
            model=model,
            loss=load("loss"),
            optimizer=optimizer,
            metric=load("metric"),
            device=load("device"),
            lr_scheduler=load("lr_scheduler", optimizer, optional=True),
            log_image_interval=load("log_image_interval"),
            mixed_precision=load("mixed_precision"),
            early_stopping=load("early_stopping"),
            logger=load("logger", only_class=True, optional=True),
            logger_kwargs=load("logger_kwargs", optional=True),
        )

    @classmethod
    def from_checkpoint(cls, checkpoint_folder, name="best", device=None):
        save_path = os.path.join(checkpoint_folder, f"{name}.pt")
        save_dict = cls._get_save_dict(save_path, device)
        load = cls.Deserializer(save_dict["init"], save_path, device)
        trainer_kwargs = cls._get_trainer_kwargs(load)
        trainer = cls(**trainer_kwargs)
        trainer._initialize(0, save_dict)
        return trainer

    def _build_init(self):

        def _full_class_path(obj):
            return f"{obj.__class__.__module__}.{obj.__class__.__name__}"

        def _full_class_path_of_class(obj):
            return f"{obj.__module__}.{obj.__name__}"

        def _update_loader(init_data, loader, name):
            init_data.update({
                f"{name}_dataset": loader.dataset,
                f"{name}_loader_kwargs": get_constructor_arguments(loader)
            })
            return init_data

        init_data = {
            "model_class": _full_class_path(self.model),
            "model_kwargs": get_constructor_arguments(self.model),
            "loss_class": _full_class_path(self.loss),
            "loss_kwargs": get_constructor_arguments(self.loss),
            "optimizer_class": _full_class_path(self.optimizer),
            "optimizer_kwargs": get_constructor_arguments(self.optimizer),
            "metric_class": _full_class_path(self.metric),
            "metric_kwargs": get_constructor_arguments(self.metric),
            "device": self.device.type,
            "log_image_interval": self.log_image_interval,
            "mixed_precision": self.mixed_precision,
            "early_stopping": self.early_stopping,
            "logger_class": None if self.logger_class is None else _full_class_path_of_class(self.logger_class),
            "logger_kwargs": self.logger_kwargs,
        }
        init_data = _update_loader(init_data, self.train_loader, "train")
        init_data = _update_loader(init_data, self.val_loader, "val")
        if self.lr_scheduler is not None:
            init_data["lr_scheduler_class"] = _full_class_path(self.lr_scheduler)
            init_data["lr_scheduler_kwargs"] = get_constructor_arguments(self.lr_scheduler)
        return init_data

    def _initialize(self, iterations, load_from_checkpoint):
        assert self.train_loader is not None
        assert self.val_loader is not None
        assert self.model is not None
        assert self.loss is not None
        assert self.optimizer is not None
        assert self.metric is not None
        assert self.device is not None

        if load_from_checkpoint is not None:
            self.load_checkpoint(load_from_checkpoint)

        self.max_iteration = self._iteration + iterations
        epochs = int(np.ceil(float(iterations) / len(self.train_loader)))
        self.max_epoch = self._epoch + epochs

        self.model.to(self.device)
        self.loss.to(self.device)

        # this saves all the information that is necessary
        # to fully load the trainer from the checkpoint
        self.init_data = self._build_init()

        if self.logger_class is None:
            self.logger = None
        else:
            # may set self.name if self.name is None
            self.logger = self.logger_class(self, **(self.logger_kwargs or {}))

        os.makedirs(self.checkpoint_folder, exist_ok=True)

        best_metric = np.inf
        return best_metric

    def save_checkpoint(self, name, best_metric, **extra_save_dict):
        save_path = os.path.join(self.checkpoint_folder, f"{name}.pt")
        save_dict = {
            "iteration": self._iteration,
            "epoch": self._epoch,
            "best_epoch": self._best_epoch,
            "best_metric": best_metric,
            "model_state": self.model.state_dict(),
            "optimizer_state": self.optimizer.state_dict(),
            "init": self.init_data
        }
        save_dict.update(**extra_save_dict)
        if self.scaler is not None:
            save_dict.update({"scaler_state": self.scaler.state_dict()})
        if self.lr_scheduler is not None:
            save_dict.update({"scheduler_state": self.lr_scheduler.state_dict()})
        torch.save(save_dict, save_path)

    def load_checkpoint(self, checkpoint="best"):
        if isinstance(checkpoint, str):
            save_path = os.path.join(self.checkpoint_folder, f"{checkpoint}.pt")
            if not os.path.exists(save_path):
                warnings.warn(f"Cannot load checkpoint. {save_path} does not exist.")
                return
            save_dict = torch.load(save_path)
        elif isinstance(checkpoint, dict):
            save_dict = checkpoint
        else:
            raise RuntimeError

        self._iteration = save_dict["iteration"]
        self._epoch = save_dict["epoch"]
        self._best_epoch = save_dict["best_epoch"]
        self.best_metric = save_dict["best_metric"]

        self.model.load_state_dict(save_dict["model_state"])
        # we need to send the network to the device before loading the optimizer state!
        self.model.to(self.device)

        self.optimizer.load_state_dict(save_dict["optimizer_state"])
        if self.scaler is not None:
            self.scaler.load_state_dict(save_dict["scaler_state"])
        if self.lr_scheduler is not None:
            self.lr_scheduler.load_state_dict(save_dict["scheduler_state"])

    def fit(self, iterations, load_from_checkpoint=None):
        best_metric = self._initialize(iterations, load_from_checkpoint)
        print("Start fitting for", self.max_iteration - self._iteration,
              "iterations / ", self.max_epoch - self._epoch, "epochs")

        if self.mixed_precision:
            train_epoch = self._train_epoch_mixed
            validate = self._validate_mixed
            print("Training with mixed precision")
        else:
            train_epoch = self._train_epoch
            validate = self._validate
            print("Training with single precision")

        # TODO pass the progress to training and update after each iteration
        progress = tqdm(total=iterations, desc=f"Epoch {self._epoch}", leave=True)
        msg = "Epoch %i: average [s/it]: %f, current metric: %f, best metric: %f"

        train_epochs = self.max_epoch - self._epoch
        for _ in range(train_epochs):
            t_per_iter = train_epoch(progress)
            current_metric = validate()

            if self.lr_scheduler is not None:
                self.lr_scheduler.step(current_metric)

            if current_metric < best_metric:
                best_metric = current_metric
                self._best_epoch = self._epoch
                self.save_checkpoint("best", best_metric)

            # TODO for tiny epochs we don"t want to save every time
            self.save_checkpoint("latest", best_metric)
            if self.early_stopping is not None:
                epochs_since_best = self._epoch - self._best_epoch
                if epochs_since_best > self.early_stopping:
                    print("Stopping training because there has been no improvement for", self.early_stopping, "epochs")
                    break

            self._epoch += 1
            progress.set_description(msg % (self._epoch, t_per_iter, current_metric, best_metric),
                                     refresh=True)

        print(f"Finished training after {self._epoch} epochs / {self._iteration} iterations.")
        print(f"The best epoch is number {self._best_epoch}.")

        if self._generate_name:
            self.name = None

        # TODO save the model to wandb if we have the wandb logger
        if isinstance(self.logger, WandbLogger):
            self.logger.get_wandb().finish()

    def _backprop(self, loss):
        loss.backward()
        self.optimizer.step()

    def _backprop_mixed(self, loss):
        self.scaler.scale(loss).backward()
        self.scaler.step(self.optimizer)
        self.scaler.update()

    def _train_epoch(self, progress):
        return self._train_epoch_impl(progress, contextlib.nullcontext, self._backprop)

    def _train_epoch_mixed(self, progress):
        return self._train_epoch_impl(progress, amp.autocast, self._backprop_mixed)

    def _forward_and_loss(self, x, y):
        pred = self.model(x)
        if self._iteration % self.log_image_interval == 0:
            pred.retain_grad()

        loss = self.loss(pred, y)
        return pred, loss

    def _train_epoch_impl(self, progress, forward_context, backprop: Callable[[torch.Tensor], None]):
        self.model.train()

        n_iter = 0
        t_per_iter = time.time()
        for x, y in self.train_loader:
            x, y = x.to(self.device), y.to(self.device)

            self.optimizer.zero_grad()

            with forward_context():
                pred, loss = self._forward_and_loss(x, y)

            backprop(loss)

            lr = [pm["lr"] for pm in self.optimizer.param_groups][0]
            if self.logger is not None:
                self.logger.log_train(self._iteration, loss, lr, x, y, pred, log_gradients=True)

            self._iteration += 1
            n_iter += 1
            if self._iteration >= self.max_iteration:
                break
            progress.update(1)

        t_per_iter = (time.time() - t_per_iter) / n_iter
        return t_per_iter

    def _validate(self):
        return self._validate_impl(contextlib.nullcontext)

    def _validate_mixed(self):
        return self._validate_impl(amp.autocast)

    def _validate_impl(self, forward_context):
        self.model.eval()

        metric_val = 0.0
        loss_val = 0.0

        with torch.no_grad():
            for x, y in self.val_loader:
                x, y = x.to(self.device), y.to(self.device)
                with forward_context():
                    pred, loss = self._forward_and_loss(x, y)
                    metric = self.metric(pred, y)

                loss_val += loss.item()
                metric_val += metric.item()

        metric_val /= len(self.val_loader)
        loss_val /= len(self.val_loader)
        if self.logger is not None:
            self.logger.log_validation(self._iteration, metric, loss, x, y, pred)
        return metric_val

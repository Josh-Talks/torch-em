import numpy as np
import torch
from torchvision import transforms
from ..util import ensure_tensor


#
# normalization functions
#


def standardize(raw, mean=None, std=None, axis=None, eps=1e-7):
    raw = raw.astype("float32")

    mean = raw.mean(axis=axis, keepdims=True) if mean is None else mean
    raw -= mean

    std = raw.std(axis=axis, keepdims=True) if std is None else std
    raw /= std + eps

    return raw


TORCH_DTYPES = {
    "float16": torch.float16,
    "float32": torch.float32,
    "float64": torch.float64,
    "complex64": torch.complex64,
    "complex128": torch.complex128,
    "uint8": torch.uint8,
    "int8": torch.int8,
    "int16": torch.int16,
    "int32": torch.int32,
    "int64": torch.int64,
    "bool": torch.bool,
}


def cast(inpt, typestring):
    if torch.is_tensor(inpt):
        assert typestring in TORCH_DTYPES, f"{typestring} not in TORCH_DTYPES"
        return inpt.to(TORCH_DTYPES[typestring])
    return inpt.astype(typestring)


def _normalize_torch(tensor, minval=None, maxval=None, axis=None, eps=1e-7):
    if axis:  # torch returns torch.return_types.min or torch.return_types.max
        minval = tensor.min(dim=axis, keepdim=True).values if minval is None else minval
        tensor -= minval

        maxval = tensor.max(dim=axis, keepdim=True).values if maxval is None else maxval
        tensor /= maxval + eps

        return tensor

    # keepdim can only be used in combination with dim
    minval = tensor.min() if minval is None else minval
    tensor -= minval

    maxval = tensor.max() if maxval is None else maxval
    tensor /= maxval + eps

    return tensor


def normalize(raw, minval=None, maxval=None, axis=None, eps=1e-7):
    raw = cast(raw, "float32")

    if torch.is_tensor(raw):
        return _normalize_torch(raw, minval=minval, maxval=maxval, axis=axis, eps=eps)

    minval = raw.min(axis=axis, keepdims=True) if minval is None else minval
    raw -= minval

    maxval = raw.max(axis=axis, keepdims=True) if maxval is None else maxval
    raw /= maxval + eps

    return raw


def normalize_percentile(raw, lower=1.0, upper=99.0, axis=None, eps=1e-7):
    v_lower = np.percentile(raw, lower, axis=axis, keepdims=True)
    v_upper = np.percentile(raw, upper, axis=axis, keepdims=True) - v_lower
    return normalize(raw, v_lower, v_upper, eps=eps)


# TODO
#
# intensity augmentations / noise augmentations
#


class RandomGamma:
    """
    Adjust contrast by non-liner transformation raising image value to power gamma.
    """

    def __init__(self, gamma=(0.5, 2), gain=1.0, clip_kwargs={"a_min": 0, "a_max": 1}):
        self.gamma = gamma
        self.gain = gain
        self.clip_kwargs = clip_kwargs

    def __call__(self, img):
        gamma = np.random.uniform(self.gamma[0], self.gamma[1])
        if gamma < 0.0:
            raise ValueError(f"Gamma must be non-negative. Got {gamma}")
        if self.gain < 0.0:
            raise ValueError(f"Gain must be non-negative. Got {gain}")
        result = self.gain * (img**gamma)
        if self.clip_kwargs:
            return np.clip(result, **self.clip_kwargs)
        return result


class RandomBrightness:
    """
    Adjust brightness by adding a random value to image.
    """

    def __init__(self, shift=(0, 1.0), clip_kwargs={"a_min": 0, "a_max": 1}):
        self.shift = shift
        self.clip_kwargs = clip_kwargs

    def __call__(self, img):
        shift = np.random.uniform(self.shift[0], self.shift[1])
        result = img + shift
        if self.clip_kwargs:
            return np.clip(result, **self.clip_kwargs)
        return result


# modified from https://github.com/kreshuklab/spoco/blob/main/spoco/transforms.py
class RandomContrast:
    """
    Adjust contrast by scaling image to `mean + alpha * (image - mean)`.
    """

    def __init__(self, alpha=(0.5, 2), mean=0.5, clip_kwargs={"a_min": 0, "a_max": 1}):
        self.alpha = alpha
        self.mean = mean
        self.clip_kwargs = clip_kwargs

    def __call__(self, img):
        alpha = np.random.uniform(self.alpha[0], self.alpha[1])
        result = self.mean + alpha * (img - self.mean)
        if self.clip_kwargs:
            return np.clip(result, **self.clip_kwargs)
        return result


class AdditiveGaussianNoise:
    """
    Add random Gaussian noise to image.
    """

    def __init__(self, scale=(0.0, 0.3), clip_kwargs={"a_min": 0, "a_max": 1}):
        self.scale = scale
        self.clip_kwargs = clip_kwargs

    def __call__(self, img):
        std = np.random.uniform(self.scale[0], self.scale[1])
        gaussian_noise = np.random.normal(0, std, size=img.shape)
        if self.clip_kwargs:
            return np.clip(img + gaussian_noise, 0, 1)
        return img + gaussian_noise


class AdditivePoissonNoise:
    """
    Add random Poisson noise to image.
    """

    # TODO: not sure if Poisson noise like this does make sense
    # for data that is already normalized
    def __init__(self, lam=(0.0, 0.1), clip_kwargs={"a_min": 0, "a_max": 1}):
        self.lam = lam
        self.clip_kwargs = clip_kwargs

    def __call__(self, img):
        lam = np.random.uniform(self.lam[0], self.lam[1])
        poisson_noise = np.random.poisson(lam, size=img.shape) / lam
        if self.clip_kwargs:
            return np.clip(img + poisson_noise, 0, 1)
        return img + poisson_noise


class PoissonNoise:
    """
    Add random data-dependent Poisson noise to image.
    """

    def __init__(self, multiplier=(5.0, 10.0), clip_kwargs={"a_min": 0, "a_max": 1}):
        self.multiplier = multiplier
        self.clip_kwargs = clip_kwargs

    def __call__(self, img):
        multiplier = np.random.uniform(self.multiplier[0], self.multiplier[1])
        offset = img.min()
        poisson_noise = np.random.poisson((img - offset) * multiplier)
        if isinstance(img, torch.Tensor):
            poisson_noise = torch.Tensor(poisson_noise)
        poisson_noise = poisson_noise / multiplier + offset
        if self.clip_kwargs:
            return np.clip(poisson_noise, **self.clip_kwargs)
        return poisson_noise


class GaussianBlur:
    """
    Blur the image.
    """

    def __init__(self, kernel_size=(2, 12), sigma=(0, 2.5)):
        self.kernel_size = kernel_size
        self.sigma = sigma

    def __call__(self, img):
        # sample kernel_size and make sure it is odd
        kernel_size = (
            2 * (np.random.randint(self.kernel_size[0], self.kernel_size[1]) // 2) + 1
        )
        # switch boundaries to make sure 0 is excluded from sampling
        sigma = np.random.uniform(self.sigma[1], self.sigma[0])
        return transforms.GaussianBlur(kernel_size, sigma=sigma)(img)


#
# default transformation:
# apply intensity augmentations and normalize
#


class RawTransform:
    def __init__(self, normalizer, augmentation1=None, augmentation2=None):
        self.normalizer = normalizer
        self.augmentation1 = augmentation1
        self.augmentation2 = augmentation2

    def __call__(self, raw):
        if self.augmentation1 is not None:
            raw = self.augmentation1(raw)
        raw = self.normalizer(raw)
        if self.augmentation2 is not None:
            raw = self.augmentation2(raw)
        return raw


def get_raw_transform(normalizer=standardize, augmentation1=None, augmentation2=None):
    return RawTransform(
        normalizer, augmentation1=augmentation1, augmentation2=augmentation2
    )


# The default values are made for an image with pixel values in
# range [0, 1]. That the image is in this range is ensured by an
# initial normalizations step.
def get_default_mean_teacher_augmentations(p=0.3):
    norm = normalize
    aug1 = transforms.Compose(
        [
            normalize,
            transforms.RandomApply([GaussianBlur()], p=p),
            transforms.RandomApply([PoissonNoise()], p=p / 2),
            transforms.RandomApply([AdditiveGaussianNoise()], p=p / 2),
        ]
    )
    aug2 = transforms.RandomApply(
        [RandomContrast(clip_kwargs={"a_min": 0, "a_max": 1})], p=p
    )
    return get_raw_transform(normalizer=norm, augmentation1=aug1, augmentation2=aug2)


def apply_augmentations_test(raw, p=0.3):
    tensors_raw = ensure_tensor(raw)
    aug = transforms.Compose(
        [
            normalize,
            transforms.RandomApply([RandomGamma(gamma=(3, 4))], p=p),
        ]
    )
    aug_raw = aug(tensors_raw)
    return aug_raw


def get_raw_augmentations(transform_inputs):
    transform_available = {
        "GaussianBlur": GaussianBlur,
        "RandomContrast": RandomContrast,
        "AdditiveGaussianNoise": AdditiveGaussianNoise,
        "AdditivePoissonNoise": AdditivePoissonNoise,
        "PoissonNoise": PoissonNoise,
        "RandomGamma": RandomGamma,
        "RandomBrightness": RandomBrightness,
    }
    group_of_transforms = [normalize]
    for t, p, param in transform_inputs:
        assert t in transform_available.keys(), f"{t} not available"
        group_of_transforms.append(
            transforms.RandomApply([transform_available[t](**param)], p=p["p"])
        )

    # compose aug using transforms.Compose from list of strings inputed as raw_tranforms
    aug = transforms.Compose(group_of_transforms)
    return aug

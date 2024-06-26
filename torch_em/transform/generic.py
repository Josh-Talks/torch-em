from typing import Any, Dict, Optional, Sequence, Union

import numpy as np
import torch

from skimage.transform import rescale


class Tile(torch.nn.Module):
    _params = None

    def __init__(self, reps: Sequence[int] = (2,), match_shape_exactly: bool = True):
        super().__init__()
        self.reps = reps
        self.match_shape_exactly = match_shape_exactly

    def forward(self, input: Union[torch.Tensor, np.ndarray], params: Optional[Dict[str, Any]] = None):
        assert not self.match_shape_exactly or len(input.shape) == len(self.reps), (input.shape, self.reps)
        if isinstance(input, torch.Tensor):
            # return torch.tile(input, self.reps)  # todo: use torch.tile (for pytorch >=1.8?)
            reps = list(self.reps)
            for _ in range(max(0, len(input.shape) - len(reps))):
                reps.insert(0, 1)

            for _ in range(max(0, len(reps) - len(input.shape))):
                input = input.unsqueeze(0)

            return input.repeat(*reps)
        elif isinstance(input, np.ndarray):
            return np.tile(input, self.reps)
        else:
            raise NotImplementedError(type(input))


# a simple way to compose transforms
class Compose:
    def __init__(self, *transforms):
        self.transforms = transforms

    def __call__(self, *inputs):
        outputs = self.transforms[0](*inputs)
        for trafo in self.transforms[1:]:
            outputs = trafo(*outputs)
        return outputs


class Rescale:
    def __init__(self, scale, with_channels=None):
        self.scale = scale
        self.with_channels = with_channels

    def _rescale_with_channels(self, input_, **kwargs):
        out = [rescale(inp, **kwargs)[None] for inp in input_]
        return np.concatenate(out, axis=0)

    def __call__(self, *inputs):
        if self.with_channels is None:
            outputs = tuple(rescale(inp, scale=self.scale, preserve_range=True) for inp in inputs)
        else:
            if isinstance(self.with_channels, (tuple, list)):
                assert len(self.with_channels) == len(inputs)
                with_channels = self.with_channels
            else:
                with_channels = [self.with_channels] * len(inputs)
            outputs = tuple(
                self._rescale_with_channels(inp, scale=self.scale, preserve_range=True) if wc else
                rescale(inp, scale=self.scale, preserve_range=True)
                for inp, wc in zip(inputs, with_channels)
            )
        if len(outputs) == 1:
            return outputs[0]
        return outputs

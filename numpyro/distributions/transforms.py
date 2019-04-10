# The implementation follows the design in PyTorch: torch.distributions.transforms.py

import jax.numpy as np
from jax.scipy.special import expit, logit

from numpyro.distributions import constraints


class Transform(object):
    domain = constraints.real
    codomain = constraints.real
    event_dim = 0

    def __call__(self, x):
        return NotImplementedError

    def inv(self, y):
        raise NotImplementedError

    def log_abs_det_jacobian(self, x, y):
        raise NotImplementedError


def _sum_rightmost(x, dim):
    return np.sum(x, axis=-1) if dim == 1 else x


class ComposeTransform(Transform):
    def __init__(self, parts):
        self.parts = parts

    @property
    def domain(self):
        return self.parts[0].domain

    @property
    def codomain(self):
        return self.parts[-1].codomain

    @property
    def event_dim(self):
        return max(p.event_dim for p in self.parts)

    def __call__(self, x):
        for part in self.parts:
            x = part(x)
        return x

    def inv(self, y):
        for part in self.parts[::-1]:
            y = part.inv(y)
        return y

    def log_abs_det_jacobian(self, x, y):
        result = 0.
        for part in self.parts[:-1]:
            y_tmp = part(x)
            result = result + _sum_rightmost(part.log_abs_det_jacobian(x, y_tmp),
                                             self.event_dim - part.event_dim)
            x = y_tmp
        result = result + _sum_rightmost(self.parts[-1].log_abs_det_jacobian(x, y),
                                         self.event_dim - self.parts[-1].event_dim)
        return result


class AffineTransform(Transform):
    # TODO: currently, just support scale > 0
    def __init__(self, loc, scale, domain=constraints.real):
        self.loc = loc
        self.scale = scale
        self.domain = domain

    @property
    def codomain(self):
        if self.domain is constraints.real:
            return constraints.real
        elif isinstance(self.domain, constraints.greater_than):
            return constraints.greater_than(self.loc + self.scale * self.domain.lower_bound)
        elif isinstance(self.domain, constraints.interval):
            return constraints.interval(self.loc + self.scale * self.domain.lower_bound,
                                        self.loc + self.scale * self.domain.upper_bound)
        else:
            raise NotImplementedError

    def __call__(self, x):
        return self.loc + self.scale * x

    def inv(self, y):
        return (y - self.loc) / self.scale

    def log_abs_det_jacobian(self, x, y):
        return np.broadcast_to(np.log(np.abs(self.scale)), x.shape)


class ExpTransform(Transform):
    codomain = constraints.positive

    def __call__(self, x):
        # XXX consider to clamp from below for stability if necessary
        return np.exp(x)

    def inv(self, y):
        return np.log(y)

    def log_abs_det_jacobian(self, x, y):
        return x


class IdentityTransform(Transform):

    def __call__(self, x):
        return x

    def inv(self, y):
        return y

    def log_abs_det_jacobian(self, x, y):
        return np.full(np.shape(x), 0.)


class SigmoidTransform(Transform):
    codomain = constraints.unit_interval

    def __call__(self, x):
        # XXX consider to clamp to (0, 1) for stability if necessary
        return expit(x)

    def inv(self, y):
        return logit(y)

    def log_abs_det_jacobian(self, x, y):
        return np.log(y * (1 - y))


class StickBreakingTransform(Transform):
    codomain = constraints.simplex
    event_dim = 1

    def __call__(self, x):
        # we shift x to obtain a balanced mapping (0, 0, ..., 0) -> (1/K, 1/K, ..., 1/K)
        x = x - np.log(x.shape[-1] - np.arange(x.shape[-1]))
        # convert to probabilities (relative to the remaining) of each fraction of the stick
        z = expit(x)  # XXX consider to clamp to (0, 1) for stability if necessary
        z1m_cumprod = np.cumprod(1 - z, axis=-1)
        pad_width = [(0, 0)] * x.ndim
        pad_width[-1] = (0, 1)
        z_padded = np.pad(z, pad_width, mode="constant", constant_values=1.)
        pad_width[-1] = (1, 0)
        z1m_cumprod_shifted = np.pad(z1m_cumprod, pad_width, mode="constant", constant_values=1.)
        return z_padded * z1m_cumprod_shifted

    def inv(self, y):
        y_crop = y[..., :-1]
        z1m_cumprod = 1 - y_crop.cumsum(-1)
        # hence x = logit(z) = log(z / (1 - z)) = y[::-1] / z1m_cumprod
        x = np.log(y_crop / z1m_cumprod)
        return x + np.log(x.shape[-1] - np.arange(x.shape[-1]))

    def log_abs_det_jacobian(self, x, y):
        # Ref: https://mc-stan.org/docs/2_19/reference-manual/simplex-transform-section.html
        # |det|(J) = Product(y * (1 - z))
        z = expit(x - np.log(x.shape[-1] - np.arange(x.shape[-1])))
        return np.sum(np.log(y[..., :-1] * (1 - z)), axis=-1)

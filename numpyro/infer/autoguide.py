# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

# Adapted from pyro.infer.autoguide
from abc import ABC, abstractmethod
from collections import namedtuple
from contextlib import ExitStack
from functools import partial
import warnings

import numpy as np

import jax
from jax import grad, hessian, lax, random
from jax.lax import select, stop_gradient
from jax.tree_util import tree_map

from numpyro.infer.hmc_util import dual_averaging
from numpyro.util import _versiontuple, find_stack_level

if _versiontuple(jax.__version__) >= (0, 2, 25):
    from jax.example_libraries import stax
else:
    from jax.experimental import stax

from jax.nn import log_sigmoid, sigmoid
import jax.numpy as jnp
from jax.scipy.special import logsumexp

import numpyro
from numpyro import handlers
import numpyro.distributions as dist
from numpyro.distributions import constraints
from numpyro.distributions.flows import (
    BlockNeuralAutoregressiveTransform,
    InverseAutoregressiveTransform,
)
from numpyro.distributions.transforms import (
    AffineTransform,
    ComposeTransform,
    IndependentTransform,
    LowerCholeskyAffine,
    PermuteTransform,
    UnpackTransform,
    biject_to,
)
from numpyro.distributions.util import (
    cholesky_of_inverse,
    periodic_repeat,
    sum_rightmost,
)
from numpyro.infer import Predictive
from numpyro.infer.elbo import Trace_ELBO
from numpyro.infer.initialization import init_to_median, init_to_uniform
from numpyro.infer.util import (
    helpful_support_errors,
    initialize_model,
    log_density,
    potential_energy,
)
from numpyro.nn.auto_reg_nn import AutoregressiveNN
from numpyro.nn.block_neural_arn import BlockNeuralAutoregressiveNN
from numpyro.util import not_jax_tracer

__all__ = [
    "AutoContinuous",
    "AutoGuide",
    "AutoDAIS",
    "AutoDiagonalNormal",
    "AutoLaplaceApproximation",
    "AutoLowRankMultivariateNormal",
    "AutoNormal",
    "AutoMultivariateNormal",
    "AutoBNAFNormal",
    "AutoIAFNormal",
    "AutoDelta",
    "AutoSemiDAIS",
    "AutoSurrogateLikelihoodDAIS",
]


class AutoGuide(ABC):
    """
    Base class for automatic guides.

    Derived classes must implement the :meth:`__call__` method.

    :param callable model: a pyro model
    :param str prefix: a prefix that will be prefixed to all param internal sites
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`init_strategy` section for available functions.
    :param callable create_plates: An optional function inputing the same
        ``*args,**kwargs`` as ``model()`` and returning a :class:`numpyro.plate`
        or iterable of plates. Plates not returned will be created
        automatically as usual. This is useful for data subsampling.
    """

    def __init__(
        self, model, *, prefix="auto", init_loc_fn=init_to_uniform, create_plates=None
    ):
        self.model = model
        self.prefix = prefix
        self.init_loc_fn = init_loc_fn
        self.create_plates = create_plates
        self.prototype_trace = None
        self._prototype_frames = {}
        self._prototype_frame_full_sizes = {}

    def _create_plates(self, *args, **kwargs):
        if self.create_plates is None:
            self.plates = {}
        else:
            plates = self.create_plates(*args, **kwargs)
            if isinstance(plates, numpyro.plate):
                plates = [plates]
            assert all(
                isinstance(p, numpyro.plate) for p in plates
            ), "create_plates() returned a non-plate"
            self.plates = {p.name: p for p in plates}
        for name, frame in sorted(self._prototype_frames.items()):
            if name not in self.plates:
                full_size = self._prototype_frame_full_sizes[name]
                self.plates[name] = numpyro.plate(
                    name, full_size, dim=frame.dim, subsample_size=frame.size
                )
        return self.plates

    def __getstate__(self):
        state = self.__dict__.copy()
        state.pop("plates", None)
        return state

    @abstractmethod
    def __call__(self, *args, **kwargs):
        """
        A guide with the same ``*args, **kwargs`` as the base ``model``.

        :return: A dict mapping sample site name to sampled value.
        :rtype: dict
        """
        raise NotImplementedError

    @abstractmethod
    def sample_posterior(self, rng_key, params, sample_shape=()):
        """
        Generate samples from the approximate posterior over the latent
        sites in the model.

        :param jax.random.PRNGKey rng_key: random key to be used draw samples.
        :param dict params: Current parameters of model and autoguide.
            The parameters can be obtained using :meth:`~numpyro.infer.svi.SVI.get_params`
            method from :class:`~numpyro.infer.svi.SVI`.
        :param tuple sample_shape: sample shape of each latent site, defaults to ().
        :return: a dict containing samples drawn the this guide.
        :rtype: dict
        """
        raise NotImplementedError

    def _setup_prototype(self, *args, **kwargs):
        rng_key = numpyro.prng_key()
        with handlers.block():
            (
                init_params,
                self._potential_fn_gen,
                postprocess_fn_gen,
                self.prototype_trace,
            ) = initialize_model(
                rng_key,
                self.model,
                init_strategy=self.init_loc_fn,
                dynamic_args=True,
                model_args=args,
                model_kwargs=kwargs,
            )
        self._potential_fn = self._potential_fn_gen(*args, **kwargs)
        postprocess_fn = postprocess_fn_gen(*args, **kwargs)
        # We apply a fixed seed just in case postprocess_fn requires
        # a random key to generate subsample indices. It does not matter
        # because we only collect deterministic sites.
        self._postprocess_fn = handlers.seed(postprocess_fn, rng_seed=0)
        self._init_locs = init_params[0]

        self._prototype_frames = {}
        self._prototype_plate_sizes = {}
        for name, site in self.prototype_trace.items():
            if site["type"] == "sample":
                if not site["is_observed"] and site["fn"].support.is_discrete:
                    # raise support errors early for discrete sites
                    with helpful_support_errors(site):
                        biject_to(site["fn"].support)
                for frame in site["cond_indep_stack"]:
                    if frame.name in self._prototype_frames:
                        assert (
                            frame == self._prototype_frames[frame.name]
                        ), f"The plate {frame.name} has inconsistent dim or size. Please check your model again."
                    else:
                        self._prototype_frames[frame.name] = frame
            elif site["type"] == "plate":
                self._prototype_frame_full_sizes[name] = site["args"][0]

    def median(self, params):
        """
        Returns the posterior median value of each latent variable.

        :param dict params: A dict containing parameter values.
            The parameters can be obtained using :meth:`~numpyro.infer.svi.SVI.get_params`
            method from :class:`~numpyro.infer.svi.SVI`.
        :return: A dict mapping sample site name to median value.
        :rtype: dict
        """
        raise NotImplementedError

    def quantiles(self, params, quantiles):
        """
        Returns posterior quantiles each latent variable. Example::

            print(guide.quantiles(params, [0.05, 0.5, 0.95]))

        :param dict params: A dict containing parameter values.
            The parameters can be obtained using :meth:`~numpyro.infer.svi.SVI.get_params`
            method from :class:`~numpyro.infer.svi.SVI`.
        :param list quantiles: A list of requested quantiles between 0 and 1.
        :return: A dict mapping sample site name to an array of quantile values.
        :rtype: dict
        """
        raise NotImplementedError


class AutoNormal(AutoGuide):
    """
    This implementation of :class:`AutoGuide` uses Normal distributions
    to construct a guide over the entire latent space. The guide does not
    depend on the model's ``*args, **kwargs``.

    This should be equivalent to :class:`AutoDiagonalNormal` , but with
    more convenient site names and with better support for mean field ELBO.

    Usage::

        guide = AutoNormal(model)
        svi = SVI(model, guide, ...)

    :param callable model: A NumPyro model.
    :param str prefix: a prefix that will be prefixed to all param internal sites.
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`init_strategy` section for available functions.
    :param float init_scale: Initial scale for the standard deviation of each
        (unconstrained transformed) latent variable.
    :param callable create_plates: An optional function inputing the same
        ``*args,**kwargs`` as ``model()`` and returning a :class:`numpyro.plate`
        or iterable of plates. Plates not returned will be created
        automatically as usual. This is useful for data subsampling.
    """

    scale_constraint = constraints.softplus_positive

    def __init__(
        self,
        model,
        *,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        init_scale=0.1,
        create_plates=None,
    ):
        self._init_scale = init_scale
        self._event_dims = {}
        super().__init__(
            model, prefix=prefix, init_loc_fn=init_loc_fn, create_plates=create_plates
        )

    def _setup_prototype(self, *args, **kwargs):
        super()._setup_prototype(*args, **kwargs)

        for name, site in self.prototype_trace.items():
            if site["type"] != "sample" or site["is_observed"]:
                continue

            event_dim = (
                site["fn"].event_dim
                + jnp.ndim(self._init_locs[name])
                - jnp.ndim(site["value"])
            )
            self._event_dims[name] = event_dim

            # If subsampling, repeat init_value to full size.
            for frame in site["cond_indep_stack"]:
                full_size = self._prototype_frame_full_sizes[frame.name]
                if full_size != frame.size:
                    dim = frame.dim - event_dim
                    self._init_locs[name] = periodic_repeat(
                        self._init_locs[name], full_size, dim
                    )

    def __call__(self, *args, **kwargs):
        if self.prototype_trace is None:
            # run model to inspect the model structure
            self._setup_prototype(*args, **kwargs)

        plates = self._create_plates(*args, **kwargs)
        result = {}
        for name, site in self.prototype_trace.items():
            if site["type"] != "sample" or site["is_observed"]:
                continue

            event_dim = self._event_dims[name]
            init_loc = self._init_locs[name]
            with ExitStack() as stack:
                for frame in site["cond_indep_stack"]:
                    stack.enter_context(plates[frame.name])

                site_loc = numpyro.param(
                    "{}_{}_loc".format(name, self.prefix), init_loc, event_dim=event_dim
                )
                site_scale = numpyro.param(
                    "{}_{}_scale".format(name, self.prefix),
                    jnp.full(jnp.shape(init_loc), self._init_scale),
                    constraint=self.scale_constraint,
                    event_dim=event_dim,
                )

                site_fn = dist.Normal(site_loc, site_scale).to_event(event_dim)
                if site["fn"].support is constraints.real or (
                    isinstance(site["fn"].support, constraints.independent)
                    and site["fn"].support.base_constraint is constraints.real
                ):
                    result[name] = numpyro.sample(name, site_fn)
                else:
                    with helpful_support_errors(site):
                        transform = biject_to(site["fn"].support)
                    guide_dist = dist.TransformedDistribution(site_fn, transform)
                    result[name] = numpyro.sample(name, guide_dist)

        return result

    def _constrain(self, latent_samples):
        name = list(latent_samples)[0]
        sample_shape = jnp.shape(latent_samples[name])[
            : jnp.ndim(latent_samples[name]) - jnp.ndim(self._init_locs[name])
        ]
        if sample_shape:
            flatten_samples = tree_map(
                lambda x: jnp.reshape(x, (-1,) + jnp.shape(x)[len(sample_shape) :]),
                latent_samples,
            )
            contrained_samples = lax.map(self._postprocess_fn, flatten_samples)
            return tree_map(
                lambda x: jnp.reshape(x, sample_shape + jnp.shape(x)[1:]),
                contrained_samples,
            )
        else:
            return self._postprocess_fn(latent_samples)

    def sample_posterior(self, rng_key, params, sample_shape=()):
        locs = {k: params["{}_{}_loc".format(k, self.prefix)] for k in self._init_locs}
        scales = {k: params["{}_{}_scale".format(k, self.prefix)] for k in locs}
        with handlers.seed(rng_seed=rng_key):
            latent_samples = {}
            for k in locs:
                latent_samples[k] = numpyro.sample(
                    k, dist.Normal(locs[k], scales[k]).expand_by(sample_shape)
                )
        return self._constrain(latent_samples)

    def median(self, params):
        locs = {
            k: params["{}_{}_loc".format(k, self.prefix)]
            for k, v in self._init_locs.items()
        }
        return self._constrain(locs)

    def quantiles(self, params, quantiles):
        quantiles = jnp.array(quantiles)
        locs = {k: params["{}_{}_loc".format(k, self.prefix)] for k in self._init_locs}
        scales = {k: params["{}_{}_scale".format(k, self.prefix)] for k in locs}
        latent = {
            k: dist.Normal(locs[k], scales[k]).icdf(
                quantiles.reshape((-1,) + (1,) * jnp.ndim(locs[k]))
            )
            for k in locs
        }
        return self._constrain(latent)


class AutoDelta(AutoGuide):
    """
    This implementation of :class:`AutoGuide` uses Delta distributions to
    construct a MAP guide over the entire latent space. The guide does not
    depend on the model's ``*args, **kwargs``.

    .. note:: This class does MAP inference in constrained space.

    Usage::

        guide = AutoDelta(model)
        svi = SVI(model, guide, ...)

    :param callable model: A NumPyro model.
    :param str prefix: a prefix that will be prefixed to all param internal sites.
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`init_strategy` section for available functions.
    :param callable create_plates: An optional function inputing the same
        ``*args,**kwargs`` as ``model()`` and returning a :class:`numpyro.plate`
        or iterable of plates. Plates not returned will be created
        automatically as usual. This is useful for data subsampling.
    """

    def __init__(
        self, model, *, prefix="auto", init_loc_fn=init_to_median, create_plates=None
    ):
        self._event_dims = {}
        super().__init__(
            model, prefix=prefix, init_loc_fn=init_loc_fn, create_plates=create_plates
        )

    def _setup_prototype(self, *args, **kwargs):
        super()._setup_prototype(*args, **kwargs)
        with numpyro.handlers.block():
            self._init_locs = {
                k: v
                for k, v in self._postprocess_fn(self._init_locs).items()
                if k in self._init_locs
            }
        for name, site in self.prototype_trace.items():
            if site["type"] != "sample" or site["is_observed"]:
                continue

            event_dim = site["fn"].event_dim
            self._event_dims[name] = event_dim

            # If subsampling, repeat init_value to full size.
            for frame in site["cond_indep_stack"]:
                full_size = self._prototype_frame_full_sizes[frame.name]
                if full_size != frame.size:
                    dim = frame.dim - event_dim
                    self._init_locs[name] = periodic_repeat(
                        self._init_locs[name], full_size, dim
                    )

    def __call__(self, *args, **kwargs):
        if self.prototype_trace is None:
            # run model to inspect the model structure
            self._setup_prototype(*args, **kwargs)

        plates = self._create_plates(*args, **kwargs)
        result = {}
        for name, site in self.prototype_trace.items():
            if site["type"] != "sample" or site["is_observed"]:
                continue

            event_dim = self._event_dims[name]
            init_loc = self._init_locs[name]
            with ExitStack() as stack:
                for frame in site["cond_indep_stack"]:
                    stack.enter_context(plates[frame.name])

                site_loc = numpyro.param(
                    "{}_{}_loc".format(name, self.prefix),
                    init_loc,
                    constraint=site["fn"].support,
                    event_dim=event_dim,
                )

                site_fn = dist.Delta(site_loc).to_event(event_dim)
                result[name] = numpyro.sample(name, site_fn)

        return result

    def sample_posterior(self, rng_key, params, *args, sample_shape=(), **kwargs):
        locs = {k: params["{}_{}_loc".format(k, self.prefix)] for k in self._init_locs}
        latent_samples = {
            k: jnp.broadcast_to(v, sample_shape + jnp.shape(v)) for k, v in locs.items()
        }
        deterministic_vars = [
            k for k, v in self.prototype_trace.items() if v["type"] == "deterministic"
        ]
        if not deterministic_vars:
            return latent_samples
        else:
            predictive = Predictive(
                model=self.model,
                posterior_samples=latent_samples,
                return_sites=deterministic_vars,
                batch_ndims=len(sample_shape),
            )
            deterministic_samples = predictive(rng_key, *args, **kwargs)
            return {**latent_samples, **deterministic_samples}

    def median(self, params):
        locs = {k: params["{}_{}_loc".format(k, self.prefix)] for k in self._init_locs}
        return locs


def _unravel_dict(x_flat, shape_dict):
    """Return `x` from the flatten version `x_flat`. Shape information
    of each item in `x` is defined in `shape_dict`.
    """
    assert jnp.ndim(x_flat) == 1
    assert isinstance(shape_dict, dict)
    x = {}
    curr_pos = next_pos = 0
    for name, shape in shape_dict.items():
        next_pos = curr_pos + int(np.prod(shape))
        x[name] = x_flat[curr_pos:next_pos].reshape(shape)
        curr_pos = next_pos
    assert next_pos == x_flat.shape[0]
    return x


def _ravel_dict(x):
    """Return the flatten version of `x` and shapes of each item in `x`."""
    assert isinstance(x, dict)
    shape_dict = {}
    x_flat = []
    for name, value in x.items():
        shape_dict[name] = jnp.shape(value)
        x_flat.append(jnp.reshape(value, -1))
    x_flat = jnp.concatenate(x_flat) if x_flat else jnp.zeros((0,))
    return x_flat, shape_dict


class AutoContinuous(AutoGuide):
    """
    Base class for implementations of continuous-valued Automatic
    Differentiation Variational Inference [1].

    Each derived class implements its own :meth:`_get_posterior` method.

    Assumes model structure and latent dimension are fixed, and all latent
    variables are continuous.

    **Reference:**

    1. *Automatic Differentiation Variational Inference*,
       Alp Kucukelbir, Dustin Tran, Rajesh Ranganath, Andrew Gelman, David M.
       Blei

    :param callable model: A NumPyro model.
    :param str prefix: a prefix that will be prefixed to all param internal sites.
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`init_strategy` section for available functions.
    """

    def _setup_prototype(self, *args, **kwargs):
        super()._setup_prototype(*args, **kwargs)
        self._init_latent, shape_dict = _ravel_dict(self._init_locs)
        unpack_latent = partial(_unravel_dict, shape_dict=shape_dict)
        # this is to match the behavior of Pyro, where we can apply
        # unpack_latent for a batch of samples
        self._unpack_latent = UnpackTransform(unpack_latent)
        self.latent_dim = jnp.size(self._init_latent)
        if self.latent_dim == 0:
            raise RuntimeError(
                "{} found no latent variables; Use an empty guide instead".format(
                    type(self).__name__
                )
            )
        for site in self.prototype_trace.values():
            if site["type"] == "sample" and not site["is_observed"]:
                for frame in site["cond_indep_stack"]:
                    if frame.size != self._prototype_frame_full_sizes[frame.name]:
                        raise ValueError(
                            "AutoContinuous guide does not support"
                            " local latent variables."
                        )

    @abstractmethod
    def _get_posterior(self):
        raise NotImplementedError

    def _sample_latent(self, *args, **kwargs):
        sample_shape = kwargs.pop("sample_shape", ())
        posterior = self._get_posterior()
        return numpyro.sample(
            "_{}_latent".format(self.prefix),
            posterior.expand_by(sample_shape),
            infer={"is_auxiliary": True},
        )

    def __call__(self, *args, **kwargs):
        if self.prototype_trace is None:
            # run model to inspect the model structure
            self._setup_prototype(*args, **kwargs)

        latent = self._sample_latent(*args, **kwargs)

        # unpack continuous latent samples
        result = {}

        for name, unconstrained_value in self._unpack_latent(latent).items():
            site = self.prototype_trace[name]
            with helpful_support_errors(site):
                transform = biject_to(site["fn"].support)
            value = transform(unconstrained_value)
            event_ndim = site["fn"].event_dim
            if numpyro.get_mask() is False:
                log_density = 0.0
            else:
                log_density = -transform.log_abs_det_jacobian(
                    unconstrained_value, value
                )
                log_density = sum_rightmost(
                    log_density, jnp.ndim(log_density) - jnp.ndim(value) + event_ndim
                )
            delta_dist = dist.Delta(
                value, log_density=log_density, event_dim=event_ndim
            )
            result[name] = numpyro.sample(name, delta_dist)

        return result

    def _unpack_and_constrain(self, latent_sample, params):
        def unpack_single_latent(latent):
            unpacked_samples = self._unpack_latent(latent)
            # XXX: we need to add param here to be able to replay model
            unpacked_samples.update(
                {
                    k: v
                    for k, v in params.items()
                    if k in self.prototype_trace
                    and self.prototype_trace[k]["type"] == "param"
                }
            )
            samples = self._postprocess_fn(unpacked_samples)
            # filter out param sites
            return {
                k: v
                for k, v in samples.items()
                if k in self.prototype_trace
                and self.prototype_trace[k]["type"] != "param"
            }

        sample_shape = jnp.shape(latent_sample)[:-1]
        if sample_shape:
            latent_sample = jnp.reshape(
                latent_sample, (-1, jnp.shape(latent_sample)[-1])
            )
            unpacked_samples = lax.map(unpack_single_latent, latent_sample)
            return tree_map(
                lambda x: jnp.reshape(x, sample_shape + jnp.shape(x)[1:]),
                unpacked_samples,
            )
        else:
            return unpack_single_latent(latent_sample)

    def get_base_dist(self):
        """
        Returns the base distribution of the posterior when reparameterized
        as a :class:`~numpyro.distributions.distribution.TransformedDistribution`. This
        should not depend on the model's `*args, **kwargs`.
        """
        raise NotImplementedError

    def get_transform(self, params):
        """
        Returns the transformation learned by the guide to generate samples from the unconstrained
        (approximate) posterior.

        :param dict params: Current parameters of model and autoguide.
            The parameters can be obtained using :meth:`~numpyro.infer.svi.SVI.get_params`
            method from :class:`~numpyro.infer.svi.SVI`.
        :return: the transform of posterior distribution
        :rtype: :class:`~numpyro.distributions.transforms.Transform`
        """
        posterior = handlers.substitute(self._get_posterior, params)()
        assert isinstance(
            posterior, dist.TransformedDistribution
        ), "posterior is not a transformed distribution"
        if len(posterior.transforms) > 0:
            return ComposeTransform(posterior.transforms)
        else:
            return posterior.transforms[0]

    def get_posterior(self, params):
        """
        Returns the posterior distribution.

        :param dict params: Current parameters of model and autoguide.
            The parameters can be obtained using :meth:`~numpyro.infer.svi.SVI.get_params`
            method from :class:`~numpyro.infer.svi.SVI`.
        """
        base_dist = self.get_base_dist()
        transform = self.get_transform(params)
        return dist.TransformedDistribution(base_dist, transform)

    def sample_posterior(self, rng_key, params, sample_shape=()):
        latent_sample = handlers.substitute(
            handlers.seed(self._sample_latent, rng_key), params
        )(sample_shape=sample_shape)
        return self._unpack_and_constrain(latent_sample, params)


class AutoDAIS(AutoContinuous):
    """
    This implementation of :class:`AutoDAIS` uses Differentiable Annealed
    Importance Sampling (DAIS) [1, 2] to construct a guide over the entire
    latent space. Samples from the variational distribution (i.e. guide)
    are generated using a combination of (uncorrected) Hamiltonian Monte Carlo
    and Annealed Importance Sampling. The same algorithm is called Uncorrected
    Hamiltonian Annealing in [1].

    Note that AutoDAIS cannot be used in conjunction with data subsampling.

    **Reference:**

    1. *MCMC Variational Inference via Uncorrected Hamiltonian Annealing*,
       Tomas Geffner, Justin Domke
    2. *Differentiable Annealed Importance Sampling and the Perils of Gradient Noise*,
       Guodong Zhang, Kyle Hsu, Jianing Li, Chelsea Finn, Roger Grosse

    Usage::

        guide = AutoDAIS(model)
        svi = SVI(model, guide, ...)

    :param callable model: A NumPyro model.
    :param str prefix: A prefix that will be prefixed to all param internal sites.
    :param int K: A positive integer that controls the number of HMC steps used.
        Defaults to 4.
    :param str base_dist: Controls whether the base Normal variational distribution
       is parameterized by a "diagonal" covariance matrix or a full-rank covariance
       matrix parameterized by a lower-triangular "cholesky" factor. Defaults to "diagonal".
    :param float eta_init: The initial value of the step size used in HMC. Defaults
        to 0.01.
    :param float eta_max: The maximum value of the learnable step size used in HMC.
        Defaults to 0.1.
    :param float gamma_init: The initial value of the learnable damping factor used
        during partial momentum refreshments in HMC. Defaults to 0.9.
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`init_strategy` section for available functions.
    :param float init_scale: Initial scale for the standard deviation of
        the base variational distribution for each (unconstrained transformed)
        latent variable. Defaults to 0.1.
    """

    def __init__(
        self,
        model,
        *,
        K=4,
        base_dist="diagonal",
        eta_init=0.01,
        eta_max=0.1,
        gamma_init=0.9,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        init_scale=0.1,
    ):
        if K < 1:
            raise ValueError("K must satisfy K >= 1 (got K = {})".format(K))
        if base_dist not in ["diagonal", "cholesky"]:
            raise ValueError('base_dist must be one of "diagonal" or "cholesky".')
        if eta_init <= 0.0 or eta_init >= eta_max:
            raise ValueError(
                "eta_init must be positive and satisfy eta_init < eta_max."
            )
        if eta_max <= 0.0:
            raise ValueError("eta_max must be positive.")
        if gamma_init <= 0.0 or gamma_init >= 1.0:
            raise ValueError("gamma_init must be in the open interval (0, 1).")
        if init_scale <= 0.0:
            raise ValueError("init_scale must be positive.")

        self.eta_init = eta_init
        self.eta_max = eta_max
        self.gamma_init = gamma_init
        self.K = K
        self.base_dist = base_dist
        self._init_scale = init_scale
        super().__init__(model, prefix=prefix, init_loc_fn=init_loc_fn)

    def _setup_prototype(self, *args, **kwargs):
        super()._setup_prototype(*args, **kwargs)

        for name, site in self.prototype_trace.items():
            if (
                site["type"] == "plate"
                and isinstance(site["args"][1], int)
                and site["args"][0] > site["args"][1]
            ):
                raise NotImplementedError(
                    "AutoDAIS cannot be used in conjunction with data subsampling."
                )

    def _get_posterior(self):
        raise NotImplementedError

    def _sample_latent(self, *args, **kwargs):
        def log_density(x):
            x_unpack = self._unpack_latent(x)
            with numpyro.handlers.block():
                return -self._potential_fn(x_unpack)

        eta0 = numpyro.param(
            "{}_eta0".format(self.prefix),
            self.eta_init,
            constraint=constraints.interval(0, self.eta_max),
        )
        eta_coeff = numpyro.param("{}_eta_coeff".format(self.prefix), 0.00)

        gamma = numpyro.param(
            "{}_gamma".format(self.prefix),
            self.gamma_init,
            constraint=constraints.interval(0, 1),
        )
        betas = numpyro.param(
            "{}_beta_increments".format(self.prefix),
            jnp.ones(self.K),
            constraint=constraints.positive,
        )
        betas = jnp.cumsum(betas)
        betas = betas / betas[-1]  # K-dimensional with betas[-1] = 1

        mass_matrix = numpyro.param(
            "{}_mass_matrix".format(self.prefix),
            jnp.ones(self.latent_dim),
            constraint=constraints.positive,
        )
        inv_mass_matrix = 0.5 / mass_matrix

        init_z_loc = numpyro.param(
            "{}_z_0_loc".format(self.prefix),
            self._init_latent,
        )

        if self.base_dist == "diagonal":
            init_z_scale = numpyro.param(
                "{}_z_0_scale".format(self.prefix),
                jnp.full(self.latent_dim, self._init_scale),
                constraint=constraints.positive,
            )
            base_z_dist = dist.Normal(init_z_loc, init_z_scale).to_event()
        elif self.base_dist == "cholesky":
            scale_tril = numpyro.param(
                "{}_z_0_scale_tril".format(self.prefix),
                jnp.identity(self.latent_dim) * self._init_scale,
                constraint=constraints.scaled_unit_lower_cholesky,
            )
            base_z_dist = dist.MultivariateNormal(init_z_loc, scale_tril=scale_tril)

        z_0 = numpyro.sample(
            "{}_z_0".format(self.prefix),
            base_z_dist,
            infer={"is_auxiliary": True},
        )
        momentum_dist = dist.Normal(0, mass_matrix).to_event()
        eps = numpyro.sample(
            "{}_momentum".format(self.prefix),
            momentum_dist.expand((self.K,)).to_event().mask(False),
            infer={"is_auxiliary": True},
        )

        def scan_body(carry, eps_beta):
            eps, beta = eps_beta
            eta = eta0 + eta_coeff * beta
            eta = jnp.clip(eta, a_min=0.0, a_max=self.eta_max)
            z_prev, v_prev, log_factor = carry
            z_half = z_prev + v_prev * eta * inv_mass_matrix
            q_grad = (1.0 - beta) * grad(base_z_dist.log_prob)(z_half)
            p_grad = beta * grad(log_density)(z_half)
            v_hat = v_prev + eta * (q_grad + p_grad)
            z = z_half + v_hat * eta * inv_mass_matrix
            v = gamma * v_hat + jnp.sqrt(1 - gamma**2) * eps
            delta_ke = momentum_dist.log_prob(v_prev) - momentum_dist.log_prob(v_hat)
            log_factor = log_factor + delta_ke
            return (z, v, log_factor), None

        v_0 = eps[-1]  # note the return value of scan doesn't depend on eps[-1]
        (z, _, log_factor), _ = jax.lax.scan(scan_body, (z_0, v_0, 0.0), (eps, betas))

        numpyro.factor("{}_factor".format(self.prefix), log_factor)

        return z

    def sample_posterior(self, rng_key, params, sample_shape=()):
        def _single_sample(_rng_key):
            latent_sample = handlers.substitute(
                handlers.seed(self._sample_latent, _rng_key), params
            )(sample_shape=())
            return self._unpack_and_constrain(latent_sample, params)

        if sample_shape:
            rng_key = random.split(rng_key, int(np.prod(sample_shape)))
            samples = lax.map(_single_sample, rng_key)
            return tree_map(
                lambda x: jnp.reshape(x, sample_shape + jnp.shape(x)[1:]),
                samples,
            )
        else:
            return _single_sample(rng_key)


class AutoSurrogateLikelihoodDAIS(AutoDAIS):
    """
    This implementation of :class:`AutoSurrogateLikelihoodDAIS` provides a
    mini-batchable family of variational distributions as described in [1].
    It combines a user-provided surrogate likelihood with Differentiable Annealed
    Importance Sampling (DAIS) [2, 3]. It is not applicable to models with local
    latent variables (see :class:`AutoSemiDAIS`), but unlike :class:`AutoDAIS`, it
    *can* be used in conjunction with data subsampling.

    **Reference:**

    1. *Surrogate likelihoods for variational annealed importance sampling*,
       Martin Jankowiak, Du Phan
    2. *MCMC Variational Inference via Uncorrected Hamiltonian Annealing*,
       Tomas Geffner, Justin Domke
    3. *Differentiable Annealed Importance Sampling and the Perils of Gradient Noise*,
       Guodong Zhang, Kyle Hsu, Jianing Li, Chelsea Finn, Roger Grosse

    Usage::

        # logistic regression model for data {X, Y}
        def model(X, Y):
            theta = numpyro.sample(
                "theta", dist.Normal(jnp.zeros(2), jnp.ones(2)).to_event(1)
            )
            with numpyro.plate("N", 100, subsample_size=10):
                X_batch = numpyro.subsample(X, event_dim=1)
                Y_batch = numpyro.subsample(Y, event_dim=0)
                numpyro.sample("obs", dist.Bernoulli(logits=theta @ X_batch.T), obs=Y_batch)

        # surrogate model defined by prior and surrogate likelihood.
        # a convenient choice for specifying the latter is to compute the likelihood on
        # a randomly chosen data subset (here {X_surr, Y_surr} of size 20) and then use
        # handlers.scale to scale the log likelihood by a vector of learnable weights.
        def surrogate_model(X_surr, Y_surr):
            theta = numpyro.sample(
                "theta", dist.Normal(jnp.zeros(2), jnp.ones(2)).to_event(1)
            )
            omegas = numpyro.param(
                "omegas", 5.0 * jnp.ones(20), constraint=dist.constraints.positive
            )
            with numpyro.plate("N", 20), numpyro.handlers.scale(scale=omegas):
                numpyro.sample("obs", dist.Bernoulli(logits=theta @ X_surr.T), obs=Y_surr)

        guide = AutoSurrogateLikelihoodDAIS(model, surrogate_model)
        svi = SVI(model, guide, ...)

    :param callable model: A NumPyro model.
    :param callable surrogate_model: A NumPyro model that is used as a surrogate model
        for guiding the HMC dynamics that define the variational distribution. In particular
        `surrogate_model` should contain the same prior as `model` but should contain a
        cheap-to-evaluate parametric ansatz for the likelihood. A simple ansatz for the latter
        involves computing the likelihood for a fixed subset of the data and scaling the resulting
        log likelihood by a learnable vector of positive weights. See the usage example above.
    :param str prefix: A prefix that will be prefixed to all param internal sites.
    :param int K: A positive integer that controls the number of HMC steps used.
        Defaults to 4.
    :param str base_dist: Controls whether the base Normal variational distribution
       is parameterized by a "diagonal" covariance matrix or a full-rank covariance
       matrix parameterized by a lower-triangular "cholesky" factor. Defaults to "diagonal".
    :param float eta_init: The initial value of the step size used in HMC. Defaults
        to 0.01.
    :param float eta_max: The maximum value of the learnable step size used in HMC.
        Defaults to 0.1.
    :param float gamma_init: The initial value of the learnable damping factor used
        during partial momentum refreshments in HMC. Defaults to 0.9.
    :param callable init_loc_fn: A per-site initialization function.
        See :ref:`init_strategy` section for available functions.
    :param float init_scale: Initial scale for the standard deviation of
        the base variational distribution for each (unconstrained transformed)
        latent variable. Defaults to 0.1.
    """

    def __init__(
        self,
        model,
        surrogate_model,
        *,
        K=4,
        eta_init=0.01,
        eta_max=0.1,
        gamma_init=0.9,
        prefix="auto",
        base_dist="diagonal",
        init_loc_fn=init_to_uniform,
        init_scale=0.1,
    ):
        super().__init__(
            model,
            K=K,
            eta_init=eta_init,
            eta_max=eta_max,
            gamma_init=gamma_init,
            prefix=prefix,
            init_loc_fn=init_loc_fn,
            init_scale=init_scale,
            base_dist=base_dist,
        )

        self.surrogate_model = surrogate_model

    def _setup_prototype(self, *args, **kwargs):
        AutoContinuous._setup_prototype(self, *args, **kwargs)

        rng_key = numpyro.prng_key()

        with numpyro.handlers.block():
            (_, self._surrogate_potential_fn, _, _) = initialize_model(
                rng_key,
                self.surrogate_model,
                init_strategy=self.init_loc_fn,
                dynamic_args=False,
                model_args=(),
                model_kwargs={},
            )

    def _sample_latent(self, *args, **kwargs):
        def blocked_surrogate_model(x):
            x_unpack = self._unpack_latent(x)
            with numpyro.handlers.block(expose_types=["param"]):
                return -self._surrogate_potential_fn(x_unpack)

        eta0 = numpyro.param(
            "{}_eta0".format(self.prefix),
            self.eta_init,
            constraint=constraints.interval(0, self.eta_max),
        )
        eta_coeff = numpyro.param("{}_eta_coeff".format(self.prefix), 0.0)

        gamma = numpyro.param(
            "{}_gamma".format(self.prefix),
            self.gamma_init,
            constraint=constraints.interval(0, 1),
        )
        betas = numpyro.param(
            "{}_beta_increments".format(self.prefix),
            jnp.ones(self.K),
            constraint=constraints.positive,
        )
        betas = jnp.cumsum(betas)
        betas = betas / betas[-1]  # K-dimensional with betas[-1] = 1

        mass_matrix = numpyro.param(
            "{}_mass_matrix".format(self.prefix),
            jnp.ones(self.latent_dim),
            constraint=constraints.positive,
        )
        inv_mass_matrix = 0.5 / mass_matrix

        init_z_loc = numpyro.param("{}_z_0_loc".format(self.prefix), self._init_latent)

        if self.base_dist == "diagonal":
            init_z_scale = numpyro.param(
                "{}_z_0_scale".format(self.prefix),
                jnp.full(self.latent_dim, self._init_scale),
                constraint=constraints.positive,
            )
            base_z_dist = dist.Normal(init_z_loc, init_z_scale).to_event()
        else:
            scale_tril = numpyro.param(
                "{}_scale_tril".format(self.prefix),
                jnp.identity(self.latent_dim) * self._init_scale,
                constraint=constraints.scaled_unit_lower_cholesky,
            )
            base_z_dist = dist.MultivariateNormal(init_z_loc, scale_tril=scale_tril)

        z_0 = numpyro.sample(
            "{}_z_0".format(self.prefix), base_z_dist, infer={"is_auxiliary": True}
        )

        base_z_dist_log_prob = base_z_dist.log_prob

        momentum_dist = dist.Normal(0, mass_matrix).to_event()
        eps = numpyro.sample(
            "{}_momentum".format(self.prefix),
            momentum_dist.expand((self.K,)).to_event().mask(False),
            infer={"is_auxiliary": True},
        )

        def scan_body(carry, eps_beta):
            eps, beta = eps_beta
            eta = eta0 + eta_coeff * beta
            eta = jnp.clip(eta, a_min=0.0, a_max=self.eta_max)
            z_prev, v_prev, log_factor = carry
            z_half = z_prev + v_prev * eta * inv_mass_matrix
            q_grad = (1.0 - beta) * grad(base_z_dist_log_prob)(z_half)
            p_grad = beta * grad(blocked_surrogate_model)(z_half)
            v_hat = v_prev + eta * (q_grad + p_grad)
            z = z_half + v_hat * eta * inv_mass_matrix
            v = gamma * v_hat + jnp.sqrt(1 - gamma**2) * eps
            delta_ke = momentum_dist.log_prob(v_prev) - momentum_dist.log_prob(v_hat)
            log_factor = log_factor + delta_ke
            return (z, v, log_factor), None

        v_0 = eps[-1]  # note the return value of scan doesn't depend on eps[-1]
        (z, _, log_factor), _ = jax.lax.scan(scan_body, (z_0, v_0, 0.0), (eps, betas))

        numpyro.factor("{}_factor".format(self.prefix), log_factor)

        return z


def _subsample_model(model, *args, **kwargs):
    data = kwargs.pop("_subsample_idx", {})
    with handlers.substitute(data=data):
        return model(*args, **kwargs)


class AutoSemiDAIS(AutoGuide):
    r"""
    This implementation of :class:`AutoSemiDAIS` [1] combines a parametric
    variational distribution over global latent variables with Differentiable
    Annealed Importance Sampling (DAIS) [2, 3] to infer local latent variables.
    Unlike :class:`AutoDAIS` this guide can be used in conjunction with data subsampling.
    Note that the resulting ELBO can be understood as a particular realization of a
    'locally enhanced bound' as described in reference [4].

    **References:**

    1. *Surrogate Likelihoods for Variational Annealed Importance Sampling*,
       Martin Jankowiak, Du Phan
    2. *MCMC Variational Inference via Uncorrected Hamiltonian Annealing*,
       Tomas Geffner, Justin Domke
    3. *Differentiable Annealed Importance Sampling and the Perils of Gradient Noise*,
       Guodong Zhang, Kyle Hsu, Jianing Li, Chelsea Finn, Roger Grosse
    4. *Variational Inference with Locally Enhanced Bounds for Hierarchical Models*,
       Tomas Geffner, Justin Domke

    Usage::

        def global_model():
            return numpyro.sample("theta", dist.Normal(0, 1))

        def local_model(theta):
            with numpyro.plate("data", 8, subsample_size=2):
                tau = numpyro.sample("tau", dist.Gamma(5.0, 5.0))
                numpyro.sample("obs", dist.Normal(0.0, tau), obs=jnp.ones(2))

        model = lambda: local_model(global_model())
        global_guide = AutoNormal(global_model)
        guide = AutoSemiDAIS(model, local_model, global_guide, K=4)
        svi = SVI(model, guide, ...)

        # sample posterior for particular data subset {3, 7}
        with handlers.substitute(data={"data": jnp.array([3, 7])}):
            samples = guide.sample_posterior(random.PRNGKey(1), params)

    :param callable model: A NumPyro model with global and local latent variables.
    :param callable local_model: The portion of `model` that includes the local latent variables only.
        The signature of `local_model` should be the return type of the global model with global latent
        variables only.
    :param callable global_guide: A guide for the global latent variables, e.g. an autoguide.
        The return type should be a dictionary of latent sample sites names and corresponding samples.
        If there is no global variable in the model, we can set this to None.
    :param callable local_guide: An optional guide for specifying the DAIS base distribution for
        local latent variables.
    :param str prefix: A prefix that will be prefixed to all internal sites.
    :param int K: A positive integer that controls the number of HMC steps used.
        Defaults to 4.
    :param float eta_init: The initial value of the step size used in HMC. Defaults
        to 0.01.
    :param float eta_max: The maximum value of the learnable step size used in HMC.
        Defaults to 0.1.
    :param float gamma_init: The initial value of the learnable damping factor used
        during partial momentum refreshments in HMC. Defaults to 0.9.
    :param float init_scale: Initial scale for the standard deviation of the variational
        distribution for each (unconstrained transformed) local latent variable. Defaults to 0.1.
    """

    def __init__(
        self,
        model,
        local_model,
        global_guide,
        local_guide=None,
        *,
        prefix="auto",
        K=4,
        eta_init=0.01,
        eta_max=0.1,
        gamma_init=0.9,
        init_scale=0.1,
    ):
        # init_loc_fn is only used to inspect the model.
        super().__init__(model, prefix=prefix, init_loc_fn=init_to_uniform)
        if K < 1:
            raise ValueError("K must satisfy K >= 1 (got K = {})".format(K))
        if eta_init <= 0.0 or eta_init >= eta_max:
            raise ValueError(
                "eta_init must be positive and satisfy eta_init < eta_max."
            )
        if eta_max <= 0.0:
            raise ValueError("eta_max must be positive.")
        if gamma_init <= 0.0 or gamma_init >= 1.0:
            raise ValueError("gamma_init must be in the open interval (0, 1).")
        if init_scale <= 0.0:
            raise ValueError("init_scale must be positive.")

        self.local_model = local_model
        self.global_guide = global_guide
        self.local_guide = local_guide
        self.eta_init = eta_init
        self.eta_max = eta_max
        self.gamma_init = gamma_init
        self.K = K
        self.init_scale = init_scale

    def _setup_prototype(self, *args, **kwargs):
        super()._setup_prototype(*args, **kwargs)
        # extract global/local/local_dim/plates
        assert self.prototype_trace is not None
        subsample_plates = {
            name: site
            for name, site in self.prototype_trace.items()
            if site["type"] == "plate"
            and isinstance(site["args"][1], int)
            and site["args"][0] > site["args"][1]
        }
        num_plates = len(subsample_plates)
        assert (
            num_plates == 1
        ), f"AutoSemiDAIS assumes that the model contains exactly 1 plate with data subsampling but got {num_plates}."
        plate_name = list(subsample_plates.keys())[0]
        local_vars = []
        subsample_axes = {}
        plate_dim = None
        for name, site in self.prototype_trace.items():
            if site["type"] == "sample" and not site["is_observed"]:
                for frame in site["cond_indep_stack"]:
                    if frame.name == plate_name:
                        if plate_dim is None:
                            plate_dim = frame.dim
                        local_vars.append(name)
                        subsample_axes[name] = plate_dim - site["fn"].event_dim
                        break
        if len(local_vars) == 0:
            raise RuntimeError(
                "There are no local variables in the `{plate_name}` plate."
                " AutoSemiDAIS is appropriate for models with local variables."
            )

        local_init_locs = {
            name: value for name, value in self._init_locs.items() if name in local_vars
        }

        one_sample = {
            k: jnp.take(v, 0, axis=subsample_axes[k])
            for k, v in local_init_locs.items()
        }
        _, shape_dict = _ravel_dict(one_sample)
        self._pack_local_latent = jax.vmap(
            lambda x: _ravel_dict(x)[0], in_axes=(subsample_axes,)
        )
        local_init_latent = self._pack_local_latent(local_init_locs)
        unpack_latent = partial(_unravel_dict, shape_dict=shape_dict)
        # this is to match the behavior of Pyro, where we can apply
        # unpack_latent for a batch of samples
        self._unpack_local_latent = jax.vmap(
            UnpackTransform(unpack_latent), out_axes=subsample_axes
        )
        plate_full_size, plate_subsample_size = subsample_plates[plate_name]["args"]
        self._local_latent_dim = jnp.size(local_init_latent) // plate_subsample_size
        self._local_plate = (plate_name, plate_full_size, plate_subsample_size)

        if self.global_guide is not None:
            with handlers.block(), handlers.seed(rng_seed=0):
                local_args = (self.global_guide.model(*args, **kwargs),)
                local_kwargs = {}
        else:
            local_args = args
            local_kwargs = kwargs.copy()

        if self.local_guide is not None:
            with handlers.block(), handlers.trace() as tr, handlers.seed(rng_seed=0):
                self.local_guide(*local_args, **local_kwargs)
            self.prototype_local_guide_trace = tr

        with handlers.block(), handlers.trace() as tr, handlers.seed(rng_seed=0):
            self.local_model(*local_args, **local_kwargs)
        self.prototype_local_model_trace = tr

    def __call__(self, *args, **kwargs):
        if self.prototype_trace is None:
            # run model to inspect the model structure
            self._setup_prototype(*args, **kwargs)

        global_latents, local_latent_flat = self._sample_latent(*args, **kwargs)

        # unpack continuous latent samples
        result = global_latents.copy()
        _, N, subsample_size = self._local_plate

        for name, unconstrained_value in self._unpack_local_latent(
            local_latent_flat
        ).items():
            site = self.prototype_trace[name]
            with helpful_support_errors(site):
                transform = biject_to(site["fn"].support)
            value = transform(unconstrained_value)
            event_ndim = site["fn"].event_dim
            if numpyro.get_mask() is False:
                log_density = 0.0
            else:
                log_density = -transform.log_abs_det_jacobian(
                    unconstrained_value, value
                )
                log_density = (N / subsample_size) * sum_rightmost(
                    log_density, jnp.ndim(log_density) - jnp.ndim(value) + event_ndim
                )
            delta_dist = dist.Delta(
                value, log_density=log_density, event_dim=event_ndim
            )
            result[name] = numpyro.sample(name, delta_dist)

        return result

    def _get_posterior(self):
        raise NotImplementedError

    def _sample_latent(self, *args, **kwargs):
        kwargs.pop("sample_shape", ())

        if self.global_guide is not None:
            global_latents = self.global_guide(*args, **kwargs)
            rng_key = numpyro.prng_key()
            with handlers.block(), handlers.seed(rng_seed=rng_key), handlers.substitute(
                data=global_latents
            ):
                global_outputs = self.global_guide.model(*args, **kwargs)
            local_args = (global_outputs,)
            local_kwargs = {}
        else:
            global_latents = {}
            local_args = args
            local_kwargs = kwargs.copy()

        local_guide_params = {}
        if self.local_guide is not None:
            for name, site in self.prototype_local_guide_trace.items():
                if site["type"] == "param":
                    local_guide_params[name] = numpyro.param(
                        name, site["value"], **site["kwargs"]
                    )

        local_model_params = {}
        for name, site in self.prototype_local_model_trace.items():
            if site["type"] == "param":
                local_model_params[name] = numpyro.param(
                    name, site["value"], **site["kwargs"]
                )

        def make_local_log_density(*local_args, **local_kwargs):
            def fn(x):
                x_unpack = self._unpack_local_latent(x)
                with numpyro.handlers.block():
                    return -potential_energy(
                        partial(_subsample_model, self.local_model),
                        local_args,
                        local_kwargs,
                        {**x_unpack, **local_model_params},
                    )

            return fn

        plate_name, N, subsample_size = self._local_plate
        D, K = self._local_latent_dim, self.K

        with numpyro.plate(plate_name, N, subsample_size=subsample_size) as idx:
            eta0 = numpyro.param(
                "{}_eta0".format(self.prefix),
                jnp.ones(N) * self.eta_init,
                constraint=constraints.interval(0, self.eta_max),
                event_dim=0,
            )
            eta_coeff = numpyro.param(
                "{}_eta_coeff".format(self.prefix), jnp.zeros(N), event_dim=0
            )

            gamma = numpyro.param(
                "{}_gamma".format(self.prefix),
                jnp.ones(N) * 0.9,
                constraint=constraints.interval(0, 1),
                event_dim=0,
            )
            betas = numpyro.param(
                "{}_beta_increments".format(self.prefix),
                jnp.ones((N, K)),
                constraint=constraints.positive,
                event_dim=1,
            )
            betas = jnp.cumsum(betas, axis=-1)
            betas = betas / betas[..., -1:]

            mass_matrix = numpyro.param(
                "{}_mass_matrix".format(self.prefix),
                jnp.ones((N, D)),
                constraint=constraints.positive,
                event_dim=1,
            )
            inv_mass_matrix = 0.5 / mass_matrix
            assert inv_mass_matrix.shape == (subsample_size, D)

            local_kwargs["_subsample_idx"] = {plate_name: idx}
            if self.local_guide is not None:
                key = numpyro.prng_key()
                subsample_guide = partial(_subsample_model, self.local_guide)
                with handlers.block(), handlers.trace() as tr, handlers.seed(
                    rng_seed=key
                ), handlers.substitute(data=local_guide_params):
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        subsample_guide(*local_args, **local_kwargs)
                    latent = {
                        name: biject_to(site["fn"].support).inv(site["value"])
                        for name, site in tr.items()
                        if site["type"] == "sample"
                        and not site.get("is_observed", False)
                    }
                    z_0 = self._pack_local_latent(latent)

                def base_z_dist_log_prob(z):
                    latent = self._unpack_local_latent(z)
                    assert isinstance(latent, dict)
                    with handlers.block():
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            scale = N / subsample_size
                            return (
                                -potential_energy(
                                    subsample_guide,
                                    local_args,
                                    local_kwargs,
                                    {**local_guide_params, **latent},
                                )
                                / scale
                            )

                # The log_prob of z_0 will be broadcasted to `subsample_size` because this statement
                # is run under the subsample plate. Hence we divide the log_prob by `subsample_size`.
                numpyro.factor(
                    "{}_z_0_factor".format(self.prefix),
                    base_z_dist_log_prob(z_0) / subsample_size,
                )
            else:
                z_0_loc_init = jnp.zeros((N, D))
                z_0_loc = numpyro.param(
                    "{}_z_0_loc".format(self.prefix), z_0_loc_init, event_dim=1
                )
                z_0_scale_init = jnp.ones((N, D)) * self.init_scale
                z_0_scale = numpyro.param(
                    "{}_z_0_scale".format(self.prefix),
                    z_0_scale_init,
                    constraint=constraints.positive,
                    event_dim=1,
                )
                base_z_dist = dist.Normal(z_0_loc, z_0_scale).to_event(1)
                assert base_z_dist.shape() == (subsample_size, D)
                z_0 = numpyro.sample(
                    "{}_z_0".format(self.prefix),
                    base_z_dist,
                    infer={"is_auxiliary": True},
                )

                def base_z_dist_log_prob(x):
                    return base_z_dist.log_prob(x).sum()

            momentum_dist = dist.Normal(0, mass_matrix).to_event(1)
            eps = numpyro.sample(
                "{}_momentum".format(self.prefix),
                dist.Normal(0, mass_matrix[..., None])
                .expand([subsample_size, D, K])
                .to_event(2)
                .mask(False),
                infer={"is_auxiliary": True},
            )

            local_log_density = make_local_log_density(*local_args, **local_kwargs)

            def scan_body(carry, eps_beta):
                eps, beta = eps_beta
                eta = eta0 + eta_coeff * beta
                eta = jnp.clip(eta, a_min=0.0, a_max=self.eta_max)
                assert eps.shape == (subsample_size, D)
                assert eta.shape == beta.shape == (subsample_size,)
                z_prev, v_prev, log_factor = carry
                z_half = z_prev + v_prev * eta[:, None] * inv_mass_matrix
                q_grad = (1.0 - beta[:, None]) * grad(base_z_dist_log_prob)(z_half)
                p_grad = (
                    beta[:, None]
                    * (subsample_size / N)
                    * grad(local_log_density)(z_half)
                )
                assert q_grad.shape == p_grad.shape == (subsample_size, D)
                v_hat = v_prev + eta[:, None] * (q_grad + p_grad)
                z = z_half + v_hat * eta[:, None] * inv_mass_matrix
                v = gamma[:, None] * v_hat + jnp.sqrt(1 - gamma[:, None] ** 2) * eps
                delta_ke = momentum_dist.log_prob(v_prev) - momentum_dist.log_prob(
                    v_hat
                )
                assert delta_ke.shape == (subsample_size,)
                log_factor = log_factor + delta_ke
                return (z, v, log_factor), None

            v_0 = eps[
                :, :, -1
            ]  # note the return value of scan doesn't depend on eps[:, :, -1]
            assert eps.shape == (subsample_size, D, K)
            assert betas.shape == (subsample_size, K)

            eps_T = jnp.moveaxis(eps, -1, 0)
            (z, _, log_factor), _ = jax.lax.scan(
                scan_body, (z_0, v_0, jnp.zeros(subsample_size)), (eps_T, betas.T)
            )
            assert log_factor.shape == (subsample_size,)

            numpyro.factor("{}_local_dais_factor".format(self.prefix), log_factor)
            return global_latents, z

    def sample_posterior(self, rng_key, params, *args, sample_shape=(), **kwargs):
        def _single_sample(_rng_key):
            global_latents, local_flat = handlers.substitute(
                handlers.seed(self._sample_latent, _rng_key), params
            )(*args, **kwargs)
            results = global_latents.copy()
            for name, unconstrained_value in self._unpack_local_latent(
                local_flat
            ).items():
                site = self.prototype_trace[name]
                transform = biject_to(site["fn"].support)
                value = transform(unconstrained_value)
                results[name] = value
            return results

        if sample_shape:
            rng_key = random.split(rng_key, int(np.prod(sample_shape)))
            samples = lax.map(_single_sample, rng_key)
            return tree_map(
                lambda x: jnp.reshape(x, sample_shape + jnp.shape(x)[1:]),
                samples,
            )
        else:
            return _single_sample(rng_key)


class AutoDiagonalNormal(AutoContinuous):
    """
    This implementation of :class:`AutoContinuous` uses a Normal distribution
    with a diagonal covariance matrix to construct a guide over the entire
    latent space. The guide does not depend on the model's ``*args, **kwargs``.

    Usage::

        guide = AutoDiagonalNormal(model, ...)
        svi = SVI(model, guide, ...)
    """

    scale_constraint = constraints.softplus_positive

    def __init__(
        self,
        model,
        *,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        init_scale=0.1,
    ):
        if init_scale <= 0:
            raise ValueError("Expected init_scale > 0. but got {}".format(init_scale))
        self._init_scale = init_scale
        super().__init__(model, prefix=prefix, init_loc_fn=init_loc_fn)

    def _get_posterior(self):
        loc = numpyro.param("{}_loc".format(self.prefix), self._init_latent)
        scale = numpyro.param(
            "{}_scale".format(self.prefix),
            jnp.full(self.latent_dim, self._init_scale),
            constraint=self.scale_constraint,
        )
        return dist.Normal(loc, scale)

    def get_base_dist(self):
        return dist.Normal(jnp.zeros(self.latent_dim), 1).to_event(1)

    def get_transform(self, params):
        loc = params["{}_loc".format(self.prefix)]
        scale = params["{}_scale".format(self.prefix)]
        return IndependentTransform(AffineTransform(loc, scale), 1)

    def get_posterior(self, params):
        """
        Returns a diagonal Normal posterior distribution.
        """
        transform = self.get_transform(params).base_transform
        return dist.Normal(transform.loc, transform.scale)

    def median(self, params):
        loc = params["{}_loc".format(self.prefix)]
        return self._unpack_and_constrain(loc, params)

    def quantiles(self, params, quantiles):
        quantiles = jnp.array(quantiles)[..., None]
        latent = self.get_posterior(params).icdf(quantiles)
        return self._unpack_and_constrain(latent, params)


class AutoMultivariateNormal(AutoContinuous):
    """
    This implementation of :class:`AutoContinuous` uses a MultivariateNormal
    distribution to construct a guide over the entire latent space.
    The guide does not depend on the model's ``*args, **kwargs``.

    Usage::

        guide = AutoMultivariateNormal(model, ...)
        svi = SVI(model, guide, ...)
    """

    scale_tril_constraint = constraints.scaled_unit_lower_cholesky

    def __init__(
        self,
        model,
        *,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        init_scale=0.1,
    ):
        if init_scale <= 0:
            raise ValueError("Expected init_scale > 0. but got {}".format(init_scale))
        self._init_scale = init_scale
        super().__init__(model, prefix=prefix, init_loc_fn=init_loc_fn)

    def _get_posterior(self):
        loc = numpyro.param("{}_loc".format(self.prefix), self._init_latent)
        scale_tril = numpyro.param(
            "{}_scale_tril".format(self.prefix),
            jnp.identity(self.latent_dim) * self._init_scale,
            constraint=self.scale_tril_constraint,
        )
        return dist.MultivariateNormal(loc, scale_tril=scale_tril)

    def get_base_dist(self):
        return dist.Normal(jnp.zeros(self.latent_dim), 1).to_event(1)

    def get_transform(self, params):
        loc = params["{}_loc".format(self.prefix)]
        scale_tril = params["{}_scale_tril".format(self.prefix)]
        return LowerCholeskyAffine(loc, scale_tril)

    def get_posterior(self, params):
        """
        Returns a multivariate Normal posterior distribution.
        """
        transform = self.get_transform(params)
        return dist.MultivariateNormal(transform.loc, scale_tril=transform.scale_tril)

    def median(self, params):
        loc = params["{}_loc".format(self.prefix)]
        return self._unpack_and_constrain(loc, params)

    def quantiles(self, params, quantiles):
        transform = self.get_transform(params)
        quantiles = jnp.array(quantiles)[..., None]
        latent = dist.Normal(
            transform.loc, jnp.linalg.norm(transform.scale_tril, axis=-1)
        ).icdf(quantiles)
        return self._unpack_and_constrain(latent, params)


class AutoLowRankMultivariateNormal(AutoContinuous):
    """
    This implementation of :class:`AutoContinuous` uses a LowRankMultivariateNormal
    distribution to construct a guide over the entire latent space.
    The guide does not depend on the model's ``*args, **kwargs``.

    Usage::

        guide = AutoLowRankMultivariateNormal(model, rank=2, ...)
        svi = SVI(model, guide, ...)
    """

    scale_constraint = constraints.softplus_positive

    def __init__(
        self,
        model,
        *,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        init_scale=0.1,
        rank=None,
    ):
        if init_scale <= 0:
            raise ValueError("Expected init_scale > 0. but got {}".format(init_scale))
        self._init_scale = init_scale
        self.rank = rank
        super(AutoLowRankMultivariateNormal, self).__init__(
            model, prefix=prefix, init_loc_fn=init_loc_fn
        )

    def _get_posterior(self, *args, **kwargs):
        rank = int(round(self.latent_dim**0.5)) if self.rank is None else self.rank
        loc = numpyro.param("{}_loc".format(self.prefix), self._init_latent)
        cov_factor = numpyro.param(
            "{}_cov_factor".format(self.prefix), jnp.zeros((self.latent_dim, rank))
        )
        scale = numpyro.param(
            "{}_scale".format(self.prefix),
            jnp.full(self.latent_dim, self._init_scale),
            constraint=self.scale_constraint,
        )
        cov_diag = scale * scale
        cov_factor = cov_factor * scale[..., None]
        return dist.LowRankMultivariateNormal(loc, cov_factor, cov_diag)

    def get_base_dist(self):
        return dist.Normal(jnp.zeros(self.latent_dim), 1).to_event(1)

    def get_transform(self, params):
        posterior = self.get_posterior(params)
        return LowerCholeskyAffine(posterior.loc, posterior.scale_tril)

    def get_posterior(self, params):
        """
        Returns a lowrank multivariate Normal posterior distribution.
        """
        loc = params["{}_loc".format(self.prefix)]
        cov_factor = params["{}_cov_factor".format(self.prefix)]
        scale = params["{}_scale".format(self.prefix)]
        cov_diag = scale * scale
        cov_factor = cov_factor * scale[..., None]
        return dist.LowRankMultivariateNormal(loc, cov_factor, cov_diag)

    def median(self, params):
        loc = params["{}_loc".format(self.prefix)]
        return self._unpack_and_constrain(loc, params)

    def quantiles(self, params, quantiles):
        loc = params[f"{self.prefix}_loc"]
        cov_factor = params[f"{self.prefix}_cov_factor"]
        scale = params[f"{self.prefix}_scale"]
        scale = scale * jnp.sqrt(jnp.square(cov_factor).sum(-1) + 1)
        quantiles = jnp.array(quantiles)[..., None]
        latent = dist.Normal(loc, scale).icdf(quantiles)
        return self._unpack_and_constrain(latent, params)


class AutoLaplaceApproximation(AutoContinuous):
    r"""
    Laplace approximation (quadratic approximation) approximates the posterior
    :math:`\log p(z | x)` by a multivariate normal distribution in the
    unconstrained space. Under the hood, it uses Delta distributions to
    construct a MAP (i.e. point estimate) guide over the entire (unconstrained) latent
    space. Its covariance is given by the inverse of the hessian of :math:`-\log p(x, z)`
    at the MAP point of `z`.

    Usage::

        guide = AutoLaplaceApproximation(model, ...)
        svi = SVI(model, guide, ...)

    :param callable hessian_fn: EXPERIMENTAL a function that takes a function `f`
        and a vector `x`and returns the hessian of `f` at `x`. By default, we use
        ``lambda f, x: jax.hessian(f)(x)``. Other alternatives can be
        ``lambda f, x: jax.jacobian(jax.jacobian(f))(x)`` or
        ``lambda f, x: jax.hessian(f)(x) + 1e-3 * jnp.eye(x.shape[0])``. The later
        example is helpful when the hessian of `f` at `x` is not positive definite.
        Note that the output hessian is the precision matrix of the laplace
        approximation.
    """

    def __init__(
        self,
        model,
        *,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        create_plates=None,
        hessian_fn=None,
    ):
        super().__init__(
            model, prefix=prefix, init_loc_fn=init_loc_fn, create_plates=create_plates
        )
        self._hessian_fn = (
            hessian_fn if hessian_fn is not None else (lambda f, x: hessian(f)(x))
        )

    def _setup_prototype(self, *args, **kwargs):
        super(AutoLaplaceApproximation, self)._setup_prototype(*args, **kwargs)

        def loss_fn(params):
            # we are doing maximum likelihood, so only require `num_particles=1` and an arbitrary rng_key.
            return Trace_ELBO().loss(
                random.PRNGKey(0), params, self.model, self, *args, **kwargs
            )

        self._loss_fn = loss_fn

    def _get_posterior(self, *args, **kwargs):
        # sample from Delta guide
        loc = numpyro.param("{}_loc".format(self.prefix), self._init_latent)
        return dist.Delta(loc, event_dim=1)

    def get_base_dist(self):
        return dist.Normal(jnp.zeros(self.latent_dim), 1).to_event(1)

    def get_transform(self, params):
        def loss_fn(z):
            params1 = params.copy()
            params1["{}_loc".format(self.prefix)] = z
            return self._loss_fn(params1)

        loc = params["{}_loc".format(self.prefix)]
        precision = self._hessian_fn(loss_fn, loc)
        scale_tril = cholesky_of_inverse(precision)
        if not_jax_tracer(scale_tril):
            if np.any(np.isnan(scale_tril)):
                warnings.warn(
                    "Hessian of log posterior at the MAP point is singular. Posterior"
                    " samples from AutoLaplaceApproxmiation will be constant (equal to"
                    " the MAP point). Please consider using an AutoNormal guide.",
                    stacklevel=find_stack_level(),
                )
        scale_tril = jnp.where(jnp.isnan(scale_tril), 0.0, scale_tril)
        return LowerCholeskyAffine(loc, scale_tril)

    def get_posterior(self, params):
        """
        Returns a multivariate Normal posterior distribution.
        """
        transform = self.get_transform(params)
        return dist.MultivariateNormal(transform.loc, scale_tril=transform.scale_tril)

    def sample_posterior(self, rng_key, params, sample_shape=()):
        latent_sample = self.get_posterior(params).sample(rng_key, sample_shape)
        return self._unpack_and_constrain(latent_sample, params)

    def median(self, params):
        loc = params["{}_loc".format(self.prefix)]
        return self._unpack_and_constrain(loc, params)

    def quantiles(self, params, quantiles):
        transform = self.get_transform(params)
        quantiles = jnp.array(quantiles)[..., None]
        latent = dist.Normal(
            transform.loc, jnp.norm(transform.scale_tril, axis=-1)
        ).icdf(quantiles)
        return self._unpack_and_constrain(latent, params)


class AutoIAFNormal(AutoContinuous):
    """
    This implementation of :class:`AutoContinuous` uses a Diagonal Normal
    distribution transformed via a
    :class:`~numpyro.distributions.flows.InverseAutoregressiveTransform`
    to construct a guide over the entire latent space. The guide does not
    depend on the model's ``*args, **kwargs``.

    Usage::

        guide = AutoIAFNormal(model, hidden_dims=[20], skip_connections=True, ...)
        svi = SVI(model, guide, ...)

    :param callable model: a generative model.
    :param str prefix: a prefix that will be prefixed to all param internal sites.
    :param callable init_loc_fn: A per-site initialization function.
    :param int num_flows: the number of flows to be used, defaults to 3.
    :param list hidden_dims: the dimensionality of the hidden units per layer.
        Defaults to ``[latent_dim, latent_dim]``.
    :param bool skip_connections: whether to add skip connections from the input to the
        output of each flow. Defaults to False.
    :param callable nonlinearity: the nonlinearity to use in the feedforward network.
        Defaults to :func:`jax.example_libraries.stax.Elu`.
    """

    def __init__(
        self,
        model,
        *,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        num_flows=3,
        hidden_dims=None,
        skip_connections=False,
        nonlinearity=stax.Elu,
    ):
        self.num_flows = num_flows
        # 2-layer, stax.Elu, skip_connections=False by default following the experiments in
        # IAF paper (https://arxiv.org/abs/1606.04934)
        # and Neutra paper (https://arxiv.org/abs/1903.03704)
        self._hidden_dims = hidden_dims
        self._skip_connections = skip_connections
        self._nonlinearity = nonlinearity
        super(AutoIAFNormal, self).__init__(
            model, prefix=prefix, init_loc_fn=init_loc_fn
        )

    def _get_posterior(self):
        if self.latent_dim == 1:
            raise ValueError(
                "latent dim = 1. Consider using AutoDiagonalNormal instead"
            )
        hidden_dims = (
            [self.latent_dim, self.latent_dim]
            if self._hidden_dims is None
            else self._hidden_dims
        )
        flows = []
        for i in range(self.num_flows):
            if i > 0:
                flows.append(PermuteTransform(jnp.arange(self.latent_dim)[::-1]))
            arn = AutoregressiveNN(
                self.latent_dim,
                hidden_dims,
                permutation=jnp.arange(self.latent_dim),
                skip_connections=self._skip_connections,
                nonlinearity=self._nonlinearity,
            )
            arnn = numpyro.module(
                "{}_arn__{}".format(self.prefix, i), arn, (self.latent_dim,)
            )
            flows.append(InverseAutoregressiveTransform(arnn))
        return dist.TransformedDistribution(self.get_base_dist(), flows)

    def get_base_dist(self):
        return dist.Normal(jnp.zeros(self.latent_dim), 1).to_event(1)


class AutoBNAFNormal(AutoContinuous):
    """
    This implementation of :class:`AutoContinuous` uses a Diagonal Normal
    distribution transformed via a
    :class:`~numpyro.distributions.flows.BlockNeuralAutoregressiveTransform`
    to construct a guide over the entire latent space. The guide does not
    depend on the model's ``*args, **kwargs``.

    Usage::

        guide = AutoBNAFNormal(model, num_flows=1, hidden_factors=[50, 50], ...)
        svi = SVI(model, guide, ...)

    **References**

    1. *Block Neural Autoregressive Flow*,
       Nicola De Cao, Ivan Titov, Wilker Aziz

    :param callable model: a generative model.
    :param str prefix: a prefix that will be prefixed to all param internal sites.
    :param callable init_loc_fn: A per-site initialization function.
    :param int num_flows: the number of flows to be used, defaults to 1.
    :param list hidden_factors: Hidden layer i has ``hidden_factors[i]`` hidden units per
        input dimension. This corresponds to both :math:`a` and :math:`b` in reference [1].
        The elements of hidden_factors must be integers.
    """

    def __init__(
        self,
        model,
        *,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        num_flows=1,
        hidden_factors=[8, 8],
    ):
        self.num_flows = num_flows
        self._hidden_factors = hidden_factors
        super(AutoBNAFNormal, self).__init__(
            model, prefix=prefix, init_loc_fn=init_loc_fn
        )

    def _get_posterior(self):
        if self.latent_dim == 1:
            raise ValueError(
                "latent dim = 1. Consider using AutoDiagonalNormal instead"
            )
        flows = []
        for i in range(self.num_flows):
            if i > 0:
                flows.append(PermuteTransform(jnp.arange(self.latent_dim)[::-1]))
            residual = "gated" if i < (self.num_flows - 1) else None
            arn = BlockNeuralAutoregressiveNN(
                self.latent_dim, self._hidden_factors, residual
            )
            arnn = numpyro.module(
                "{}_arn__{}".format(self.prefix, i), arn, (self.latent_dim,)
            )
            flows.append(BlockNeuralAutoregressiveTransform(arnn))
        return dist.TransformedDistribution(self.get_base_dist(), flows)

    def get_base_dist(self):
        return dist.Normal(jnp.zeros(self.latent_dim), 1).to_event(1)


TempAdaptState = namedtuple("TempAdaptState", ["temperature", "da_state", "window_idx"])


def temperature_adapter(
    init_temperature,
    num_adapt_steps=float("inf"),
    # find_reasonable_temperature=None,
    target_accept_prob=0.33,
):
    """
    A scheme to adapt tunable temperature during the warmup phase of HMC.

    :param int num_adapt_steps: Number of warmup steps.
    :param find_reasonable_temperature: A callable to find a reasonable temperature
        at the beginning of each adaptation window.
    :param float target_accept_prob: Target acceptance probability for temperature
        adaptation using Dual Averaging. Increasing this value will lead to a smaller
        temperature. Default to 0.33.
    :return: a pair of (`init_fn`, `update_fn`).
    """
    da_init, da_update = dual_averaging()
    init_window_size = 25

    def init_fn(temperature=-1.0):
        """
        :param float temperature: Initial temperature.
        :return: initial state of the adapt scheme.
        """
        da_state = da_init(-temperature)
        window_idx = jnp.array(0, dtype=jnp.result_type(int))
        return TempAdaptState(temperature, da_state, window_idx)

    def _update_at_window_end(state):
        temperature, da_state, window_idx = state
        da_state = da_init(-temperature)
        return TempAdaptState(temperature, da_state, window_idx + 1)

    def update_fn(t, accept_prob, state):
        """
        :param int t: The current time step.
        :param float accept_prob: Acceptance probability of the current time step.
        :param state: Current state of the adapt scheme.
        :return: new state of the adapt scheme.
        """
        temperature, da_state, window_idx = state
        da_state = da_update(target_accept_prob - accept_prob, da_state)
        # note: at the end of warmup phase, use average of log temperature
        neg_temperature, neg_temperature_avg, *_ = da_state
        temperature = jnp.where(
            t == (num_adapt_steps - 1), -neg_temperature_avg, -neg_temperature
        )
        t_at_window_end = t == (init_window_size * (2 ** (window_idx + 1) - 1) - 1)
        window_idx = jnp.where(t_at_window_end, window_idx + 1, window_idx)
        da_state = jax.lax.cond(
            t_at_window_end, -temperature, da_init, da_state, lambda x: x
        )
        return TempAdaptState(temperature, da_state, window_idx)

    return init_fn(init_temperature), update_fn


class AutoRVRS(AutoContinuous):
    """ """

    def __init__(
        self,
        model,
        *,
        S=4,  # number of samples
        T=0.0,
        T_lr=1.0,
        adaptation_scheme="Z_target",
        epsilon=0.1,
        guide=None,
        prefix="auto",
        init_loc_fn=init_to_uniform,
        init_scale=1.0,
        Z_target=0.33,
        T_exponent=None,
        gamma=0.99,  # controls momentum (0.0 => no momentum)
        num_warmup=float("inf"),
        include_log_Z=True,
        reparameterized=True,
        T_lr_drop=None,
    ):
        if S < 1:
            raise ValueError("S must satisfy S >= 1 (got S = {})".format(S))
        if init_scale <= 0.0:
            raise ValueError("init_scale must be positive.")
        # NOTE: removed because not jittable
        # if T is not None and not isinstance(T, float):
        #    raise ValueError("T must be None or a float.")
        if adaptation_scheme not in ["fixed", "Z_target", "dual_averaging"]:
            raise ValueError(
                "adaptation_scheme must be one of 'fixed', 'Z_target', or 'dual_averaging'."
            )

        self.S = S
        self.T = T
        self.epsilon = epsilon
        self.lambd = epsilon / (1 - epsilon)
        self.gamma = gamma
        self.include_log_Z = include_log_Z
        self.reparameterized = reparameterized
        self.T_lr_drop = T_lr_drop

        if guide is not None:
            if not isinstance(guide, AutoContinuous):
                raise ValueError("We only support AutoContinuous guide in AutoRVRS.")
            self.guide = guide
        else:
            self.guide = AutoDiagonalNormal(
                model, init_loc_fn=init_loc_fn, init_scale=init_scale, prefix=prefix
            )

        self.adaptation_scheme = adaptation_scheme
        self.T_lr = T_lr
        self.T_exponent = T_exponent
        self.Z_target = Z_target
        self.num_warmup = num_warmup
        super().__init__(model, prefix=prefix, init_loc_fn=init_loc_fn)

    def _setup_prototype(self, *args, **kwargs):
        super()._setup_prototype(*args, **kwargs)

        for name, site in self.prototype_trace.items():
            if (
                site["type"] == "plate"
                and isinstance(site["args"][1], int)
                and site["args"][0] > site["args"][1]
            ):
                raise NotImplementedError(
                    "AutoRVRS cannot be used in conjunction with data subsampling."
                )
        with handlers.block(), handlers.trace() as tr, handlers.seed(rng_seed=0):
            self.guide(*args, **kwargs)
        self.prototype_guide_trace = tr
        if self.adaptation_scheme == "dual_averaging":
            self._da_init_state, self._da_update = temperature_adapter(
                self.T, self.num_warmup, self.Z_target
            )

    def _get_posterior(self):
        raise NotImplementedError

    def _sample_latent(self, *args, **kwargs):
        model_params = {}
        for name, site in self.prototype_trace.items():
            if site["type"] == "param":
                model_params[name] = numpyro.param(
                    name,
                    site["value"],
                    constraint=site["kwargs"].pop("constraint", constraints.real),
                )
        guide_params = {}
        for name, site in self.prototype_guide_trace.items():
            if site["type"] == "param":
                guide_params[name] = numpyro.param(
                    name,
                    site["value"],
                    constraint=site["kwargs"].pop("constraint", constraints.real),
                )
        params = {"guide": guide_params, "model": model_params}

        def guide_sampler(key, params):
            with handlers.block(), handlers.seed(rng_seed=key), handlers.substitute(
                data=params["guide"]
            ):
                z = self.guide._sample_latent(*args, **kwargs)
            return z

        def guide_log_prob(z, params):
            z_and_params = {f"_{self.prefix}_latent": z, **params}
            with handlers.block():
                return log_density(
                    self.guide._sample_latent, args, kwargs, z_and_params
                )[0]

        def model_log_density(x, params):
            x_unpack = self._unpack_latent(x)
            with numpyro.handlers.block(), handlers.substitute(data=params):
                return -self._potential_fn(x_unpack)

        if self.adaptation_scheme == "Z_target":
            T_adapt = numpyro.primitives.mutable(
                "_T_adapt", {"value": jnp.array(self.T)}
            )
            if self.gamma != 0.0:
                T_grad_smoothed = numpyro.primitives.mutable(
                    "_T_grad_smoothed", {"value": jnp.array(0.0)}
                )
            T = T_adapt["value"]
        elif self.adaptation_scheme == "dual_averaging":
            T_adapt = numpyro.primitives.mutable(
                "_T_adapt", {"value": self._da_init_state}
            )
            T = T_adapt["value"].temperature
        else:
            T = self.T

        num_updates = numpyro.primitives.mutable(
            "_num_updates", {"value": jnp.array(0)}
        )

        def accept_log_prob_fn(z, params):
            params = jax.lax.stop_gradient(params)
            guide_lp = guide_log_prob(z, params["guide"])
            lw = model_log_density(z, params["model"]) - guide_lp
            a = sigmoid(lw + T)
            return jnp.log(self.epsilon + (1 - self.epsilon) * a), lw, guide_lp

        keys = random.split(numpyro.prng_key(), self.S)
        zs, log_weight, log_Z, first_log_a, guide_lp = batch_rejection_sampler_custom(
            accept_log_prob_fn, guide_sampler, keys, params
        )
        assert zs.shape == (self.S, self.latent_dim)

        numpyro.deterministic("first_log_a", first_log_a)

        # compute surrogate elbo
        az = sigmoid(log_weight + T)
        log_a_eps_z = jnp.log(self.epsilon + (1 - self.epsilon) * az)
        Az = log_weight - log_sigmoid(log_weight + T)
        A_bar = stop_gradient(Az - Az.mean(0))
        assert az.shape == Az.shape == log_weight.shape == (self.S,)

        S_ratio = self.S / (self.S - 1)

        if self.reparameterized:
            ratio = (self.lambd + jnp.square(az)) / (self.lambd + az)
            ratio_bar = stop_gradient(ratio)
            surrogate = (
                S_ratio * (A_bar * (ratio_bar * log_a_eps_z + ratio)).sum()
                + (ratio_bar * Az).sum()
            )
            # surrogate = S_ratio * (2 * A_bar * az).sum() + (stop_gradient(az) * Az).sum()
        else:
            # recompute for simplicity
            guide_lp = jax.vmap(
                lambda z: guide_log_prob(stop_gradient(z), params["guide"])
            )(zs)
            assert guide_lp.shape == (self.S,)
            surrogate = (
                S_ratio
                * (
                    stop_gradient(A_bar * (self.epsilon + (1 - self.epsilon) * az))
                    * guide_lp
                ).sum()
            )

        if self.include_log_Z:
            elbo_correction = stop_gradient(
                surrogate + guide_lp.sum() + log_a_eps_z.sum() - log_Z * self.S
            )
        else:
            elbo_correction = stop_gradient(
                surrogate + guide_lp.sum() + log_a_eps_z.sum()
            )
        numpyro.factor("surrogate_factor", -surrogate + elbo_correction)

        num_updates["value"] = num_updates["value"] + 1

        if self.adaptation_scheme == "Z_target":
            # minimize (Z - Z_target) ** 2
            a = stop_gradient(jnp.exp(first_log_a))
            a_minus = 1 / (self.S - 1) * (jnp.sum(a) - a)
            T_grad = jnp.mean((a_minus - self.Z_target) * a * (1 - a))

            if self.gamma != 0.0:
                T_grad_smoothed["value"] = (
                    self.gamma * T_grad_smoothed["value"] + (1.0 - self.gamma) * T_grad
                )
                T_grad = T_grad_smoothed["value"]

            if self.T_exponent is not None:
                T_lr = self.T_lr * jnp.power(num_updates["value"], -self.T_exponent)
            elif self.T_lr_drop is not None:
                T_lr = self.T_lr * 0.2 ** (
                    num_updates["value"] // self.T_lr_drop
                ).astype(float)
            else:
                T_lr = self.T_lr

            T_adapt["value"] = T_adapt["value"] - T_lr * T_grad
        elif self.adaptation_scheme == "dual_averaging":
            # Z = stop_gradient(jnp.exp(log_Z))
            # TODO: use log_a_sum instead of first_log_a?
            a = stop_gradient(jnp.exp(first_log_a))
            a_minus = 1 / (self.S - 1) * (jnp.sum(a) - a)
            T_grad = jnp.mean((a_minus - self.Z_target) * a * (1 - a))
            Z = self.Z_target + T_grad
            t = num_updates["value"]
            T_adapt["value"] = lax.cond(
                t < self.num_warmup,
                (t, Z, T_adapt["value"]),
                lambda args: self._da_update(*args),
                T_adapt["value"],
                lambda x: x,
            )
            # num_updates["value"] = t + 1

        return stop_gradient(zs)

    def __call__(self, *args, **kwargs):
        if self.prototype_trace is None:
            # run model to inspect the model structure
            self._setup_prototype(*args, **kwargs)

        latent = self._sample_latent(*args, **kwargs)

        # unpack continuous latent samples
        result = {}

        for name, unconstrained_value in jax.vmap(self._unpack_latent)(latent).items():
            site = self.prototype_trace[name]
            with helpful_support_errors(site):
                transform = biject_to(site["fn"].support)
            value = jax.vmap(transform)(unconstrained_value)
            event_ndim = site["fn"].event_dim
            if numpyro.get_mask() is False:
                log_density = 0.0
            else:
                log_density = -jax.vmap(transform.log_abs_det_jacobian)(
                    unconstrained_value, value
                )
                log_density = sum_rightmost(
                    log_density, jnp.ndim(log_density) - jnp.ndim(value) + event_ndim
                )
            delta_dist = dist.Delta(
                value, log_density=log_density, event_dim=event_ndim
            )
            result[name] = numpyro.sample(name, delta_dist)

        return result

    def sample_posterior(self, rng_key, params, sample_shape=()):
        def _single_sample(_rng_key):
            latent_sample = handlers.substitute(
                handlers.seed(self._sample_latent, _rng_key), params
            )(sample_shape=())
            return jax.vmap(self._unpack_and_constrain, in_axes=(0, None))(
                latent_sample, params
            )

        if sample_shape:
            rng_key = random.split(rng_key, int(np.prod(sample_shape)))
            samples = lax.map(_single_sample, rng_key)
            return tree_map(
                lambda x: jnp.reshape(x, sample_shape + jnp.shape(x)[1:]),
                samples,
            )
        else:
            return _single_sample(rng_key)


def rejection_sampler(accept_log_prob_fn, guide_sampler, key):
    def cond_fn(val):
        return ~val[-1]

    def body_fn(val):
        key, _, _, log_a_sum, num_samples, first_log_a, _, _ = val
        key_next, key_uniform, key_q = random.split(key, 3)
        z = guide_sampler(key_q)
        accept_log_prob, log_weight, guide_lp = accept_log_prob_fn(z)
        log_u = -random.exponential(key_uniform)
        is_accepted = log_u < accept_log_prob
        first_log_a = select(num_samples == 0, accept_log_prob, first_log_a)
        log_a_sum = logsumexp(jnp.stack([log_a_sum, accept_log_prob]))
        return (
            key_next,
            z,
            log_weight,
            log_a_sum,
            num_samples + 1,
            first_log_a,
            guide_lp,
            is_accepted,
        )

    prototype_z = tree_map(
        lambda x: jnp.zeros(x.shape, dtype=x.dtype), jax.eval_shape(guide_sampler, key)
    )
    init_val = (key, prototype_z, -jnp.inf, -jnp.inf, 0, -jnp.inf, 0, False)
    _, z, log_w, log_a_sum, num_samples, first_log_a, guide_lp, _ = jax.lax.while_loop(
        cond_fn, body_fn, init_val
    )

    return z, log_w, log_a_sum, num_samples, first_log_a, guide_lp


def batch_rejection_sampler(accept_log_prob_fn, guide_sampler, keys, params):
    def sample_and_accept_fn(key, params):
        z = guide_sampler(key, params)
        return z, accept_log_prob_fn(z, params)

    z_init = tree_map(
        lambda x: jnp.zeros(x.shape, dtype=x.dtype),
        jax.eval_shape(guide_sampler, keys[0], params),
    )
    return _rs_impl(sample_and_accept_fn, z_init, keys, params)[1:]


def _rs_impl(sample_and_accept_fn, z_init, keys, params):
    assert keys.ndim == 2
    S = keys.shape[0]
    zs_init = tree_map(lambda x: jnp.broadcast_to(x, (S,) + jnp.shape(x)), z_init)
    neg_inf = jnp.full(S, -jnp.inf)
    buffer = (keys, zs_init, neg_inf, neg_inf, jnp.full(S, False))
    init_val = (keys, neg_inf, neg_inf, jnp.array(0), buffer)

    def cond_fn(val):
        is_accepted = val[-1][-1]
        return is_accepted.sum() < S

    def body_fn(key, params):
        key_next, key_uniform, key_q = random.split(key, 3)
        z, (accept_log_prob, log_weight, guide_lp) = sample_and_accept_fn(key_q, params)
        log_u = -random.exponential(key_uniform)
        is_accepted = log_u < accept_log_prob
        return key_next, accept_log_prob, (key_q, z, log_weight, guide_lp, is_accepted)

    def batch_body_fn(val):
        keys, log_a_sum, first_log_a, num_samples, buffer = val
        keys_next, accept_log_prob, candidate = jax.vmap(body_fn, in_axes=(0, None))(
            keys, params
        )
        log_a_sum = logsumexp(jnp.stack([log_a_sum, accept_log_prob], axis=0), axis=0)
        buffer_extend = tree_map(
            lambda a, b: jnp.concatenate([a, b]), candidate, buffer
        )
        is_accepted = buffer_extend[-1]
        maybe_accept_indices = jnp.argsort(is_accepted)[-S:]
        new_buffer = tree_map(lambda x: x[maybe_accept_indices], buffer_extend)
        first_log_a = select(num_samples == 0, accept_log_prob, first_log_a)
        return keys_next, log_a_sum, first_log_a, num_samples + 1, new_buffer

    _, log_a_sum, first_log_a, num_samples, buffer = jax.lax.while_loop(
        cond_fn, batch_body_fn, init_val
    )
    key_q, z, log_w, guide_lp, _ = buffer
    log_Z = logsumexp(log_a_sum) - jnp.log(num_samples * S)
    return key_q, z, log_w, log_Z, first_log_a, guide_lp


def batch_rejection_sampler_custom(accept_log_prob_fn, guide_sampler, keys, params):
    def sample_and_accept_fn(key, params):
        z = guide_sampler(key, params)
        return z, accept_log_prob_fn(z, params)

    z_init = tree_map(
        lambda x: jnp.zeros(x.shape, dtype=x.dtype),
        jax.eval_shape(guide_sampler, keys[0], params),
    )
    return _rs_custom_impl(sample_and_accept_fn, z_init, keys, params)[1:]


@partial(jax.custom_vjp, nondiff_argnums=(0,))
def _rs_custom_impl(sample_and_accept_fn, z_init, keys, params):
    return _rs_impl(sample_and_accept_fn, z_init, keys, params)


def _rs_fwd(sample_and_accept_fn, z_init, keys, params):
    out = _rs_custom_impl(sample_and_accept_fn, z_init, keys, params)
    key_q = out[0]
    return out, (key_q, params)


def _rs_bwd(sample_and_accept_fn, res, g):
    key_q, params = res
    _, z_grads, lw_grads, *_ = g

    def get_z_and_lw(key, params):
        z, (_, lw, _) = sample_and_accept_fn(key, params)
        return z, lw

    def sample_grad(key, z_grad, lw_grad):
        _, guide_vjp = jax.vjp(partial(get_z_and_lw, key), params)
        return guide_vjp((z_grad, lw_grad))[0]

    batch_params_grad = jax.vmap(sample_grad)(key_q, z_grads, lw_grads)
    params_grad = jax.tree_util.tree_map(lambda x: x.sum(0), batch_params_grad)
    return (None, None, params_grad)


_rs_custom_impl.defvjp(_rs_fwd, _rs_bwd)


class AutoSemiRVRS(AutoGuide):
    """
    This implementation of :class:`AutoSemiRVRS` [1] combines a parametric variational
    distribution over global latent variables with RVRS to infer local latent variables.
    Unlike :class:`AutoRVRS` this guide can be used in conjunction with data subsampling.

    Usage::

        def global_model():
            return numpyro.sample("theta", dist.Normal(0, 1))

        def local_model(theta):
            with numpyro.plate("data", 8, subsample_size=2):
                tau = numpyro.sample("tau", dist.Gamma(5.0, 5.0))
                numpyro.sample("obs", dist.Normal(0.0, tau), obs=jnp.ones(2))

        global_guide = AutoNormal(global_model)
        local_guide = AutoNormal(local_model)
        model = lambda: local_model(global_model())
        guide = AutoSemiRVRS(model, local_model, global_guide, local_guide)
        svi = SVI(model, guide, ...)
        # sample posterior for particular data subset {3, 7}
        with handlers.substitute(data={"data": jnp.array([3, 7])}):
            samples = guide.sample_posterior(random.PRNGKey(1), params)

    :param callable model: A NumPyro model with global and local latent variables.
    :param callable global_guide: A guide for the global latent variables, e.g. an autoguide.
        The return type should be a dictionary of latent sample sites names and corresponding samples.
    :param callable local_guide: An auto guide for the local latent variables.
    :param str prefix: A prefix that will be prefixed to all internal sites.
    """

    def __init__(
        self,
        model,
        local_model,
        global_guide,
        local_guide,
        *,
        prefix="auto",
        S=4,
        T=0.0,
        T_lr=1.0,
        adaptation_scheme="Z_target",
        epsilon=0.1,
        init_loc_fn=init_to_uniform,
        Z_target=0.33,
        T_exponent=None,
        gamma=0.99,  # controls momentum (0.0 => no momentum)
        num_warmup=float("inf"),
        include_log_Z=True,
        reparameterized=True,
        T_lr_drop=None,
    ):
        if S < 1:
            raise ValueError("S must satisfy S >= 1 (got S = {})".format(S))
        # if T is not None and not isinstance(T, float):
        #     raise ValueError("T must be None or a float.")
        if adaptation_scheme not in ["fixed", "Z_target", "dual_averaging"]:
            raise ValueError(
                "adaptation_scheme must be one of 'fixed', 'Z_target', or 'dual_averaging'."
            )

        self.local_model = local_model
        self.global_guide = global_guide
        self.local_guide = local_guide
        self.S = S
        self.T = T
        self.epsilon = epsilon
        self.lambd = epsilon / (1 - epsilon)
        self.gamma = gamma
        self.include_log_Z = include_log_Z
        self.reparameterized = reparameterized
        self.T_lr_drop = T_lr_drop
        self.adaptation_scheme = adaptation_scheme
        self.T_lr = T_lr
        self.T_exponent = T_exponent
        self.Z_target = Z_target
        self.num_warmup = num_warmup
        super().__init__(model, prefix=prefix, init_loc_fn=init_loc_fn)

    def _setup_prototype(self, *args, **kwargs):
        super()._setup_prototype(*args, **kwargs)
        assert isinstance(self.prototype_trace, dict)
        # extract global/local/local_dim/plates
        subsample_plates = {
            name: site
            for name, site in self.prototype_trace.items()
            if site["type"] == "plate"
            and isinstance(site["args"][1], int)
            and site["args"][0] > site["args"][1]
        }
        num_plates = len(subsample_plates)
        assert (
            num_plates == 1
        ), f"AutoSemiRVRS assumes that the model contains exactly 1 plate with data subsampling but got {num_plates}."
        plate_name = list(subsample_plates.keys())[0]
        local_vars = []
        subsample_axes = {}
        plate_dim = None
        for name, site in self.prototype_trace.items():
            if site["type"] == "sample" and not site["is_observed"]:
                for frame in site["cond_indep_stack"]:
                    if frame.name == plate_name:
                        if plate_dim is None:
                            plate_dim = frame.dim
                        local_vars.append(name)
                        subsample_axes[name] = plate_dim - site["fn"].event_dim
                        break
        if len(local_vars) == 0:
            raise RuntimeError(
                "There are no local variables in the `{plate_name}` plate."
                " AutoSemiDAIS is appropriate for models with local variables."
            )

        local_init_locs = {
            name: site["value"]
            for name, site in self.prototype_trace.items()
            if name in local_vars
        }

        one_sample = {
            k: jnp.take(v, 0, axis=subsample_axes[k])
            for k, v in local_init_locs.items()
        }
        _, shape_dict = _ravel_dict(one_sample)
        self._pack_local_latent = jax.vmap(
            lambda x: _ravel_dict(x)[0], in_axes=(subsample_axes,)
        )
        local_init_latent = self._pack_local_latent(local_init_locs)
        unpack_latent = partial(_unravel_dict, shape_dict=shape_dict)
        # this is to match the behavior of Pyro, where we can apply
        # unpack_latent for a batch of samples
        self._unpack_local_latent = jax.vmap(
            UnpackTransform(unpack_latent), out_axes=subsample_axes
        )
        plate_full_size, plate_subsample_size = subsample_plates[plate_name]["args"]
        self._local_latent_dim = jnp.size(local_init_latent) // plate_subsample_size
        self._local_plate = (plate_name, plate_full_size, plate_subsample_size)

        if self.global_guide is not None:
            with handlers.block(), handlers.trace() as tr, handlers.seed(rng_seed=0):
                self.global_guide(*args, **kwargs)
            self.prototype_global_guide_trace = tr

            with handlers.block(), handlers.seed(rng_seed=0):
                local_args = (self.global_guide.model(*args, **kwargs),)
                local_kwargs = {}
        else:
            local_args = args
            local_kwargs = kwargs

        with handlers.block(), handlers.trace() as tr, handlers.seed(rng_seed=0):
            self.local_guide(*local_args, **local_kwargs)
        self.prototype_local_guide_trace = tr

        with handlers.block(), handlers.trace() as tr, handlers.seed(rng_seed=0):
            self.local_model(*local_args, **local_kwargs)
        self.prototype_local_model_trace = tr

        if self.adaptation_scheme == "dual_averaging":
            self._da_init_state, self._da_update = temperature_adapter(
                self.T, self.num_warmup, self.Z_target
            )

    def __call__(self, *args, **kwargs):
        if self.prototype_trace is None:
            # run model to inspect the model structure
            self._setup_prototype(*args, **kwargs)
        assert isinstance(self.prototype_trace, dict)

        global_latents, local_latent_flat = self._sample_latent(*args, **kwargs)

        # unpack continuous latent samples
        result = {}
        for name, value in global_latents.items():
            site = self.prototype_trace[name]
            event_ndim = site["fn"].event_dim
            delta_dist = dist.Delta(value, log_density=0.0, event_dim=event_ndim)
            result[name] = numpyro.sample(name, delta_dist)

        for name, value in jax.vmap(self._unpack_local_latent)(
            local_latent_flat
        ).items():
            site = self.prototype_trace[name]
            event_ndim = site["fn"].event_dim
            # Note: "surrogate_factor"'s guide_lp is log density in constrained space.
            delta_dist = dist.Delta(value, log_density=0.0, event_dim=event_ndim)
            result[name] = numpyro.sample(name, delta_dist)

        return result

    def _get_posterior(self):
        raise NotImplementedError

    def _sample_latent(self, *args, **kwargs):
        kwargs.pop("sample_shape", ())
        plate_name, N, M = self._local_plate
        subsample_plate = numpyro.plate(plate_name, N, subsample_size=M)
        subsample_idx = subsample_plate._indices
        M = subsample_idx.shape[0]

        model_params = {}
        assert isinstance(self.prototype_trace, dict)
        for name, site in self.prototype_trace.items():
            if site["type"] == "param":
                model_params[name] = numpyro.param(
                    name, site["value"], **site["kwargs"]
                )

        global_key = numpyro.prng_key()
        global_guide_params = {}
        global_lp = 0.0
        if self.global_guide is not None:
            for name, site in self.prototype_global_guide_trace.items():
                if site["type"] == "param":
                    global_guide_params[name] = numpyro.param(
                        name, site["value"], **site["kwargs"]
                    )
            with handlers.block(), handlers.trace() as tr, handlers.seed(
                rng_seed=global_key
            ), handlers.substitute(data=global_guide_params):
                self.global_guide(*args, **kwargs)
            global_latents = {
                name: site["value"]
                for name, site in tr.items()
                if site["type"] == "sample" and not site.get("is_observed", False)
            }
            for name, site in tr.items():
                if name in global_latents:
                    global_lp = global_lp + site["fn"].log_prob(site["value"]).sum()

            rng_key = numpyro.prng_key()
            with handlers.block(), handlers.seed(rng_seed=rng_key), handlers.substitute(
                data=dict(**global_latents, **model_params)
            ):
                local_args = (
                    jax.lax.stop_gradient(self.global_guide.model(*args, **kwargs)),
                )
                local_kwargs = {}
        else:
            local_args = args
            local_kwargs = kwargs
            global_latents = {}

        assert isinstance(self.prototype_local_guide_trace, dict)
        local_guide_params = {}
        for name, site in self.prototype_local_guide_trace.items():
            if site["type"] == "param":
                local_guide_params[name] = numpyro.param(
                    name, site["value"], **site["kwargs"]
                )

        subsample_model = partial(_subsample_model, self.local_model)
        subsample_guide = partial(_subsample_model, self.local_guide)

        def single_local_model_log_density(z, subsample_idx):
            latent = self._unpack_local_latent(z)
            with handlers.block():
                # Scale down potential_fn by N because potential_fn scales up the log_prob.
                # We skip the warning because we are using subsample_idx with size 1 for a
                # plate with subsample_size M.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    kwargs = {"_subsample_idx": {plate_name: subsample_idx}}
                    scale = N / subsample_idx.shape[0]
                    kwargs.update(local_kwargs)
                    latent_and_params = dict(
                        **latent, **jax.lax.stop_gradient(model_params)
                    )
                    return (
                        log_density(
                            subsample_model, local_args, kwargs, latent_and_params
                        )[0]
                        / scale
                    )

        def local_model_log_density(z, subsample_idx):
            # shape: local_latent_flat -> (M,) | subsample_idx -> (M,) | out -> (M,)
            return jax.vmap(single_local_model_log_density)(
                jnp.expand_dims(z, 1), jnp.expand_dims(subsample_idx, 1)
            )

        def single_local_guide_sampler(subsample_idx, key, params):
            with handlers.block(), handlers.trace() as tr, handlers.seed(
                rng_seed=key
            ), handlers.substitute(data=params):
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    kwargs = {"_subsample_idx": {plate_name: subsample_idx}}
                    kwargs.update(local_kwargs)
                    subsample_guide(*local_args, **kwargs)
            latent = {
                name: site["value"]
                for name, site in tr.items()
                if site["type"] == "sample" and not site.get("is_observed", False)
            }
            z = self._pack_local_latent(latent)
            return z  # flatten array in constrained space

        def local_guide_sampler(subsample_idx, key, params):
            # shape: params -> (N,) | key -> (M,) | subsample_idx -> (M,) | out -> (M,)
            return jax.vmap(single_local_guide_sampler, (0, 0, None))(
                jnp.expand_dims(subsample_idx, 1), key, params
            )[:, 0]

        def single_local_guide_log_density(z, subsample_idx, params):
            latent = self._unpack_local_latent(z)
            assert isinstance(latent, dict)
            with handlers.block():
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    kwargs = {"_subsample_idx": {plate_name: subsample_idx}}
                    scale = N / subsample_idx.shape[0]
                    kwargs.update(local_kwargs)
                    return (
                        log_density(
                            subsample_guide, local_args, kwargs, {**params, **latent}
                        )[0]
                        / scale
                    )

        def local_guide_log_density(z, subsample_idx, params):
            # shape: params -> (N,) | z -> (M,) | subsample_idx -> (M,) | out -> (M,)
            return jax.vmap(single_local_guide_log_density, (0, 0, None))(
                jnp.expand_dims(z, 1), jnp.expand_dims(subsample_idx, 1), params
            )

        if self.adaptation_scheme == "Z_target":
            T_adapt = numpyro.primitives.mutable(
                "_T_adapt", {"value": jnp.full(N, self.T)}
            )
            if self.gamma != 0.0:
                T_grad_smoothed = numpyro.primitives.mutable(
                    "_T_grad_smoothed", {"value": jnp.full(N, 0.0)}
                )
            num_updates = numpyro.primitives.mutable(
                "_num_updates", {"value": jnp.full(N, 0)}
            )
            T = T_adapt["value"]
        elif self.adaptation_scheme == "dual_averaging":
            init_value = tree_map(
                lambda x: jnp.broadcast_to(x, (N,) + jnp.shape(x)), self._da_init_state
            )
            T_adapt = numpyro.primitives.mutable("_T_adapt", {"value": init_value})
            num_updates = numpyro.primitives.mutable(
                "_num_updates", {"value": jnp.full(N, 0)}
            )
            T = T_adapt["value"].temperature
        else:
            T = jnp.full(N, self.T)

        def accept_log_prob_fn(z, subsample_idx, params):
            # shape: z -> (M,) | params -> (N,) | subsample_idx -> (M,) | out -> (M,)
            params = jax.lax.stop_gradient(params)
            guide_lp = local_guide_log_density(z, subsample_idx, params)
            lw = local_model_log_density(z, subsample_idx) - guide_lp
            a = sigmoid(lw + T[subsample_idx])
            return jnp.log(self.epsilon + (1 - self.epsilon) * a), lw, guide_lp

        rs_key, resample_key = random.split(numpyro.prng_key())
        keys = random.split(rs_key, self.S)
        zs, log_weight, log_Z, first_log_a, guide_lp = rs_local(
            accept_log_prob_fn,
            local_guide_sampler,
            keys,
            resample_key,
            local_guide_params,
            subsample_idx,
        )
        assert T.shape == (N,)
        assert zs.shape == (self.S, M, self._local_latent_dim)
        assert log_weight.shape == (self.S, M)
        assert log_Z.shape == (M,)
        assert first_log_a.shape == (self.S, M)
        assert guide_lp.shape == (self.S, M)

        numpyro.deterministic("first_log_a", first_log_a)

        # compute surrogate elbo
        log_weight_T = log_weight + T[subsample_idx]
        az = sigmoid(log_weight_T)
        log_a_eps_z = jnp.log(self.epsilon + (1 - self.epsilon) * az)
        Az = log_weight - log_sigmoid(log_weight_T)
        A_bar = stop_gradient(Az - Az.mean(0))
        assert az.shape == Az.shape == log_weight.shape == (self.S, M)

        assert self.reparameterized
        ratio = (self.lambd + jnp.square(az)) / (self.lambd + az)
        ratio_bar = stop_gradient(ratio)
        surrogate = (
            self.S / (self.S - 1) * (A_bar * (ratio_bar * log_a_eps_z + ratio)).sum()
            + (ratio_bar * Az).sum()
        )

        if self.include_log_Z:
            elbo_correction = stop_gradient(
                surrogate + guide_lp.sum() + log_a_eps_z.sum() - log_Z.sum() * self.S
            )
        else:
            elbo_correction = stop_gradient(
                surrogate + guide_lp.sum() + log_a_eps_z.sum()
            )
        # Scale the factor by subsample factor N / M
        factor = (-surrogate + elbo_correction) * N / M + global_lp * self.S
        numpyro.factor("surrogate_factor", factor)

        if self.adaptation_scheme == "Z_target":
            # minimize (Z - Z_target) ** 2
            a = stop_gradient(jnp.exp(first_log_a))
            a_minus = 1 / (self.S - 1) * (a.sum(0) - a)
            T_grad = jnp.mean((a_minus - self.Z_target) * a * (1 - a), axis=0)
            assert T_grad.shape == (M,)

            num_updates["value"] = (
                num_updates["value"]
                .at[subsample_idx]
                .set(num_updates["value"][subsample_idx] + 1)
            )
            if self.gamma != 0.0:
                T_grad = (
                    self.gamma * T_grad_smoothed["value"][subsample_idx]
                    + (1.0 - self.gamma) * T_grad
                )
                T_grad_smoothed["value"] = (
                    T_grad_smoothed["value"].at[subsample_idx].set(T_grad)
                )

            if self.T_exponent is not None:
                T_lr = self.T_lr * jnp.power(
                    num_updates["value"][subsample_idx], -self.T_exponent
                )
            elif self.T_lr_drop is not None:
                T_lr = self.T_lr * 0.1 ** (
                    num_updates["value"][subsample_idx] // self.T_lr_drop
                ).astype(jnp.result_type(float))
            else:
                T_lr = self.T_lr

            T_adapt["value"] = (
                T_adapt["value"]
                .at[subsample_idx]
                .set(T_adapt["value"][subsample_idx] - T_lr * T_grad)
            )
        elif self.adaptation_scheme == "dual_averaging":
            a = stop_gradient(jnp.exp(first_log_a))
            a_minus = 1 / (self.S - 1) * (a.sum(0) - a)
            T_grad = jnp.mean((a_minus - self.Z_target) * a * (1 - a), axis=0)
            Z = self.Z_target + T_grad
            t = num_updates["value"][subsample_idx]
            T_adapt_old = tree_map(lambda x: x[subsample_idx], T_adapt["value"])
            assert self.num_warmup == float("inf")
            T_adapt_new = jax.vmap(self._da_update)(t, Z, T_adapt_old)
            T_adapt["value"] = tree_map(
                lambda x, x_new: x.at[subsample_idx].set(x_new),
                T_adapt["value"],
                T_adapt_new,
            )
            num_updates["value"] = num_updates["value"].at[subsample_idx].set(t + 1)

        global_latents = tree_map(
            lambda x: jnp.broadcast_to(x, (self.S,) + x.shape), global_latents
        )
        return global_latents, stop_gradient(zs)

    def sample_posterior(self, rng_key, params, *args, sample_shape=(), **kwargs):
        def _single_sample(_rng_key):
            with handlers.trace() as tr:
                global_latents, local_flat = handlers.substitute(
                    handlers.seed(self._sample_latent, _rng_key), params
                )(*args, **kwargs)
            deterministics = {
                name: site["value"]
                for name, site in tr.items()
                if site["type"] == "deterministic"
            }

            def unpack(global_latents, local_flat):
                local_latents = self._unpack_local_latent(local_flat)
                return {**global_latents, **local_latents}

            return dict(
                **deterministics, **jax.vmap(unpack)(global_latents, local_flat)
            )

        if sample_shape:
            rng_key = random.split(rng_key, int(np.prod(sample_shape)))
            samples = lax.map(_single_sample, rng_key)
            return tree_map(
                lambda x: jnp.reshape(x, sample_shape + jnp.shape(x)[1:]), samples
            )
        else:
            return _single_sample(rng_key)


def get_systematic_resampling_indices(weights, rng_key, num_samples):
    """Gets resampling indices based on systematic resampling."""
    n = weights.shape[0]
    cummulative_weight = weights.cumsum(axis=0)
    cummulative_weight = cummulative_weight / cummulative_weight[-1]
    cummulative_weight = cummulative_weight.reshape((n, -1)).swapaxes(0, 1)
    m = cummulative_weight.shape[0]
    uniform = jax.random.uniform(rng_key, (m,))
    positions = (uniform[:, None] + np.arange(num_samples)) / num_samples
    shift = jnp.arange(m)[:, None]
    cummulative_weight = (cummulative_weight + 2 * shift).reshape(-1)
    positions = (positions + 2 * shift).reshape(-1)
    index = cummulative_weight.searchsorted(positions)
    index = (index.reshape(m, num_samples) - n * shift).swapaxes(0, 1)
    return index.reshape((num_samples,) + weights.shape[1:])


def rs_local(
    accept_log_prob_fn, guide_sampler, keys, resample_key, params, subsample_idx
):
    def sample_and_accept_fn(subsample_idx, key, params):
        z = guide_sampler(subsample_idx, key, params)
        return z, accept_log_prob_fn(z, subsample_idx, params)

    M = subsample_idx.shape[0]
    z_init = tree_map(
        lambda x: jnp.zeros(x.shape, dtype=x.dtype),
        jax.eval_shape(guide_sampler, subsample_idx, random.split(keys[0], M), params),
    )
    return _rs_local_custom_impl(
        sample_and_accept_fn, z_init, subsample_idx, keys, resample_key, params
    )[1:]


def _rs_local_impl(
    sample_and_accept_fn, z_init, subsample_idx, keys, resample_key, params
):
    # sample_and_accept_fn(idx, key, param) -> z, (accept_lp, lw, guide_lp)
    assert keys.ndim == 2
    S = keys.shape[0]
    M = subsample_idx.shape[0]
    assert z_init.ndim == 2
    assert z_init.shape[0] == M
    zs_init = tree_map(lambda x: jnp.broadcast_to(x, (S,) + jnp.shape(x)), z_init)
    neg_inf = jnp.full((S, M), -jnp.inf)
    keys = jax.vmap(lambda k: random.split(k, M))(keys)
    buffer = (keys, zs_init, neg_inf, neg_inf, jnp.full((S, M), False))
    init_val = (resample_key, keys, neg_inf, neg_inf, jnp.full(M, 0), buffer)

    def cond_fn(val):
        is_accepted = val[-1][-1]
        return (is_accepted.sum(0) < S).any()

    def body_fn(key, resample_idx):
        key_next, key_uniform, key_q = jax.vmap(
            lambda k: random.split(k, 3), out_axes=1
        )(key)
        z, (accept_log_prob, log_weight, guide_lp) = sample_and_accept_fn(
            subsample_idx[resample_idx], key_q, params
        )
        log_u = -jax.vmap(random.exponential)(key_uniform)
        is_accepted = log_u < accept_log_prob
        return key_next, accept_log_prob, (key_q, z, log_weight, guide_lp, is_accepted)

    def batch_body_fn(val):
        (
            resample_key,
            keys,
            batch_log_a_sum,
            batch_first_log_a,
            batch_num_samples,
            batch_buffer,
        ) = val
        resample_key, resample_subkey = random.split(resample_key)
        # distribute batch-size resource to subsample items
        is_accepted = batch_buffer[-1]
        weights = S - is_accepted.sum(0)
        weights = jnp.where(weights < 0, 0, weights)
        weights = weights / weights.sum(-1, keepdims=True)
        resample_idxs = get_systematic_resampling_indices(weights, resample_subkey, M)
        # resample_idxs = jnp.arange(M)
        assert resample_idxs.shape == (M,)

        keys_next, batch_accept_log_prob, batch_candidate = jax.vmap(
            body_fn, in_axes=(0, None)
        )(keys, resample_idxs)

        def update_idx(i, val):
            batch_log_a_sum, batch_first_log_a, batch_num_samples, batch_buffer = val
            idx = resample_idxs[i]
            accept_log_prob = batch_accept_log_prob[:, i]
            candidate = tree_map(lambda x: x[:, i], batch_candidate)
            buffer = tree_map(lambda x: x[:, idx], batch_buffer)
            log_a_sum = batch_log_a_sum[:, idx]
            first_log_a = batch_first_log_a[:, idx]
            num_samples = batch_num_samples[idx]

            buffer_extend = tree_map(
                lambda a, b: jnp.concatenate([a, b]), candidate, buffer
            )
            is_accepted = buffer_extend[-1]
            maybe_accept_indices = jnp.argsort(is_accepted)[-S:]
            new_buffer = tree_map(lambda x: x[maybe_accept_indices], buffer_extend)
            log_a_sum = logsumexp(
                jnp.stack([log_a_sum, accept_log_prob], axis=0), axis=0
            )
            first_log_a = select(num_samples == 0, accept_log_prob, first_log_a)

            batch_buffer = tree_map(
                lambda x, y: x.at[:, idx].set(y), batch_buffer, new_buffer
            )
            batch_log_a_sum = batch_log_a_sum.at[:, idx].set(log_a_sum)
            batch_first_log_a = batch_first_log_a.at[:, idx].set(first_log_a)
            batch_num_samples = batch_num_samples.at[idx].set(num_samples + 1)
            return batch_log_a_sum, batch_first_log_a, batch_num_samples, batch_buffer

        (
            batch_log_a_sum,
            batch_first_log_a,
            batch_num_samples,
            batch_buffer,
        ) = lax.fori_loop(
            0,
            M,
            update_idx,
            (batch_log_a_sum, batch_first_log_a, batch_num_samples, batch_buffer),
        )
        return (
            resample_key,
            keys_next,
            batch_log_a_sum,
            batch_first_log_a,
            batch_num_samples,
            batch_buffer,
        )

    _, _, log_a_sum, first_log_a, num_samples, buffer = jax.lax.while_loop(
        cond_fn, batch_body_fn, init_val
    )
    key_q, z, log_w, guide_lp, _ = buffer
    log_Z = logsumexp(log_a_sum, axis=0) - jnp.log(num_samples * S)
    return key_q, z, log_w, log_Z, first_log_a, guide_lp


@partial(jax.custom_vjp, nondiff_argnums=(0,))
def _rs_local_custom_impl(
    sample_and_accept_fn, z_init, subsample_idx, keys, resample_key, params
):
    return _rs_local_impl(
        sample_and_accept_fn, z_init, subsample_idx, keys, resample_key, params
    )


def _rs_local_fwd(
    sample_and_accept_fn, z_init, subsample_idx, keys, resample_key, params
):
    out = _rs_local_custom_impl(
        sample_and_accept_fn, z_init, subsample_idx, keys, resample_key, params
    )
    key_q = out[0]
    return out, (subsample_idx, key_q, params)


def _rs_local_bwd(sample_and_accept_fn, res, g):
    subsample_idx, key_q, params = res
    _, z_grads, lw_grads, *_ = g

    def get_z_and_lw(key, params):
        z, (_, lw, _) = sample_and_accept_fn(subsample_idx, key, params)
        return z, lw

    def sample_grad(key, z_grad, lw_grad):
        _, guide_vjp = jax.vjp(partial(get_z_and_lw, key), params)
        return guide_vjp((z_grad, lw_grad))[0]

    batch_params_grad = jax.vmap(sample_grad)(key_q, z_grads, lw_grads)
    params_grad = jax.tree_util.tree_map(lambda x: x.sum(0), batch_params_grad)
    return (None, None, None, None, params_grad)


_rs_local_custom_impl.defvjp(_rs_local_fwd, _rs_local_bwd)

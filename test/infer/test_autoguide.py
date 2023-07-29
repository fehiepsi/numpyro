# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from functools import partial
import warnings

import numpy as np
from numpy.random import RandomState
from numpy.testing import assert_allclose
import pytest

import jax
from jax import jacobian, jit, lax, random
from jax.tree_util import tree_all, tree_map

from numpyro.util import _versiontuple

if _versiontuple(jax.__version__) >= (0, 2, 25):
    from jax.example_libraries.stax import Dense
else:
    from jax.experimental.stax import Dense

import jax.numpy as jnp
from jax.scipy.special import logsumexp
import optax
from optax import piecewise_constant_schedule

import numpyro
from numpyro import handlers, optim
from numpyro.contrib.control_flow import scan
import numpyro.distributions as dist
from numpyro.distributions import constraints, transforms
from numpyro.distributions.flows import InverseAutoregressiveTransform
from numpyro.handlers import substitute
from numpyro.infer import SVI, Trace_ELBO, TraceMeanField_ELBO
from numpyro.infer.autoguide import (
    AutoBNAFNormal,
    AutoDAIS,
    AutoDelta,
    AutoDiagonalNormal,
    AutoIAFNormal,
    AutoLaplaceApproximation,
    AutoLowRankMultivariateNormal,
    AutoMultivariateNormal,
    AutoNormal,
    AutoRVRS,
    AutoSemiDAIS,
    AutoSemiRVRS,
    AutoSurrogateLikelihoodDAIS,
    batch_rejection_sampler,
    batch_rejection_sampler_custom,
)
from numpyro.infer.initialization import (
    init_to_feasible,
    init_to_median,
    init_to_sample,
    init_to_uniform,
    init_to_value,
)
from numpyro.infer.reparam import TransformReparam
from numpyro.infer.util import Predictive
from numpyro.nn.auto_reg_nn import AutoregressiveNN
from numpyro.util import fori_loop

init_strategy = init_to_median(num_samples=2)


@pytest.mark.parametrize(
    "auto_class",
    [
        AutoDiagonalNormal,
        AutoDAIS,
        AutoIAFNormal,
        AutoBNAFNormal,
        AutoMultivariateNormal,
        AutoLaplaceApproximation,
        AutoLowRankMultivariateNormal,
        AutoNormal,
        AutoDelta,
        AutoRVRS,
    ],
)
def test_beta_bernoulli(auto_class):
    numpyro.enable_x64()
    data = jnp.array([[1.0] * 8 + [0.0] * 2, [1.0] * 4 + [0.0] * 6]).T
    N = len(data)

    def model(data):
        f = numpyro.sample("beta", dist.Beta(jnp.ones(2), jnp.ones(2)).to_event())
        with numpyro.plate("N", N):
            numpyro.sample("obs", dist.Bernoulli(f).to_event(1), obs=data)

    adam = optim.Adam(0.001)
    if auto_class == AutoDAIS:
        guide = auto_class(model, init_loc_fn=init_strategy, base_dist="cholesky")
    elif auto_class == AutoRVRS:
        guide = auto_class(
            model,
            S=4,
            T=13.0,
            epsilon=0.1,
            adaptation_scheme="dual_averaging",
            init_loc_fn=init_strategy,
            init_scale=1.0,
        )
    else:
        guide = auto_class(model, init_loc_fn=init_strategy)
    svi = SVI(model, guide, adam, Trace_ELBO())
    svi_state = svi.init(random.PRNGKey(1), data)

    def body_fn(i, val):
        svi_state, loss = svi.update(val, data)
        return svi_state

    svi_state = fori_loop(0, 3000, body_fn, svi_state)
    params = svi.get_params(svi_state)

    true_coefs = (jnp.sum(data, axis=0) + 1) / (data.shape[0] + 2)
    # test .sample_posterior method
    posterior_samples = guide.sample_posterior(
        random.PRNGKey(1), params, sample_shape=(1000,)
    )
    if auto_class == AutoRVRS:
        posterior_samples = jax.tree_util.tree_map(
            lambda x: x.reshape((-1,) + x.shape[2:]), posterior_samples
        )
    posterior_mean = jnp.mean(posterior_samples["beta"], 0)
    posterior_std = jnp.std(posterior_samples["beta"], 0)

    if auto_class == AutoRVRS:
        print(
            "\nFINAL AutoDiag auto_loc: [1.19420906, -0.35535608]\nFINAL AutoDiag auto_scale: [0.69223469, 0.61101982]"
        )
        print("FINAL RVRS auto_loc", params["auto_loc"])
        print("FINAL RVRS auto_scale", params["auto_scale"])
        print("AutoDiag posterior_mean:  [0.75519637 0.42030578]")
        print("AutoDiag posterior_std:  [0.11744191 0.14285692]")
        print("posterior_mean: ", posterior_mean)
        print("posterior_std: ", posterior_std)
        print("true posterior_mean: ", true_coefs)
        final_elbo = jnp.mean(
            lax.map(
                lambda key: -Trace_ELBO().loss(key, params, model, guide, data),
                random.split(random.PRNGKey(2), 1000),
            )
        )
        print("RVRS final elbo: ", final_elbo, "    (expected ~ -13.96)")
    elif auto_class == AutoDiagonalNormal:
        final_elbo = (
            -Trace_ELBO(num_particles=1000)
            .loss(random.PRNGKey(2), params, model, guide, data)
            .item()
        )
        print("AutoDiagonalNormal final elbo: ", final_elbo)

    assert_allclose(posterior_mean, true_coefs, atol=0.03)

    if auto_class not in [AutoDAIS, AutoDelta, AutoIAFNormal, AutoBNAFNormal, AutoRVRS]:
        quantiles = guide.quantiles(params, [0.2, 0.5, 0.8])
        assert quantiles["beta"].shape == (3, 2)

    # Predictive can be instantiated from posterior samples...
    predictive = Predictive(model, posterior_samples=posterior_samples)
    predictive_samples = predictive(random.PRNGKey(1), None)
    num_samples = 1000 if auto_class != AutoRVRS else 1000 * guide.S
    assert predictive_samples["obs"].shape == (num_samples, N, 2)

    # ... or from the guide + params
    if auto_class != AutoRVRS:
        predictive = Predictive(model, guide=guide, params=params, num_samples=11)
        predictive_samples = predictive(random.PRNGKey(1), None)
        assert predictive_samples["obs"].shape == (11, N, 2)


@pytest.mark.parametrize(
    "auto_class",
    [
        AutoRVRS,
        AutoDiagonalNormal,
        AutoIAFNormal,
        AutoDAIS,
        AutoBNAFNormal,
        AutoMultivariateNormal,
        AutoLaplaceApproximation,
        AutoLowRankMultivariateNormal,
        AutoNormal,
        AutoDelta,
    ],
)
@pytest.mark.parametrize("Elbo", [Trace_ELBO, TraceMeanField_ELBO][:1])
def test_logistic_regression(auto_class, Elbo):
    if auto_class == AutoRVRS and Elbo is TraceMeanField_ELBO:
        pytest.skip("Skip MeanField_ELBO for AutoRVRS.")

    N, dim = 100, 3
    data = random.normal(random.PRNGKey(0), (N, dim))
    true_coefs = jnp.arange(1.0, dim + 1.0)
    logits = jnp.sum(true_coefs * data, axis=-1)
    labels = dist.Bernoulli(logits=logits).sample(random.PRNGKey(1))

    def model(data=None, labels=None):
        coefs = numpyro.sample("coefs", dist.Normal(0, 1).expand([dim]).to_event())
        if data is not None:
            logits = numpyro.deterministic("logits", jnp.sum(coefs * data, axis=-1))
            with numpyro.plate("N", len(data)):
                return numpyro.sample("obs", dist.Bernoulli(logits=logits), obs=labels)

    adam = optim.Adam(0.001)
    rng_key_init = random.PRNGKey(1)
    if auto_class not in [AutoRVRS]:
        guide = auto_class(model, init_loc_fn=init_strategy)
    else:
        init_loc_fn = init_to_median(num_samples=100)
        guide = auto_class(
            model, S=16, T=40.0, epsilon=0.05, init_scale=0.5, init_loc_fn=init_loc_fn
        )
    svi = SVI(model, guide, adam, Elbo())
    svi_state = svi.init(rng_key_init, data, labels)

    # smoke test if analytic KL is used
    if auto_class is AutoNormal and Elbo is TraceMeanField_ELBO:
        _, mean_field_loss = svi.update(svi_state, data, labels)
        svi.loss = Trace_ELBO()
        _, elbo_loss = svi.update(svi_state, data, labels)
        svi.loss = TraceMeanField_ELBO()
        # assert abs(mean_field_loss - elbo_loss) > 0.5

    def body_fn(i, val):
        svi_state, loss = svi.update(val, data, labels)
        return svi_state

    svi_state = fori_loop(0, 10000, body_fn, svi_state)
    params = svi.get_params(svi_state)
    if auto_class not in (AutoDAIS, AutoIAFNormal, AutoBNAFNormal, AutoRVRS):
        median = guide.median(params)
        # assert_allclose(median["coefs"], true_coefs, rtol=0.1)
        # test .quantile method
        if auto_class is not AutoDelta:
            median = guide.quantiles(params, [0.2, 0.5])
            # assert_allclose(median["coefs"][1], true_coefs, rtol=0.1)
    # test .sample_posterior method
    posterior_samples = guide.sample_posterior(
        random.PRNGKey(1), params, sample_shape=(1000,)
    )
    expected_coefs = jnp.array([0.97, 2.05, 3.18])
    print("\nRVRS posterior: ", jnp.mean(posterior_samples["coefs"], (0, 1)))
    print("RVRS posterior std: ", jnp.std(posterior_samples["coefs"], (0, 1)))
    # print("Expected posterior: ", expected_coefs)

    if "auto_z_0_loc" in params:
        print("auto_z_0_loc: ", params["auto_z_0_loc"])
        print("auto_z_0_scale: ", params["auto_z_0_scale"])
    if auto_class == AutoRVRS:
        print(
            "Final T_adapt: {:.4f}".format(
                svi_state.mutable_state["_T_adapt"]["value"].item()
            )
        )

    # assert_allclose(jnp.mean(posterior_samples["coefs"], 0), expected_coefs, rtol=0.1)


def test_iaf():
    # test for substitute logic for exposed methods `sample_posterior` and `get_transforms`
    N, dim = 3000, 3
    data = random.normal(random.PRNGKey(0), (N, dim))
    true_coefs = jnp.arange(1.0, dim + 1.0)
    logits = jnp.sum(true_coefs * data, axis=-1)
    labels = dist.Bernoulli(logits=logits).sample(random.PRNGKey(1))

    def model(data, labels):
        coefs = numpyro.sample("coefs", dist.Normal(jnp.zeros(dim), jnp.ones(dim)))
        offset = numpyro.sample("offset", dist.Uniform(-1, 1))
        logits = offset + jnp.sum(coefs * data, axis=-1)
        return numpyro.sample("obs", dist.Bernoulli(logits=logits), obs=labels)

    adam = optim.Adam(0.01)
    rng_key_init = random.PRNGKey(1)
    guide = AutoIAFNormal(model)
    svi = SVI(model, guide, adam, Trace_ELBO())
    svi_state = svi.init(rng_key_init, data, labels)
    params = svi.get_params(svi_state)

    x = random.normal(random.PRNGKey(0), (dim + 1,))
    rng_key = random.PRNGKey(1)
    actual_sample = guide.sample_posterior(rng_key, params)
    actual_output = guide._unpack_latent(guide.get_transform(params)(x))

    flows = []
    for i in range(guide.num_flows):
        if i > 0:
            flows.append(transforms.PermuteTransform(jnp.arange(dim + 1)[::-1]))
        arn_init, arn_apply = AutoregressiveNN(
            dim + 1,
            [dim + 1, dim + 1],
            permutation=jnp.arange(dim + 1),
            skip_connections=guide._skip_connections,
            nonlinearity=guide._nonlinearity,
        )
        arn = partial(arn_apply, params["auto_arn__{}$params".format(i)])
        flows.append(InverseAutoregressiveTransform(arn))
    flows.append(guide._unpack_latent)

    transform = transforms.ComposeTransform(flows)
    _, rng_key_sample = random.split(rng_key)
    expected_sample = transform(
        dist.Normal(jnp.zeros(dim + 1), 1).sample(rng_key_sample)
    )
    expected_output = transform(x)
    assert_allclose(actual_sample["coefs"], expected_sample["coefs"])
    assert_allclose(
        actual_sample["offset"],
        transforms.biject_to(constraints.interval(-1, 1))(expected_sample["offset"]),
    )

    tree_all(tree_map(assert_allclose, actual_output, expected_output))


def test_uniform_normal():
    true_coef = 0.9
    data = true_coef + random.normal(random.PRNGKey(0), (1000,))

    def model(data):
        alpha = numpyro.sample("alpha", dist.Uniform(0, 1))
        with numpyro.handlers.reparam(config={"loc": TransformReparam()}):
            loc = numpyro.sample(
                "loc",
                dist.TransformedDistribution(
                    dist.Uniform(0, 1), transforms.AffineTransform(0, alpha)
                ),
            )
        with numpyro.plate("N", len(data)):
            numpyro.sample("obs", dist.Normal(loc, 0.1), obs=data)

    adam = optim.Adam(0.01)
    rng_key_init = random.PRNGKey(1)
    guide = AutoDiagonalNormal(model)
    svi = SVI(model, guide, adam, Trace_ELBO())
    svi_state = svi.init(rng_key_init, data)

    def body_fn(i, val):
        svi_state, loss = svi.update(val, data)
        return svi_state

    svi_state = fori_loop(0, 1000, body_fn, svi_state)
    params = svi.get_params(svi_state)
    median = guide.median(params)
    assert_allclose(median["loc"], true_coef, rtol=0.05)
    # test .quantile method
    median = guide.quantiles(params, [0.2, 0.5])
    assert_allclose(median["loc"][1], true_coef, rtol=0.1)


def test_param():
    # this test the validity of model having
    # param sites contain composed transformed constraints
    rng_keys = random.split(random.PRNGKey(0), 3)
    a_minval = 1
    a_init = jnp.exp(random.normal(rng_keys[0])) + a_minval
    b_init = jnp.exp(random.normal(rng_keys[1]))
    x_init = random.normal(rng_keys[2])

    def model():
        a = numpyro.param("a", a_init, constraint=constraints.greater_than(a_minval))
        b = numpyro.param("b", b_init, constraint=constraints.positive)
        numpyro.sample("x", dist.Normal(a, b))

    # this class is used to force init value of `x` to x_init
    class _AutoGuide(AutoDiagonalNormal):
        def __call__(self, *args, **kwargs):
            return substitute(
                super(_AutoGuide, self).__call__, {"_auto_latent": x_init[None]}
            )(*args, **kwargs)

    adam = optim.Adam(0.01)
    rng_key_init = random.PRNGKey(1)
    guide = _AutoGuide(model)
    svi = SVI(model, guide, adam, Trace_ELBO())
    svi_state = svi.init(rng_key_init)

    params = svi.get_params(svi_state)
    assert_allclose(params["a"], a_init, rtol=1e-6)
    assert_allclose(params["b"], b_init, rtol=1e-6)
    assert_allclose(params["auto_loc"], guide._init_latent, rtol=1e-6)
    assert_allclose(params["auto_scale"], jnp.ones(1) * guide._init_scale, rtol=1e-6)

    actual_loss = svi.evaluate(svi_state)
    assert jnp.isfinite(actual_loss)
    expected_loss = dist.Normal(guide._init_latent, guide._init_scale).log_prob(
        x_init
    ) - dist.Normal(a_init, b_init).log_prob(x_init)
    assert_allclose(actual_loss, expected_loss, rtol=1e-6)


def test_dynamic_supports():
    true_coef = 0.9
    data = true_coef + random.normal(random.PRNGKey(0), (1000,))

    def actual_model(data):
        alpha = numpyro.sample("alpha", dist.Uniform(0, 1))
        with numpyro.handlers.reparam(config={"loc": TransformReparam()}):
            loc = numpyro.sample(
                "loc",
                dist.TransformedDistribution(
                    dist.Uniform(0, 1), transforms.AffineTransform(0, alpha)
                ),
            )
        with numpyro.plate("N", len(data)):
            numpyro.sample("obs", dist.Normal(loc, 0.1), obs=data)

    def expected_model(data):
        alpha = numpyro.sample("alpha", dist.Uniform(0, 1))
        loc = numpyro.sample("loc", dist.Uniform(0, 1)) * alpha
        with numpyro.plate("N", len(data)):
            numpyro.sample("obs", dist.Normal(loc, 0.1), obs=data)

    adam = optim.Adam(0.01)
    rng_key_init = random.PRNGKey(1)

    guide = AutoDiagonalNormal(actual_model)
    svi = SVI(actual_model, guide, adam, Trace_ELBO())
    svi_state = svi.init(rng_key_init, data)
    actual_opt_params = adam.get_params(svi_state.optim_state)
    actual_params = svi.get_params(svi_state)
    actual_values = guide.median(actual_params)
    actual_loss = svi.evaluate(svi_state, data)

    guide = AutoDiagonalNormal(expected_model)
    svi = SVI(expected_model, guide, adam, Trace_ELBO())
    svi_state = svi.init(rng_key_init, data)
    expected_opt_params = adam.get_params(svi_state.optim_state)
    expected_params = svi.get_params(svi_state)
    expected_values = guide.median(expected_params)
    expected_loss = svi.evaluate(svi_state, data)

    # test auto_loc, auto_scale
    tree_all(tree_map(assert_allclose, actual_opt_params, expected_opt_params))
    tree_all(tree_map(assert_allclose, actual_params, expected_params))
    # test latent values
    assert_allclose(actual_values["alpha"], expected_values["alpha"])
    assert_allclose(actual_values["loc_base"], expected_values["loc"])
    assert_allclose(actual_loss, expected_loss)


def test_laplace_approximation_warning():
    def model(x, y):
        a = numpyro.sample("a", dist.Normal(0, 10))
        b = numpyro.sample("b", dist.Normal(0, 10).expand([3]).to_event())
        mu = a + b[0] * x + b[1] * x**2 + b[2] * x**3
        with numpyro.plate("N", len(x)):
            numpyro.sample("y", dist.Normal(mu, 0.001), obs=y)

    x = random.normal(random.PRNGKey(0), (3,))
    y = 1 + 2 * x + 3 * x**2 + 4 * x**3
    guide = AutoLaplaceApproximation(model)
    svi = SVI(model, guide, optim.Adam(0.1), Trace_ELBO(), x=x, y=y)
    init_state = svi.init(random.PRNGKey(0))
    svi_state = fori_loop(0, 10000, lambda i, val: svi.update(val)[0], init_state)
    params = svi.get_params(svi_state)
    with pytest.warns(UserWarning, match="Hessian of log posterior"):
        guide.sample_posterior(random.PRNGKey(1), params)


def test_laplace_approximation_custom_hessian():
    def model(x, y):
        a = numpyro.sample("a", dist.Normal(0, 10))
        b = numpyro.sample("b", dist.Normal(0, 10))
        mu = a + b * x
        with numpyro.plate("N", len(x)):
            numpyro.sample("y", dist.Normal(mu, 1), obs=y)

    x = random.normal(random.PRNGKey(0), (100,))
    y = 1 + 2 * x
    guide = AutoLaplaceApproximation(
        model, hessian_fn=lambda f, x: jacobian(jacobian(f))(x)
    )
    svi = SVI(model, guide, optim.Adam(0.1), Trace_ELBO(), x=x, y=y)
    svi_result = svi.run(random.PRNGKey(0), 10000, progress_bar=False)
    guide.get_transform(svi_result.params)


def test_improper():
    y = random.normal(random.PRNGKey(0), (100,))

    def model(y):
        lambda1 = numpyro.sample(
            "lambda1", dist.ImproperUniform(dist.constraints.real, (), ())
        )
        lambda2 = numpyro.sample(
            "lambda2", dist.ImproperUniform(dist.constraints.real, (), ())
        )
        sigma = numpyro.sample(
            "sigma", dist.ImproperUniform(dist.constraints.positive, (), ())
        )
        mu = numpyro.deterministic("mu", lambda1 + lambda2)
        with numpyro.plate("N", len(y)):
            numpyro.sample("y", dist.Normal(mu, sigma), obs=y)

    guide = AutoDiagonalNormal(model)
    svi = SVI(model, guide, optim.Adam(0.003), Trace_ELBO(), y=y)
    svi_state = svi.init(random.PRNGKey(2))
    lax.scan(lambda state, i: svi.update(state), svi_state, jnp.zeros(10000))


def test_module():
    x = random.normal(random.PRNGKey(0), (100, 10))
    y = random.normal(random.PRNGKey(1), (100,))

    def model(x, y):
        nn = numpyro.module("nn", Dense(1), (10,))
        mu = nn(x).squeeze(-1)
        sigma = numpyro.sample("sigma", dist.HalfNormal(1))
        with numpyro.plate("N", len(y)):
            numpyro.sample("y", dist.Normal(mu, sigma), obs=y)

    guide = AutoDiagonalNormal(model)
    svi = SVI(model, guide, optim.Adam(0.003), Trace_ELBO(), x=x, y=y)
    svi_state = svi.init(random.PRNGKey(2))
    lax.scan(lambda state, i: svi.update(state), svi_state, jnp.zeros(1000))


@pytest.mark.parametrize("auto_class", [AutoNormal])
def test_subsample_guide(auto_class):
    # The model adapted from tutorial/source/easyguide.ipynb
    def model(batch, subsample, full_size):
        drift = numpyro.sample("drift", dist.LogNormal(-1, 0.5))
        with handlers.substitute(data={"data": subsample}):
            plate = numpyro.plate("data", full_size, subsample_size=len(subsample))
        assert plate.size == 50

        def transition_fn(z_prev, y_curr):
            with plate:
                z_curr = numpyro.sample("state", dist.Normal(z_prev, drift))
                y_curr = numpyro.sample(
                    "obs", dist.Bernoulli(logits=z_curr), obs=y_curr
                )
            return z_curr, y_curr

        _, result = scan(
            transition_fn, jnp.zeros(len(subsample)), batch, length=num_time_steps
        )
        return result

    def create_plates(batch, subsample, full_size):
        with handlers.substitute(data={"data": subsample}):
            return numpyro.plate("data", full_size, subsample_size=subsample.shape[0])

    guide = auto_class(model, create_plates=create_plates)

    full_size = 50
    batch_size = 20
    num_time_steps = 8
    with handlers.seed(rng_seed=0):
        data = model(None, jnp.arange(full_size), full_size)
    assert data.shape == (num_time_steps, full_size)

    svi = SVI(model, guide, optim.Adam(0.02), Trace_ELBO())
    svi_state = svi.init(
        random.PRNGKey(0),
        data[:, :batch_size],
        jnp.arange(batch_size),
        full_size=full_size,
    )
    update_fn = jit(svi.update, static_argnums=(3,))
    for epoch in range(2):
        beg = 0
        while beg < full_size:
            end = min(full_size, beg + batch_size)
            subsample = jnp.arange(beg, end)
            batch = data[:, beg:end]
            beg = end
            svi_state, loss = update_fn(svi_state, batch, subsample, full_size)


@pytest.mark.parametrize(
    "auto_class",
    [
        AutoDiagonalNormal,
        AutoMultivariateNormal,
        AutoLaplaceApproximation,
        AutoLowRankMultivariateNormal,
        AutoNormal,
        AutoDelta,
    ],
)
def test_autoguide_deterministic(auto_class):
    def model(y=None):
        n, len_y = (y.size, len(y)) if y is not None else (1, 1)

        mu = numpyro.sample("mu", dist.Normal(0, 5))
        sigma = numpyro.param("sigma", 1, constraint=constraints.positive)

        with numpyro.plate("N", len_y):
            y = numpyro.sample("y", dist.Normal(mu, sigma).expand((n,)), obs=y)
        numpyro.deterministic("z", (y - mu) / sigma)

    mu, sigma = 2, 3
    y = mu + sigma * random.normal(random.PRNGKey(0), shape=(300,))
    y_train = y[:200]
    y_test = y[200:]

    guide = auto_class(model)
    optimiser = numpyro.optim.Adam(step_size=0.01)
    svi = SVI(model, guide, optimiser, Trace_ELBO())

    svi_result = svi.run(random.PRNGKey(0), num_steps=500, y=y_train)
    params = svi_result.params
    posterior_samples = guide.sample_posterior(
        random.PRNGKey(0), params, sample_shape=(1000,)
    )

    predictive = Predictive(model, posterior_samples, params=params)
    predictive_samples = predictive(random.PRNGKey(0), y_test)

    assert predictive_samples["y"].shape == (1000, 100)
    assert predictive_samples["z"].shape == (1000, 100)
    assert_allclose(
        (predictive_samples["y"] - posterior_samples["mu"][..., None])
        / params["sigma"],
        predictive_samples["z"],
        atol=0.05,
    )


@pytest.mark.parametrize("size,dim", [(10, -2), (5, -1)])
def test_plate_inconsistent(size, dim):
    def model():
        with numpyro.plate("a", 10, dim=-1):
            numpyro.sample("x", dist.Normal(0, 1))
        with numpyro.plate("a", size, dim=dim):
            numpyro.sample("y", dist.Normal(0, 1))

    guide = AutoDelta(model)
    svi = SVI(model, guide, numpyro.optim.Adam(step_size=0.1), Trace_ELBO())
    with pytest.raises(AssertionError, match="has inconsistent dim or size"):
        svi.run(random.PRNGKey(0), 10)


@pytest.mark.parametrize(
    "auto_class",
    [
        AutoDelta,
        AutoDiagonalNormal,
        AutoMultivariateNormal,
        AutoNormal,
        AutoLowRankMultivariateNormal,
        AutoLaplaceApproximation,
    ],
)
@pytest.mark.parametrize(
    "init_loc_fn",
    [
        init_to_feasible,
        init_to_median,
        init_to_sample,
        init_to_uniform,
    ],
)
@pytest.mark.filterwarnings("ignore:.*enumerate.*:FutureWarning")
def test_discrete_helpful_error(auto_class, init_loc_fn):
    def model():
        p = numpyro.sample("p", dist.Beta(2.0, 2.0))
        x = numpyro.sample("x", dist.Bernoulli(p))
        with numpyro.plate("N", 2):
            numpyro.sample(
                "obs",
                dist.Bernoulli(p * x + (1 - p) * (1 - x)),
                obs=jnp.array([1.0, 0.0]),
            )

    guide = auto_class(model, init_loc_fn=init_loc_fn)
    with pytest.raises(ValueError, match=".*handle discrete.*"):
        handlers.seed(guide, 0)()


@pytest.mark.parametrize(
    "auto_class",
    [
        AutoDelta,
        AutoDiagonalNormal,
        AutoMultivariateNormal,
        AutoNormal,
        AutoLowRankMultivariateNormal,
        AutoLaplaceApproximation,
    ],
)
@pytest.mark.parametrize(
    "init_loc_fn",
    [
        init_to_feasible,
        init_to_median,
        init_to_sample,
        init_to_uniform,
    ],
)
def test_sphere_helpful_error(auto_class, init_loc_fn):
    def model():
        x = numpyro.sample("x", dist.Normal(0.0, 1.0).expand([2]).to_event(1))
        y = numpyro.sample("y", dist.ProjectedNormal(x))
        numpyro.sample("obs", dist.Normal(y, 1), obs=jnp.array([1.0, 0.0]))

    guide = auto_class(model, init_loc_fn=init_loc_fn)
    with pytest.raises(ValueError, match=".*ProjectedNormalReparam.*"):
        handlers.seed(guide, 0)()


def test_autodais_subsampling_error():
    data = jnp.array([1.0] * 8 + [0.0] * 2)

    def model(data):
        f = numpyro.sample("beta", dist.Beta(1, 1))
        with numpyro.plate("plate", 20, 10, dim=-1):
            numpyro.sample("obs", dist.Bernoulli(f), obs=data)

    adam = optim.Adam(0.01)
    guide = AutoDAIS(model)
    svi = SVI(model, guide, adam, Trace_ELBO())

    with pytest.raises(NotImplementedError, match=".*data subsampling.*"):
        svi.init(random.PRNGKey(1), data)


def test_subsample_model_with_deterministic():
    def model():
        x = numpyro.sample("x", dist.Normal(0, 1))
        numpyro.deterministic("x2", x * 2)
        with numpyro.plate("N", 10, subsample_size=5):
            numpyro.sample("obs", dist.Normal(x, 1), obs=jnp.ones(5))

    guide = AutoNormal(model)
    svi = SVI(model, guide, optim.Adam(1.0), Trace_ELBO())
    svi_result = svi.run(random.PRNGKey(0), 10)
    samples = guide.sample_posterior(random.PRNGKey(1), svi_result.params)
    assert "x2" in samples


def test_autocontinuous_local_error():
    def model():
        with numpyro.plate("N", 10, subsample_size=4):
            numpyro.sample("x", dist.Normal(0, 1))

    guide = AutoDiagonalNormal(model)
    svi = SVI(model, guide, optim.Adam(1.0), Trace_ELBO())
    with pytest.raises(ValueError, match="local latent variables"):
        svi.init(random.PRNGKey(0))


def test_init_to_scalar_value():
    def model():
        numpyro.sample("x", dist.Normal(0, 1))

    guide = AutoDiagonalNormal(model, init_loc_fn=init_to_value(values={"x": 1.0}))
    svi = SVI(model, guide, optim.Adam(1.0), Trace_ELBO())
    svi.init(random.PRNGKey(0))


def test_autosemidais(N=18, P=3, sigma_obs=0.1, num_steps=45 * 1000, num_samples=5000):
    X = RandomState(0).randn(N, P)
    Y = X[:, 0] - 0.5 * X[:, 1] + sigma_obs * RandomState(1).randn(N)

    def global_model():
        return numpyro.sample("theta", dist.Normal(jnp.zeros(P), 1).to_event(1))

    def local_model(subsample_size, theta):
        with numpyro.plate("N", N, subsample_size=subsample_size):
            X_batch = numpyro.subsample(X, event_dim=1)
            Y_batch = numpyro.subsample(Y, event_dim=0)
            tau = numpyro.sample("tau", dist.Gamma(5.0, 5.0))
            numpyro.sample(
                "obs",
                dist.Normal(X_batch @ theta, sigma_obs / jnp.sqrt(tau)),
                obs=Y_batch,
            )

    def model12():
        return local_model(12, global_model())

    def model16():
        return local_model(16, global_model())

    def _get_optim():
        scheduler = piecewise_constant_schedule(
            1.0e-3, {15 * 1000: 1.0e-4, 30 * 1000: 1.0e-5}
        )
        return optax.chain(
            optax.scale_by_adam(), optax.scale_by_schedule(scheduler), optax.scale(-1.0)
        )

    base_guide = AutoNormal(global_model)
    guide16 = AutoSemiDAIS(
        model16, partial(local_model, 16), base_guide, K=4, eta_max=0.25, eta_init=0.005
    )
    svi_result16 = SVI(model16, guide16, _get_optim(), Trace_ELBO()).run(
        random.PRNGKey(0), num_steps
    )

    assert svi_result16.params["auto_eta_coeff"].mean() > 0.2

    samples16 = guide16.sample_posterior(random.PRNGKey(1), svi_result16.params)
    assert samples16["theta"].shape == (P,) and samples16["tau"].shape == (16,)

    dais_elbo16 = Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(0), svi_result16.params, model16, guide16
    )
    dais_elbo16 = -dais_elbo16.item()

    guide12 = AutoSemiDAIS(
        model12, partial(local_model, 12), base_guide, K=4, eta_max=0.25, eta_init=0.005
    )
    with handlers.seed(rng_seed=0):
        guide12()  # initialize guide since we are not training this guide
    samples12 = guide12.sample_posterior(random.PRNGKey(1), svi_result16.params)
    assert samples12["theta"].shape == (P,) and samples12["tau"].shape == (12,)

    dais_elbo12 = Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(0), svi_result16.params, model12, guide12
    )
    dais_elbo12 = -dais_elbo12.item()
    assert_allclose(dais_elbo12, dais_elbo16, atol=0.05)

    def create_plates():
        return numpyro.plate("N", N, subsample_size=16)

    mf_guide = AutoNormal(model16, create_plates=create_plates)
    mf_svi_result = SVI(model16, mf_guide, _get_optim(), Trace_ELBO()).run(
        random.PRNGKey(0), num_steps
    )

    mf_elbo = Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(0), mf_svi_result.params, model16, mf_guide
    )
    mf_elbo = -mf_elbo.item()
    assert dais_elbo16 > mf_elbo + 0.1

    with handlers.substitute(
        data={"N": jnp.array([0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])}
    ):
        samples_one = guide12.sample_posterior(random.PRNGKey(1), svi_result16.params)

    with handlers.substitute(
        data={"N": jnp.array([0, 2, 9, 10, 11, 12, 13, 14, 3, 4, 5, 6])}
    ):
        samples_two = guide12.sample_posterior(random.PRNGKey(1), svi_result16.params)
    assert_allclose(samples_one["theta"], samples_two["theta"])
    assert_allclose(samples_one["tau"][:2], samples_two["tau"][:2])
    assert jnp.min(jnp.abs(samples_one["tau"][2:] - samples_two["tau"][2:])) > 1e-5


def test_autosemidais_admissible_smoke():
    def global_model():
        theta = numpyro.sample("theta", dist.Normal(0, 1))
        tau = numpyro.sample("tau", dist.LogNormal(jnp.zeros(2), 1).to_event(1))
        return {"tau": tau.mean(), "theta": theta}

    def local_model1(global_latents):
        tau = global_latents["tau"]
        theta = global_latents["theta"]
        with numpyro.plate("inner", 10, subsample_size=5, dim=-1):
            with numpyro.plate("outer", 20, dim=-2):
                sigma1 = numpyro.sample("sigma", dist.LogNormal(0.0, 1.0))
                sigma2 = numpyro.sample(
                    "log_sigma", dist.Normal(jnp.zeros(2), 1.0).to_event(1)
                )
                sigma2 = jnp.exp(sigma2.mean(-1))
                assert sigma1.shape == (20, 5) and sigma2.shape == (20, 5)
                numpyro.sample(
                    "obs",
                    dist.Normal(theta, tau * sigma1 * sigma2),
                    obs=jnp.ones((20, 5)),
                )

    def model():
        return local_model1(global_model())

    base_guide = AutoNormal(global_model)
    guide = AutoSemiDAIS(model, local_model1, base_guide)
    svi = SVI(model, guide, optim.Adam(0.01), Trace_ELBO())
    svi_result = svi.run(random.PRNGKey(0), 10)
    samples = guide.sample_posterior(random.PRNGKey(1), svi_result.params)
    assert samples["theta"].shape == () and samples["tau"].shape == (2,)
    assert samples["sigma"].shape == (20, 5)
    assert samples["log_sigma"].shape == (20, 5, 2)

    def local_model2(global_latents):
        tau = global_latents["tau"]
        theta = global_latents["theta"]
        with numpyro.plate("inner", 10, subsample_size=5, dim=-1):
            sigma1 = numpyro.sample("sigma", dist.LogNormal(0.0, 1.0))
            sigma2 = numpyro.sample(
                "log_sigma", dist.Normal(jnp.zeros(2), 1.0).to_event(1)
            )
            sigma2 = jnp.exp(sigma2.mean(-1))
            numpyro.sample(
                "obs", dist.Normal(theta, tau * sigma1 * sigma2), obs=jnp.ones(5)
            )

    def model():
        return local_model2(global_model())

    base_guide = AutoNormal(global_model)
    guide = AutoSemiDAIS(model, local_model2, base_guide)
    svi = SVI(model, guide, optim.Adam(0.01), Trace_ELBO())
    svi_result = svi.run(random.PRNGKey(0), 10)
    samples = guide.sample_posterior(random.PRNGKey(1), svi_result.params)
    assert samples["theta"].shape == () and samples["tau"].shape == (2,)
    assert samples["sigma"].shape == (5,) and samples["log_sigma"].shape == (5, 2)


def test_autosemidais_local_only():
    data = jnp.linspace(0, 1, 10)

    def model():
        with numpyro.plate("N", 10, subsample_size=5, dim=-1):
            batch = numpyro.subsample(data, event_dim=0)
            numpyro.sample("x", dist.Normal(batch, 1))

    def create_plates():
        return numpyro.plate("N", 10, subsample_size=5, dim=-1)

    local_guide = AutoNormal(model, create_plates=create_plates)
    guide = AutoSemiDAIS(model, model, None, local_guide=local_guide)
    svi = SVI(model, guide, optim.Adam(0.01), Trace_ELBO())
    svi_result = svi.run(random.PRNGKey(0), 10)
    samples = guide.sample_posterior(
        random.PRNGKey(1), svi_result.params, sample_shape=(100,)
    )
    assert samples["x"].shape == (100, 5)


def test_autosemidais_inadmissible_smoke():
    def global_model():
        return numpyro.sample("theta", dist.Normal(0, 1))

    def local_model1(theta):
        with numpyro.plate("a", 10, 5):
            numpyro.sample("x", dist.Normal(0, 1))
            with numpyro.plate("b", 20, 10):
                numpyro.sample("y", dist.Normal(0, 1))

    def local_model2(theta):
        with numpyro.plate("a", 10, 5):
            numpyro.sample("y", dist.Normal(0, 1), obs=jnp.ones(5))

    def model():
        return local_model1(global_model())

    base_guide = AutoNormal(global_model)
    guide = AutoSemiDAIS(model, local_model1, base_guide)
    svi = SVI(model, guide, optim.Adam(0.01), Trace_ELBO())

    with pytest.raises(AssertionError, match="contains exactly 1 plate"):
        svi.run(random.PRNGKey(0), 10)

    def model2():
        return local_model2(global_model())

    guide = AutoSemiDAIS(model2, local_model2, base_guide)
    svi = SVI(model2, guide, optim.Adam(0.01), Trace_ELBO())
    with pytest.raises(RuntimeError, match="are no local variables"):
        svi.run(random.PRNGKey(0), 10)


def test_autosldais(
    N=64, subsample_size=48, num_surrogate=32, D=3, num_steps=40000, num_samples=2000
):
    def _model(X, Y):
        theta = numpyro.sample(
            "theta", dist.Normal(jnp.zeros(D), jnp.ones(D)).to_event(1)
        )
        with numpyro.plate("N", N, subsample_size=subsample_size):
            X_batch = numpyro.subsample(X, event_dim=1)
            Y_batch = numpyro.subsample(Y, event_dim=0)
            numpyro.sample("obs", dist.Bernoulli(logits=theta @ X_batch.T), obs=Y_batch)

    def _surrogate_model(X_surr, Y_surr):
        theta = numpyro.sample(
            "theta", dist.Normal(jnp.zeros(D), jnp.ones(D)).to_event(1)
        )
        omegas = numpyro.param(
            "omegas",
            2.0 * jnp.ones(num_surrogate),
            constraint=dist.constraints.positive,
        )

        with numpyro.plate("N", num_surrogate), numpyro.handlers.scale(scale=omegas):
            numpyro.sample("obs", dist.Bernoulli(logits=theta @ X_surr.T), obs=Y_surr)

    X = RandomState(0).randn(N, D)
    X[:, 2] = X[:, 0] + X[:, 1]
    logits = X[:, 0] - 0.5 * X[:, 1]
    Y = dist.Bernoulli(logits=logits).sample(random.PRNGKey(0))

    model = partial(_model, X, Y)
    surrogate_model = partial(_surrogate_model, X[:num_surrogate], Y[:num_surrogate])

    def _get_optim():
        scheduler = piecewise_constant_schedule(
            1.0e-3, {15 * 1000: 1.0e-4, 30 * 1000: 1.0e-5}
        )
        return optax.chain(
            optax.scale_by_adam(), optax.scale_by_schedule(scheduler), optax.scale(-1.0)
        )

    guide = AutoSurrogateLikelihoodDAIS(
        model, surrogate_model, K=3, eta_max=0.25, eta_init=0.005
    )
    svi_result = SVI(model, guide, _get_optim(), Trace_ELBO()).run(
        random.PRNGKey(1), num_steps
    )

    samples = guide.sample_posterior(random.PRNGKey(2), svi_result.params)
    assert samples["theta"].shape == (D,)

    dais_elbo = Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(0), svi_result.params, model, guide
    )
    dais_elbo = -dais_elbo.item()

    def create_plates():
        return numpyro.plate("N", N, subsample_size=subsample_size)

    mf_guide = AutoNormal(model, create_plates=create_plates)
    mf_svi_result = SVI(model, mf_guide, _get_optim(), Trace_ELBO()).run(
        random.PRNGKey(0), num_steps
    )

    mf_elbo = Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(1), mf_svi_result.params, model, mf_guide
    )
    mf_elbo = -mf_elbo.item()

    assert dais_elbo > mf_elbo + 0.1


def test_batch_rejection_sampler(T=-2.5, num_mc_samples=20**4):
    p = dist.Normal(0, 1)
    q = dist.Normal(0.2, 1.2)

    def accept_log_prob_fn(z, _):
        guide_lp = q.log_prob(z)
        lw = p.log_prob(z) - guide_lp
        return jax.nn.log_sigmoid(lw + T), lw, guide_lp

    def guide_sampler(key, _):
        return q.sample(key)

    keys = random.split(random.PRNGKey(0), num_mc_samples)

    z, _, log_Z_rejection, _, _ = batch_rejection_sampler(
        accept_log_prob_fn, guide_sampler, keys, None
    )

    assert_allclose(np.mean(z), 0, atol=0.02)
    assert_allclose(np.std(z), 1, atol=0.03)

    z_q = jax.vmap(guide_sampler)(keys, None)
    log_Z_q = logsumexp(accept_log_prob_fn(z_q, None)[0]) - jnp.log(num_mc_samples)

    assert_allclose(log_Z_rejection, log_Z_q, atol=0.01)


def test_rejection_sampler_grad(T=-2.5, num_mc_samples=10):
    p = dist.Normal(0, 1)
    keys = random.split(random.PRNGKey(0), num_mc_samples)

    def accept_log_prob_fn(z, params):
        q = dist.Normal(**params)
        guide_lp = q.log_prob(z)
        lw = p.log_prob(z) - guide_lp
        return jax.nn.log_sigmoid(lw + T), lw, guide_lp

    def guide_sampler(key, params):
        q = dist.Normal(**params)
        return q.sample(key)

    def get_z(params_raw):
        params = {"loc": params_raw["loc"], "scale": jnp.exp(params_raw["log_scale"])}
        z, *_ = batch_rejection_sampler(accept_log_prob_fn, guide_sampler, keys, params)
        return z, z

    def sample_z(params_raw, eps):
        return params_raw["loc"] + jnp.exp(params_raw["log_scale"]) * eps

    params_raw = {"loc": 0.2, "log_scale": jnp.log(1.2)}
    actual_grad, z = jax.jacfwd(get_z, argnums=0, has_aux=True)(params_raw)
    eps = (z - params_raw["loc"]) / jnp.exp(params_raw["log_scale"])
    expected_grad = jax.vmap(jax.grad(sample_z), (None, 0))(params_raw, eps)
    assert_allclose(actual_grad["loc"], expected_grad["loc"], atol=1e-7)
    assert_allclose(actual_grad["log_scale"], expected_grad["log_scale"], atol=1e-7)


def test_rejection_sampler_custom_grad(T=-2.5, num_mc_samples=10):
    p = dist.Normal(0, 1)

    def accept_log_prob_fn(z, params):
        q = dist.Normal(**params)
        return jax.nn.log_sigmoid(p.log_prob(z) - q.log_prob(z) + T), 0.0, 0.0

    def guide_sampler(key, params):
        q = dist.Normal(**params)
        return q.sample(key)

    def get_z(params_raw, key):
        params = {"loc": params_raw["loc"], "scale": jnp.exp(params_raw["log_scale"])}
        z, *_ = batch_rejection_sampler_custom(
            accept_log_prob_fn, guide_sampler, key, params
        )
        return z, z

    def sample_z(params_raw, eps):
        return params_raw["loc"] + jnp.exp(params_raw["log_scale"]) * eps

    params_raw = {"loc": 0.2, "log_scale": jnp.log(1.2)}
    keys = random.split(random.PRNGKey(0), num_mc_samples)
    actual_grad, z = jax.jacrev(get_z, has_aux=True)(params_raw, keys)
    eps = (z - params_raw["loc"]) / jnp.exp(params_raw["log_scale"])
    expected_grad = jax.vmap(jax.grad(sample_z), (None, 0))(params_raw, eps)
    assert_allclose(actual_grad["loc"], expected_grad["loc"], atol=1e-7)
    assert_allclose(actual_grad["log_scale"], expected_grad["log_scale"], atol=1e-7)


def test_autosemirvrs(
    N=18,
    P=3,
    sigma_obs=0.1,
    num_steps=45 * 1000,
    num_samples=5000,
    epsilon=0.1,
    Z_target=0.5,
    S=2,
    T_init=0.0,
    seed=0,
    adaptation_scheme="dual_averaging",
    # adaptation_scheme="fixed",
    # adaptation_scheme="Z_target",
):
    X = RandomState(0).randn(N, P)
    Y = X[:, 0] - 0.5 * X[:, 1] + sigma_obs * RandomState(1).randn(N)
    print()
    # print("X", X)
    # print("Y", Y)
    fix_theta = False

    def global_model():
        if fix_theta:
            return jnp.array([1.0, -0.5, 0.0])
        return numpyro.sample("theta", dist.Normal(jnp.zeros(P), 1).to_event(1))

    def local_model(subsample_size, theta):
        with numpyro.plate("N", N, subsample_size=subsample_size):
            X_batch = numpyro.subsample(X, event_dim=1)
            Y_batch = numpyro.subsample(Y, event_dim=0)
            tau = numpyro.sample("tau", dist.Gamma(2.0, 2.0))
            numpyro.sample(
                "obs",
                dist.Normal(X_batch @ theta, sigma_obs / jnp.sqrt(tau)),
                obs=Y_batch,
            )

    def model12():
        return local_model(12, global_model())

    def model16():
        return local_model(16, global_model())

    def _get_optim():
        scheduler = piecewise_constant_schedule(
            1.0e-3, {15 * 1000: 1.0e-4, 30 * 1000: 1.0e-5}
        )
        return optax.chain(
            optax.scale_by_adam(), optax.scale_by_schedule(scheduler), optax.scale(-1.0)
        )

    def create_plates(subsample_size, theta):
        return numpyro.plate("N", N, subsample_size=subsample_size)

    global_guide = AutoNormal(global_model)
    local_guide16 = AutoNormal(
        partial(local_model, 16), create_plates=partial(create_plates, 16)
    )
    local_guide12 = AutoNormal(
        partial(local_model, 12), create_plates=partial(create_plates, 12)
    )

    def mf_guide16():
        global_guide()
        local_guide16(jnp.zeros(P))

    svi_mf = SVI(model16, mf_guide16, _get_optim(), Trace_ELBO())
    mf_results = svi_mf.run(random.PRNGKey(seed), num_steps=num_steps)
    mf_params = mf_results.params
    mf_elbo = -Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(seed + 1), mf_params, model16, mf_guide16
    )
    print("MF ELBO:", mf_elbo)
    # if not fix_theta:
    #     print("MF theta:", mf_params["theta_auto_loc"])
    # print("MF tau:", mf_params["tau_auto_loc"])

    for T in list(range(11)) + [100, 1000, 10000]:
        rvrs_guide_T = AutoSemiRVRS(
            model16,
            partial(local_model, 16),
            global_guide,
            local_guide16,
            S=S,
            T=T,
            adaptation_scheme="fixed",
            Z_target=Z_target,
            epsilon=epsilon,
            include_log_Z=False,
        )
        rvrs_elbo_T = -Trace_ELBO(num_particles=num_samples).loss(
            random.PRNGKey(0), mf_params, model16, rvrs_guide_T
        )
        with handlers.substitute(data={"N": jnp.arange(N)}):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                posterior_samples = rvrs_guide_T.sample_posterior(
                    random.PRNGKey(1), params=mf_params, sample_shape=(num_samples,)
                )
        first_log_a = posterior_samples["first_log_a"].reshape(-1, N)
        log_Z = logsumexp(first_log_a, axis=0) - jnp.log(first_log_a.shape[0])
        rvrs_elbo_T = rvrs_elbo_T + log_Z.sum()
        print(f"RVRS16 T={T} ELBO:", rvrs_elbo_T)

    rvrs_guide16 = AutoSemiRVRS(
        model16,
        partial(local_model, 16),
        global_guide,
        local_guide16,
        S=S,
        T=T_init,
        adaptation_scheme=adaptation_scheme,
        Z_target=Z_target,
        epsilon=epsilon,
    )
    rvrs16_results = SVI(model16, rvrs_guide16, _get_optim(), Trace_ELBO()).run(
        random.PRNGKey(seed + 2),
        num_steps,
        init_params=mf_params,
    )
    if adaptation_scheme == "Z_target":
        print("RVRS16 T:", rvrs16_results.state.mutable_state["_T_adapt"]["value"])
    elif adaptation_scheme == "dual_averaging":
        print(
            "RVRS16 T:",
            rvrs16_results.state.mutable_state["_T_adapt"]["value"].temperature,
        )
    else:
        print("RVRS16 T:", T_init)
    rvrs16_params = rvrs16_results.params
    rvrs16_elbo = -Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(seed + 3), rvrs16_params, model16, rvrs_guide16
    )
    print("RVRS16 ELBO:", rvrs16_elbo)
    # if not fix_theta:
    #     print("RVRS16 theta:", rvrs16_params["theta_auto_loc"])
    # print("RVRS16 tau:", rvrs16_params["tau_auto_loc"])

    rvrs_guide12 = AutoSemiRVRS(
        model12,
        partial(local_model, 12),
        global_guide,
        local_guide12,
        S=S,
        T=T_init,
        adaptation_scheme=adaptation_scheme,
        Z_target=Z_target,
        epsilon=epsilon,
    )
    rvrs12_results = SVI(model12, rvrs_guide12, _get_optim(), Trace_ELBO()).run(
        random.PRNGKey(seed + 4),
        num_steps,
        init_params=mf_params,
    )
    if adaptation_scheme == "Z_target":
        print("RVRS12 T:", rvrs12_results.state.mutable_state["_T_adapt"]["value"])
    elif adaptation_scheme == "dual_averaging":
        print(
            "RVRS12 T:",
            rvrs12_results.state.mutable_state["_T_adapt"]["value"].temperature,
        )
    else:
        print("RVRS12 T:", T_init)
    rvrs12_params = rvrs12_results.params
    rvrs12_elbo = -Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(seed + 5), rvrs12_params, model12, rvrs_guide12
    )
    print("RVRS12 ELBO:", rvrs12_elbo)
    # if not fix_theta:
    #     print("RVRS12 theta:", rvrs12_params["theta_auto_loc"])
    # print("RVRS12 tau:", rvrs12_params["tau_auto_loc"])

    rvrs12_subs_elbo = -Trace_ELBO(num_particles=num_samples).loss(
        random.PRNGKey(seed + 6), rvrs16_params, model12, rvrs_guide12
    )
    print("RVRS12 ELBO (using RVRS16 params):", rvrs12_subs_elbo)

    samples16 = rvrs_guide16.sample_posterior(random.PRNGKey(seed + 7), rvrs16_params)
    if not fix_theta:
        assert samples16["theta"].shape == (S, P) and samples16["tau"].shape == (S, 16)

    with handlers.substitute(
        data={"N": jnp.array([0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12])}
    ):
        samples = rvrs_guide12.sample_posterior(
            random.PRNGKey(seed + 8), rvrs16_params, sample_shape=(100,)
        )
    if not fix_theta:
        assert samples["theta"].shape == (100, S, P)


def test_dummy_autorvrs(
    N=18,
    P=3,
    sigma_obs=0.1,
    num_steps=10 * 1000,
    num_samples=5000,
    epsilon=0.1,
    Z_target=0.5,
    S=2,
    T_init=0.0,
    seed=0,
    # adaptation_scheme="dual_averaging",
    # adaptation_scheme="Z_target",
    adaptation_scheme="fixed",
):
    X = RandomState(0).randn(N, P)
    Y = X[:, 0] - 0.5 * X[:, 1] + sigma_obs * RandomState(1).randn(N)
    print()
    print("X", X)
    print("Y", Y)

    def global_model():
        return jnp.array([1.0, -0.5, 0.0])

    def local_model(data_index, theta):
        X_i = X[data_index]
        Y_i = Y[data_index]
        tau = numpyro.sample("tau", dist.Gamma(2.0, 2.0))
        numpyro.sample(
            "obs",
            dist.Normal(X_i @ theta, sigma_obs / jnp.sqrt(tau)),
            obs=Y_i,
        )

    def _get_optim():
        scheduler = piecewise_constant_schedule(
            1.0e-3, {15 * 1000: 1.0e-4, 30 * 1000: 1.0e-5}
        )
        return optax.chain(
            optax.scale_by_adam(), optax.scale_by_schedule(scheduler), optax.scale(-1.0)
        )

    for i in range(N):
        print("=====")

        def model0():
            return local_model(i, global_model())

        local_guide0 = AutoDiagonalNormal(model0)
        mf_results = SVI(model0, local_guide0, _get_optim(), Trace_ELBO()).run(
            random.PRNGKey(seed), num_steps, progress_bar=False
        )
        mf_elbo = -Trace_ELBO(num_particles=num_samples).loss(
            random.PRNGKey(seed + 1), mf_results.params, model0, local_guide0
        )
        print("MF ELBO:", mf_elbo)
        print(f"MF tau{i}:", mf_results.params["auto_loc"])

        rvrs_guide0 = AutoRVRS(
            model0,
            guide=local_guide0,
            S=S,
            T=T_init,
            adaptation_scheme=adaptation_scheme,
            Z_target=Z_target,
            epsilon=epsilon,
        )
        rvrs0_results = SVI(model0, rvrs_guide0, _get_optim(), Trace_ELBO()).run(
            random.PRNGKey(seed + 2), num_steps, progress_bar=False
        )
        rvrs0_params = rvrs0_results.params
        rvrs_elbo = -Trace_ELBO(num_particles=num_samples).loss(
            random.PRNGKey(seed + 3), rvrs0_params, model0, rvrs_guide0
        )
        if adaptation_scheme == "Z_target":
            print(f"RVRS T{i}:", rvrs0_results.state.mutable_state["_T_adapt"]["value"])
        elif adaptation_scheme == "dual_averaging":
            print(
                f"RVRS T{i}:",
                rvrs0_results.state.mutable_state["_T_adapt"]["value"].temperature,
            )
        print("RVRS ELBO:", rvrs_elbo)
        print(f"RVRS tau{i}:", rvrs0_params["auto_loc"])


def test_indep_semirvrs(N=21, T=100.0, subsample_size=10, num_steps=10000):
    target_loc = np.linspace(1.0, 3.0, N)

    def global_model():
        numpyro.sample("a", dist.LogNormal(1.0, 0.3))
        numpyro.sample("b", dist.LogNormal(2.0, 0.7))

    def local_model(_):
        with numpyro.plate("N", N, subsample_size=subsample_size):
            loc = numpyro.subsample(target_loc, event_dim=0)
            numpyro.sample("x", dist.LogNormal(loc, 0.4))
            numpyro.sample("y", dist.LogNormal(loc, 0.6))

    def create_plates(theta):
        return numpyro.plate("N", N, subsample_size=subsample_size)

    S = 6
    global_guide = AutoNormal(global_model)
    local_guide = AutoNormal(local_model, create_plates=create_plates)
    model = lambda: local_model(global_model())
    guide = AutoSemiRVRS(
        model,
        local_model,
        global_guide,
        local_guide,
        S=S,
        T=T,
        adaptation_scheme="dual_averaging",
    )
    svi_result = SVI(model, guide, optim.Adam(0.01), Trace_ELBO()).run(
        random.PRNGKey(0), num_steps
    )
    params = svi_result.params
    np.testing.assert_allclose(params["a_auto_loc"], 1.0, rtol=0.1)
    np.testing.assert_allclose(params["a_auto_scale"], 0.3, rtol=0.1)
    np.testing.assert_allclose(params["b_auto_loc"], 2.0, rtol=0.1)
    np.testing.assert_allclose(params["b_auto_scale"], 0.7, rtol=0.1)
    np.testing.assert_allclose(params["x_auto_loc"], target_loc, rtol=0.1)
    np.testing.assert_allclose(params["x_auto_scale"], 0.4, rtol=0.1)
    np.testing.assert_allclose(params["y_auto_loc"], target_loc, rtol=0.1)
    np.testing.assert_allclose(params["y_auto_scale"], 0.6, rtol=0.1)


def test_custom_local_semirvrs(N=21, T=100.0, subsample_size=10, num_steps=10000):
    target_loc = np.linspace(1.0, 3.0, N)

    def global_model():
        numpyro.sample("a", dist.LogNormal(1.0, 0.3))
        numpyro.sample("b", dist.LogNormal(2.0, 0.7))

    def local_model(_):
        with numpyro.plate("N", N, subsample_size=subsample_size):
            loc = numpyro.subsample(target_loc, event_dim=0)
            numpyro.sample("x", dist.LogNormal(loc, 0.4))
            numpyro.sample("y", dist.LogNormal(loc, 0.6))

    def local_guide(_):
        with numpyro.plate("N", N, subsample_size=subsample_size):
            x_loc = numpyro.param("x_loc", jnp.zeros(N), event_dim=0)
            x_scale = numpyro.param(
                "x_scale",
                0.1 * jnp.ones(N),
                event_dim=0,
                constraint=constraints.positive,
            )
            y_loc = numpyro.param("y_loc", jnp.zeros(N), event_dim=0)
            y_scale = numpyro.param(
                "y_scale",
                0.1 * jnp.ones(N),
                event_dim=0,
                constraint=constraints.positive,
            )
            numpyro.sample("x", dist.LogNormal(x_loc, x_scale))
            numpyro.sample("y", dist.LogNormal(y_loc, y_scale))

    S = 6
    global_guide = AutoNormal(global_model)
    model = lambda: local_model(global_model())
    guide = AutoSemiRVRS(
        model,
        local_model,
        global_guide,
        local_guide,
        S=S,
        T=T,
        adaptation_scheme="dual_averaging",
    )
    svi_result = SVI(model, guide, optim.Adam(0.01), Trace_ELBO()).run(
        random.PRNGKey(0), num_steps
    )
    params = svi_result.params
    np.testing.assert_allclose(params["a_auto_loc"], 1.0, rtol=0.1)
    np.testing.assert_allclose(params["a_auto_scale"], 0.3, rtol=0.1)
    np.testing.assert_allclose(params["b_auto_loc"], 2.0, rtol=0.1)
    np.testing.assert_allclose(params["b_auto_scale"], 0.7, rtol=0.1)
    np.testing.assert_allclose(params["x_loc"], target_loc, rtol=0.1)
    np.testing.assert_allclose(params["x_scale"], 0.4, rtol=0.1)
    np.testing.assert_allclose(params["y_loc"], target_loc, rtol=0.1)
    np.testing.assert_allclose(params["y_scale"], 0.6, rtol=0.1)


def test_autodelta_capture_deterministic_variables():
    def model():
        x = numpyro.sample("x", dist.Normal())
        numpyro.deterministic("x2", x**2)

    guide = AutoDelta(model)
    svi = SVI(model, guide, optim.Adam(0.01), Trace_ELBO())
    svi_result = svi.run(random.PRNGKey(0), num_steps=1_000)
    guide_samples = guide.sample_posterior(
        rng_key=random.PRNGKey(1), params=svi_result.params
    )
    assert "x2" in guide_samples


@pytest.mark.parametrize("shape", [(), (1,), (2, 3)])
@pytest.mark.parametrize("sample_shape", [(), (1,), (2, 3)])
def test_autodelta_sample_posterior_with_sample_shape(shape, sample_shape):
    def model():
        x = numpyro.sample("x", dist.Normal().expand(shape))
        numpyro.deterministic("x2", x**2)

    guide = AutoDelta(model)
    svi = SVI(model, guide, optim.Adam(0.01), Trace_ELBO())
    svi_result = svi.run(random.PRNGKey(0), num_steps=1_000)
    guide_samples = guide.sample_posterior(
        rng_key=random.PRNGKey(1), params=svi_result.params, sample_shape=sample_shape
    )
    assert guide_samples["x"].shape == sample_shape + shape
    assert guide_samples["x2"].shape == sample_shape + shape

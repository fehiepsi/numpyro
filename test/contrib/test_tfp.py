# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

import inspect

from numpy.testing import assert_allclose
import pytest

from jax import random
import jax.numpy as jnp

import numpyro
import numpyro.distributions as dist
from numpyro.infer import MCMC, NUTS


# XXX: for some reasons, pytest raises ImportWarning when we import tfp
@pytest.mark.filterwarnings("ignore:can't resolve package")
def test_api_consistent():
    from numpyro.contrib.tfp import distributions as tfd

    for name in tfd.__all__:
        if name in numpyro.distributions.__all__:
            tfp_dist = getattr(tfd, name)
            numpyro_dist = getattr(numpyro.distributions, name)
            if type(numpyro_dist).__name__ == "function":
                numpyro_dist = getattr(numpyro.distributions, name + "Logits")
            for p in tfp_dist.arg_constraints:
                assert p in dict(inspect.signature(tfp_dist).parameters)


@pytest.mark.filterwarnings("ignore:can't resolve package")
def test_independent():
    from numpyro.contrib.tfp import distributions as tfd

    d = tfd.Independent(tfd.Normal(jnp.zeros(10), jnp.ones(10)), reinterpreted_batch_ndims=1)
    assert d.event_shape == (10,)
    assert d.batch_shape == ()


@pytest.mark.filterwarnings("ignore:can't resolve package")
def test_transformed_distributions():
    from tensorflow_probability.substrates.jax import bijectors as tfb
    from numpyro.contrib.tfp import distributions as tfd

    d = dist.TransformedDistribution(dist.Normal(0, 1), dist.transforms.ExpTransform())
    d1 = tfd.TransformedDistribution(tfd.Normal(0, 1), tfb.Exp())
    d2 = dist.TransformedDistribution(dist.Normal(0, 1), tfd.BijectorTransform(tfb.Exp()))
    x = random.normal(random.PRNGKey(0), (1000,))
    d_x = d.log_prob(x).sum()
    d1_x = d1.log_prob(x).sum()
    d2_x = d2.log_prob(x).sum()
    assert_allclose(d_x, d1_x)
    assert_allclose(d_x, d2_x)


@pytest.mark.filterwarnings("ignore:can't resolve package")
def test_logistic_regression():
    from numpyro.contrib.tfp import distributions as dist

    N, dim = 3000, 3
    num_warmup, num_samples = (1000, 1000)
    data = random.normal(random.PRNGKey(0), (N, dim))
    true_coefs = jnp.arange(1., dim + 1.)
    logits = jnp.sum(true_coefs * data, axis=-1)
    labels = dist.Bernoulli(logits=logits)(rng_key=random.PRNGKey(1))

    def model(labels):
        coefs = numpyro.sample('coefs', dist.Normal(jnp.zeros(dim), jnp.ones(dim)))
        logits = numpyro.deterministic('logits', jnp.sum(coefs * data, axis=-1))
        return numpyro.sample('obs', dist.Bernoulli(logits=logits), obs=labels)

    kernel = NUTS(model)
    mcmc = MCMC(kernel, num_warmup, num_samples)
    mcmc.run(random.PRNGKey(2), labels)
    mcmc.print_summary()
    samples = mcmc.get_samples()
    assert samples['logits'].shape == (num_samples, N)
    assert_allclose(jnp.mean(samples['coefs'], 0), true_coefs, atol=0.22)


@pytest.mark.filterwarnings("ignore:can't resolve package")
# TODO: remove after https://github.com/tensorflow/probability/issues/1072 is resolved
@pytest.mark.filterwarnings("ignore:Explicitly requested dtype")
def test_beta_bernoulli():
    from numpyro.contrib.tfp import distributions as dist

    warmup_steps, num_samples = (500, 2000)

    def model(data):
        alpha = jnp.array([1.1, 1.1])
        beta = jnp.array([1.1, 1.1])
        p_latent = numpyro.sample('p_latent', dist.Beta(alpha, beta))
        numpyro.sample('obs', dist.Bernoulli(p_latent), obs=data)
        return p_latent

    true_probs = jnp.array([0.9, 0.1])
    data = dist.Bernoulli(true_probs)(rng_key=random.PRNGKey(1), sample_shape=(1000, 2))
    kernel = NUTS(model=model, trajectory_length=0.1)
    mcmc = MCMC(kernel, num_warmup=warmup_steps, num_samples=num_samples)
    mcmc.run(random.PRNGKey(2), data)
    mcmc.print_summary()
    samples = mcmc.get_samples()
    assert_allclose(jnp.mean(samples['p_latent'], 0), true_probs, atol=0.05)

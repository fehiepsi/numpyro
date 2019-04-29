import pytest
from numpy.testing import assert_allclose

import jax.numpy as np
from jax import random

import numpyro.distributions as dist
from numpyro.handlers import sample
from numpyro.hmc_util import initialize_model
from numpyro.mcmc import hmc
from numpyro.util import fori_append, fori_collect


# TODO: add test for diag_mass=False
@pytest.mark.parametrize('algo', ['HMC', 'NUTS'])
def test_unnormalized_normal(algo):
    true_mean, true_std = 1., 2.
    warmup_steps, num_samples = 1000, 8000

    def potential_fn(z):
        return 0.5 * np.sum(((z - true_mean) / true_std) ** 2)

    init_kernel, sample_kernel = hmc(potential_fn, algo=algo)
    init_samples = np.array(0.)
    hmc_state = init_kernel(init_samples,
                            trajectory_length=10,
                            num_warmup_steps=warmup_steps)
    hmc_states = fori_append(sample_kernel, hmc_state, num_samples,
                             transform=lambda x: x.z)
    assert_allclose(np.mean(hmc_states), true_mean, rtol=0.05)
    assert_allclose(np.std(hmc_states), true_std, rtol=0.05)


@pytest.mark.parametrize('algo', ['HMC', 'NUTS'])
def test_logistic_regression(algo):
    N, dim = 3000, 3
    warmup_steps, num_samples = 1000, 8000
    data = random.normal(random.PRNGKey(0), (N, dim))
    true_coefs = np.arange(1., dim + 1.)
    logits = np.sum(true_coefs * data, axis=-1)
    labels = dist.bernoulli(logits, is_logits=True).rvs(random_state=random.PRNGKey(1))

    def model(labels):
        coefs = sample('coefs', dist.norm(np.zeros(dim), np.ones(dim)))
        logits = np.sum(coefs * data, axis=-1)
        return sample('obs', dist.bernoulli(logits, is_logits=True), obs=labels)

    init_params, potential_fn, transform_fn = initialize_model(random.PRNGKey(2), model, (labels,), {})
    init_kernel, sample_kernel = hmc(potential_fn, algo=algo)
    hmc_state = init_kernel(init_params,
                            trajectory_length=10,
                            num_warmup_steps=warmup_steps)
    hmc_states = fori_append(sample_kernel, hmc_state, num_samples,
                             transform=lambda x: transform_fn(x.z))
    assert_allclose(np.mean(hmc_states['coefs'], 0), true_coefs, atol=0.2)


@pytest.mark.parametrize('algo', ['HMC', 'NUTS'])
@pytest.mark.parametrize('fori_method', ['append', 'collect'])
def test_beta_bernoulli(algo, fori_method):
    warmup_steps, num_samples = 500, 20000

    def model(data):
        alpha = np.array([1.1, 1.1])
        beta = np.array([1.1, 1.1])
        p_latent = sample('p_latent', dist.beta(alpha, beta))
        sample('obs', dist.bernoulli(p_latent), obs=data)
        return p_latent

    true_probs = np.array([0.9, 0.1])
    data = dist.bernoulli(true_probs).rvs(size=(1000, 2), random_state=random.PRNGKey(1))
    init_params, potential_fn, transform_fn = initialize_model(random.PRNGKey(2), model, (data,), {})
    init_kernel, sample_kernel = hmc(potential_fn, algo=algo)
    hmc_state = init_kernel(init_params,
                            trajectory_length=1.,
                            num_warmup_steps=warmup_steps)
    if fori_method == 'append':
        hmc_states = fori_append(sample_kernel, hmc_state, num_samples,
                                 transform=lambda x: transform_fn(x.z))
    else:
        hmc_states = fori_collect(num_samples, sample_kernel, hmc_state,
                                  transform=lambda x: transform_fn(x.z))
    assert_allclose(np.mean(hmc_states['p_latent'], 0), true_probs, atol=0.05)


@pytest.mark.parametrize('algo', ['HMC', 'NUTS'])
@pytest.mark.parametrize('fori_method', ['append', 'collect'])
def test_dirichlet_categorical(algo, fori_method):
    warmup_steps, num_samples = 100, 20000

    def model(data):
        concentration = np.array([1.0, 1.0, 1.0])
        p_latent = sample('p_latent', dist.dirichlet(alpha=concentration))
        sample("obs", dist.categorical(p=p_latent), obs=data)
        return p_latent

    true_probs = np.array([0.1, 0.6, 0.3])
    data = dist.categorical(p=true_probs).rvs(size=(2000,), random_state=random.PRNGKey(1))
    init_params, potential_fn, transform_fn = initialize_model(random.PRNGKey(2), model, (data,), {})
    init_kernel, sample_kernel = hmc(potential_fn, algo=algo)
    hmc_state = init_kernel(init_params,
                            trajectory_length=1.,
                            num_warmup_steps=warmup_steps)
    if fori_method == 'append':
        hmc_states = fori_append(sample_kernel, hmc_state, num_samples,
                                 transform=lambda x: transform_fn(x.z))
    else:
        hmc_states = fori_collect(num_samples, sample_kernel, hmc_state,
                                  transform=lambda x: transform_fn(x.z))
    assert_allclose(np.mean(hmc_states['p_latent'], 0), true_probs, atol=0.02)

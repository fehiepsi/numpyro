import pytest
from numpy.testing import assert_allclose

import jax.numpy as np
import jax.random as random
from jax.scipy.special import expit

import numpyro.distributions as dist
from numpyro.distributions.util import validation_disabled
from numpyro.mcmc import hmc_kernel
from numpyro.util import tscan


@pytest.mark.parametrize('algo', ['HMC', 'NUTS'])
def test_unnormalized_normal(algo):
    true_mean, true_std = 1., 2.
    warmup_steps, num_samples = 1000, 8000

    def potential_fn(z):
        return 0.5 * np.sum(((z - true_mean) / true_std) ** 2)

    def kinetic_fn(r, m_inv):
        return 0.5 * np.sum(m_inv * r ** 2)

    init_kernel, sample_kernel = hmc_kernel(potential_fn, kinetic_fn, algo)
    init_samples = np.array(0.)
    hmc_state = init_kernel(init_samples,
                            num_steps=10,
                            num_warmup_steps=warmup_steps)
    hmc_states = tscan(lambda state, i: sample_kernel(state), hmc_state, np.arange(num_samples))
    assert_allclose(np.mean(hmc_states.z), true_mean, rtol=0.05)
    assert_allclose(np.std(hmc_states.z), true_std, rtol=0.05)


@pytest.mark.parametrize('algo', ['HMC', 'NUTS'])
def test_logistic_regression(algo):
    N, dim = 3000, 3
    warmup_steps, num_samples = 1000, 8000
    data = random.normal(random.PRNGKey(0), (N, dim))
    true_coefs = np.arange(1., dim + 1.)
    probs = expit(np.sum(true_coefs * data, axis=-1))
    labels = dist.bernoulli(probs).rvs(random_state=random.PRNGKey(0))

    with validation_disabled():
        def potential_fn(beta):
            coefs_mean = np.zeros(dim)
            coefs_lpdf = dist.norm(coefs_mean, np.ones(dim)).logpdf(beta)
            logits = np.sum(beta * data, axis=-1)
            y_lpdf = dist.bernoulli(logits, is_logits=True).logpmf(labels)
            return - (np.sum(coefs_lpdf) + np.sum(y_lpdf))

        def kinetic_fn(beta, m_inv):
            return 0.5 * np.dot(m_inv, beta ** 2)

        init_kernel, sample_kernel = hmc_kernel(potential_fn, kinetic_fn, algo)
        init_samples = np.zeros(dim)
        hmc_state = init_kernel(init_samples,
                                step_size=0.1,
                                num_steps=15,
                                num_warmup_steps=warmup_steps)
        hmc_states = tscan(lambda state, i: sample_kernel(state),
                           hmc_state, np.arange(num_samples))
        assert_allclose(np.mean(hmc_states.z, 0), true_coefs, atol=0.2)

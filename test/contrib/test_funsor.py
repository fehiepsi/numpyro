# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from collections import OrderedDict

import numpy as np
from numpy.testing import assert_allclose

from jax import random
import jax.numpy as jnp

from funsor import Tensor, bint, reals
import numpyro
from numpyro.contrib.funsor.enum_messenger import NamedMessenger, markov, to_data, to_funsor
import numpyro.distributions as dist
from numpyro.infer import MCMC, NUTS


def test_gaussian_mixture_model():
    K, N = 3, 1000

    def gmm(data):
        mix_proportions = numpyro.sample("phi", dist.Dirichlet(jnp.ones(K)))
        with numpyro.plate("num_clusters", K, dim=-1):
            cluster_means = numpyro.sample("cluster_means", dist.Normal(jnp.arange(K), 1.))
        with numpyro.plate("data", data.shape[0], dim=-1):
            assignments = numpyro.sample("assignments", dist.Categorical(mix_proportions))
            numpyro.sample("obs", dist.Normal(cluster_means[assignments], 1.), obs=data)

    true_cluster_means = jnp.array([1., 5., 10.])
    true_mix_proportions = jnp.array([0.1, 0.3, 0.6])
    cluster_assignments = dist.Categorical(true_mix_proportions).sample(random.PRNGKey(0), (N,))
    data = dist.Normal(true_cluster_means[cluster_assignments], 1.0).sample(random.PRNGKey(1))

    nuts_kernel = NUTS(gmm)
    mcmc = MCMC(nuts_kernel, num_warmup=500, num_samples=500)
    mcmc.run(random.PRNGKey(2), data)
    samples = mcmc.get_samples()
    assert_allclose(samples["phi"].mean(0).sort(), true_mix_proportions, atol=0.05)
    assert_allclose(samples["cluster_means"].mean(0).sort(), true_cluster_means, atol=0.2)


def test_bernoulli_latent_model():
    def model(data):
        y_prob = numpyro.sample("y_prob", dist.Beta(1., 1.))
        with numpyro.plate("data", data.shape[0]):
            y = numpyro.sample("y", dist.Bernoulli(y_prob))
            z = numpyro.sample("z", dist.Bernoulli(0.65 * y + 0.1))
            numpyro.sample("obs", dist.Normal(2. * z, 1.), obs=data)

    N = 2000
    y_prob = 0.3
    y = dist.Bernoulli(y_prob).sample(random.PRNGKey(0), (N,))
    z = dist.Bernoulli(0.65 * y + 0.1).sample(random.PRNGKey(1))
    data = dist.Normal(2. * z, 1.0).sample(random.PRNGKey(2))

    nuts_kernel = NUTS(model)
    mcmc = MCMC(nuts_kernel, num_warmup=500, num_samples=500)
    mcmc.run(random.PRNGKey(3), data)
    samples = mcmc.get_samples()
    assert_allclose(samples["y_prob"].mean(0), y_prob, atol=0.05)


def test_change_point():
    def model(count_data):
        n_count_data = count_data.shape[0]
        alpha = 1 / jnp.mean(count_data)
        lambda_1 = numpyro.sample('lambda_1', dist.Exponential(alpha))
        lambda_2 = numpyro.sample('lambda_2', dist.Exponential(alpha))
        # this is the same as DiscreteUniform(0, 69)
        tau = numpyro.sample('tau', dist.Categorical(logits=jnp.zeros(70)))
        idx = jnp.arange(n_count_data)
        lambda_ = jnp.where(tau > idx, lambda_1, lambda_2)
        with numpyro.plate("data", n_count_data):
            numpyro.sample('obs', dist.Poisson(lambda_), obs=count_data)

    count_data = jnp.array([
        13, 24, 8, 24,  7, 35, 14, 11, 15, 11, 22, 22, 11, 57, 11,
        19, 29, 6, 19, 12, 22, 12, 18, 72, 32,  9,  7, 13, 19, 23,
        27, 20, 6, 17, 13, 10, 14,  6, 16, 15,  7,  2, 15, 15, 19,
        70, 49, 7, 53, 22, 21, 31, 19, 11,  1, 20, 12, 35, 17, 23,
        17,  4, 2, 31, 30, 13, 27,  0, 39, 37,  5, 14, 13, 22,
    ])

    kernel = NUTS(model)
    mcmc = MCMC(kernel, num_warmup=500, num_samples=500)
    mcmc.run(random.PRNGKey(0), count_data)
    samples = mcmc.get_samples()
    assert_allclose(samples["lambda_1"].mean(0), 18., atol=1.)
    assert_allclose(samples["lambda_2"].mean(0), 23., atol=1.)


def test_gaussian_hmm():
    dim = 4
    num_steps = 10

    def model(data):
        with numpyro.plate("states", dim):
            transition = numpyro.sample("transition", dist.Dirichlet(jnp.ones(dim)))
            emission_loc = numpyro.sample("emission_loc", dist.Normal(0, 1))
            emission_scale = numpyro.sample("emission_scale", dist.LogNormal(0, 1))

        trans_prob = numpyro.sample("initialize", dist.Dirichlet(jnp.ones(dim)))
        for t, y in markov(enumerate(data)):
            x = numpyro.sample("x_{}".format(t), dist.Categorical(trans_prob))
            numpyro.sample("y_{}".format(t), dist.Normal(emission_loc[x], emission_scale[x]), obs=y)
            trans_prob = transition[x]

    def _generate_data():
        transition_probs = np.random.rand(dim, dim)
        transition_probs = transition_probs / transition_probs.sum(-1, keepdims=True)
        emissions_loc = np.arange(dim)
        emissions_scale = 1.
        state = np.random.choice(3)
        obs = [np.random.normal(emissions_loc[state], emissions_scale)]
        for _ in range(num_steps - 1):
            state = np.random.choice(dim, p=transition_probs[state])
            obs.append(np.random.normal(emissions_loc[state], emissions_scale))
        return np.stack(obs)

    data = _generate_data()
    nuts_kernel = NUTS(model)
    mcmc = MCMC(nuts_kernel, num_warmup=500, num_samples=500)
    mcmc.run(random.PRNGKey(0), data)


def test_iteration():

    def testing():
        for i in markov(range(5)):
            v1 = to_data(Tensor(jnp.ones(2), OrderedDict([(str(i), bint(2))]), 'real'))
            v2 = to_data(Tensor(jnp.zeros(2), OrderedDict([('a', bint(2))]), 'real'))
            fv1 = to_funsor(v1, reals())
            fv2 = to_funsor(v2, reals())
            print(i, v1.shape)  # shapes should alternate
            if i % 2 == 0:
                assert v1.shape == (2,)
            else:
                assert v1.shape == (2, 1, 1)
            assert v2.shape == (2, 1)
            print(i, fv1.inputs)
            print('a', v2.shape)  # shapes should stay the same
            print('a', fv2.inputs)

    with NamedMessenger():
        testing()


def test_nesting():

    def testing():

        with markov():
            v1 = to_data(Tensor(jnp.ones(2), OrderedDict([("1", bint(2))]), 'real'))
            print(1, v1.shape)  # shapes should alternate
            assert v1.shape == (2,)

            with markov():
                v2 = to_data(Tensor(jnp.ones(2), OrderedDict([("2", bint(2))]), 'real'))
                print(2, v2.shape)  # shapes should alternate
                assert v2.shape == (2, 1)

                with markov():
                    v3 = to_data(Tensor(jnp.ones(2), OrderedDict([("3", bint(2))]), 'real'))
                    print(3, v3.shape)  # shapes should alternate
                    assert v3.shape == (2,)

                    with markov():
                        v4 = to_data(Tensor(jnp.ones(2), OrderedDict([("4", bint(2))]), 'real'))
                        print(4, v4.shape)  # shapes should alternate

                        assert v4.shape == (2, 1)

    with NamedMessenger():
        testing()


def test_staggered():

    def testing():
        for i in markov(range(12)):
            if i % 4 == 0:
                v2 = to_data(Tensor(jnp.zeros(2), OrderedDict([('a', bint(2))]), 'real'))
                fv2 = to_funsor(v2, reals())
                assert v2.shape == (2,)
                print('a', v2.shape)
                print('a', fv2.inputs)

    with NamedMessenger():
        testing()

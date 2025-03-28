# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from functools import partial

import numpy as np
from numpy.testing import assert_allclose
import pytest

from jax import jacfwd, random
from jax.example_libraries import stax
import jax.numpy as jnp
import jax.random as jr

from numpyro.distributions import Normal, TransformedDistribution
from numpyro.distributions.flows import (
    BlockNeuralAutoregressiveTransform,
    InverseAutoregressiveTransform,
)
from numpyro.distributions.util import matrix_to_tril_vec
from numpyro.nn import AutoregressiveNN, BlockNeuralAutoregressiveNN
from numpyro.nn.block_neural_arn import LeakyTanh, Tanh


def _make_iaf_args(input_dim, hidden_dims):
    _, rng_perm = random.split(random.PRNGKey(0))
    perm = random.permutation(rng_perm, np.arange(input_dim))
    # we use Elu nonlinearity because the default one, Relu, masks out negative hidden values,
    # which in turn create some zero entries in the lower triangular part of Jacobian.
    arn_init, arn = AutoregressiveNN(
        input_dim,
        hidden_dims,
        param_dims=[1, 1],
        permutation=perm,
        nonlinearity=stax.Elu,
    )
    _, init_params = arn_init(random.PRNGKey(0), (input_dim,))
    return (partial(arn, init_params),)


def _make_bnaf_args(input_dim, hidden_factors, activation):
    arn_init, arn = BlockNeuralAutoregressiveNN(
        input_dim,
        hidden_factors,
        activation=activation,
    )
    _, rng_key_perm = random.split(random.PRNGKey(0))
    _, init_params = arn_init(random.PRNGKey(0), (input_dim,))
    return (partial(arn, init_params),)


@pytest.mark.parametrize(
    "flow_class, flow_args, input_dim",
    [
        (
            InverseAutoregressiveTransform,
            lambda: _make_iaf_args(5, hidden_dims=[10]),
            5,
        ),
        (
            InverseAutoregressiveTransform,
            lambda: _make_iaf_args(7, hidden_dims=[8, 9]),
            7,
        ),
        (
            BlockNeuralAutoregressiveTransform,
            lambda: _make_bnaf_args(7, hidden_factors=[4], activation=LeakyTanh()),
            7,
        ),
        (
            BlockNeuralAutoregressiveTransform,
            lambda: _make_bnaf_args(7, hidden_factors=[2, 3], activation=LeakyTanh()),
            7,
        ),
        (
            BlockNeuralAutoregressiveTransform,
            lambda: _make_bnaf_args(7, hidden_factors=[4], activation=Tanh()),
            7,
        ),
        (
            BlockNeuralAutoregressiveTransform,
            lambda: _make_bnaf_args(7, hidden_factors=[2, 3], activation=Tanh()),
            7,
        ),
    ],
)
@pytest.mark.parametrize("batch_shape", [(), (1,), (4,), (2, 3)])
def test_flows(flow_class, flow_args, input_dim, batch_shape):
    flow_args = flow_args()
    transform = flow_class(*flow_args)
    x = random.normal(random.PRNGKey(0), batch_shape + (input_dim,))

    # test inverse is correct
    y = transform(x)
    try:
        inv = transform.inv(y)
        assert_allclose(x, inv, atol=1e-5)
    except NotImplementedError:
        pass

    # test jacobian shape
    actual = transform.log_abs_det_jacobian(x, y)
    assert np.shape(actual) == batch_shape

    if batch_shape == ():
        # make sure transform.log_abs_det_jacobian is correct
        jac = jacfwd(transform)(x)
        expected = np.linalg.slogdet(jac)[1]
        assert_allclose(actual, expected, atol=1e-5)

        # make sure jacobian is triangular, first permute jacobian as necessary
        if isinstance(transform, InverseAutoregressiveTransform):
            permuted_jac = np.zeros(jac.shape)
            _, rng_key_perm = random.split(random.PRNGKey(0))
            perm = random.permutation(rng_key_perm, np.arange(input_dim))

            for j in range(input_dim):
                for k in range(input_dim):
                    permuted_jac[j, k] = jac[perm[j], perm[k]]

            jac = permuted_jac

        assert np.sum(np.abs(np.triu(jac, 1))) == 0.00
        assert np.all(np.abs(matrix_to_tril_vec(jac)) > 0)


def test_bnaf_normalization():
    dim = (1,)
    x = jnp.linspace(-1000, 1000, 5000)[:, None]

    init_fn, apply_fn = BlockNeuralAutoregressiveNN(dim[0], activation=LeakyTanh(0.1))
    params = init_fn(jr.PRNGKey(0), (1,))[1]
    arn = partial(apply_fn, params)
    bnaf = BlockNeuralAutoregressiveTransform(arn)
    dist = TransformedDistribution(Normal(jnp.zeros(dim), 0.5), bnaf.inv)
    probs = jnp.exp(dist.log_prob(x))
    probs, x = jnp.squeeze(probs), jnp.squeeze(x)

    # Rough integral
    integral = jnp.trapezoid(probs, x)
    assert integral > 0.9
    assert integral < 1.1

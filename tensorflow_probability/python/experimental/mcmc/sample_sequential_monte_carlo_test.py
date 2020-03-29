# Copyright 2020 The TensorFlow Probability Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ============================================================================
"""Tests for MCMC driver, `sample_sequential_monte_carlo`."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

import numpy as np
import tensorflow.compat.v2 as tf
import tensorflow_probability as tfp
from tensorflow_probability.python.distributions.internal import statistical_testing as st
from tensorflow_probability.python.experimental.mcmc.sample_sequential_monte_carlo import gen_make_transform_hmc_kernel_fn
from tensorflow_probability.python.internal import test_util

tfb = tfp.bijectors
tfd = tfp.distributions


@test_util.test_all_tf_execution_regimes
class SampleSequentialMonteCarloTest(test_util.TestCase):

  def testMixtureTargetLogProb(self):
    seed = test_util.test_seed()
    n = 4
    mu = np.ones(n) * (1. / 2)
    w = 0.1

    proposal = tfd.Sample(tfd.Normal(0., 10.), sample_shape=n)
    init_state = proposal.sample(5000, seed=seed)

    likelihood_dist = tfd.MixtureSameFamily(
        mixture_distribution=tfd.Categorical(probs=[w, 1. - w]),
        components_distribution=tfd.MultivariateNormalDiag(
            loc=np.asarray([mu, -mu]).astype(np.float32),
            scale_identity_multiplier=[.1, .2]))

    # Uniform prior
    init_log_prob = tf.zeros_like(proposal.log_prob(init_state))

    [
        n_stage, final_state, _
    ] = tfp.experimental.mcmc.sample_sequential_monte_carlo(
        lambda x: init_log_prob,
        likelihood_dist.log_prob,
        init_state,
        max_num_steps=50,
        parallel_iterations=1,
        seed=seed)

    self.assertTrue(self.evaluate(n_stage), 15)

    self.evaluate(
        st.kolmogorov_smirnov_distance_two_sample(
            final_state, likelihood_dist.sample(5000, seed=seed)))

  # TODO(junpenglao) Enable this test.
  def DISABLED_testSampleEndtoEndXLA(self):
    """An end-to-end test of sampling using SMC."""
    if tf.executing_eagerly() or tf.config.experimental_functions_run_eagerly():
      self.skipTest('No need to test XLA under all execution regimes.')

    seed = test_util.test_seed()
    dtype = tf.float32
    # Set up data.
    predictors = np.asarray([
        201., 244., 47., 287., 203., 58., 210., 202., 198., 158., 165., 201.,
        157., 131., 166., 160., 186., 125., 218., 146.
    ])
    obs = np.asarray([
        592., 401., 583., 402., 495., 173., 479., 504., 510., 416., 393., 442.,
        317., 311., 400., 337., 423., 334., 533., 344.
    ])
    y_sigma = np.asarray([
        61., 25., 38., 15., 21., 15., 27., 14., 30., 16., 14., 25., 52., 16.,
        34., 31., 42., 26., 16., 22.
    ])
    y_sigma = tf.cast(y_sigma / (2 * obs.std(axis=0)), dtype)
    obs = tf.cast((obs - obs.mean(axis=0)) / (2 * obs.std(axis=0)), dtype)
    predictors = tf.cast(
        (predictors - predictors.mean(axis=0)) / (2 * predictors.std(axis=0)),
        dtype)

    hyper_mean = tf.cast(0, dtype)
    hyper_scale = tf.cast(10, dtype)
    # Generate model prior_log_prob_fn and likelihood_log_prob_fn.
    prior_jd = tfd.JointDistributionSequential([
        tfd.Normal(loc=hyper_mean, scale=hyper_scale),
        tfd.Normal(loc=hyper_mean, scale=hyper_scale),
        tfd.Normal(loc=hyper_mean, scale=hyper_scale),
        tfd.HalfNormal(scale=tf.cast(1., dtype)),
        tfd.Uniform(low=tf.cast(0, dtype), high=.5),
    ], validate_args=True)

    def likelihood_log_prob_fn(b0, b1, mu_out, sigma_out, weight):
      return tfd.Independent(
          tfd.Mixture(
              tfd.Categorical(
                  probs=tf.stack([
                      tf.repeat(1 - weight[..., tf.newaxis], 20, axis=-1),
                      tf.repeat(weight[..., tf.newaxis], 20, axis=-1)
                  ], -1)), [
                      tfd.Normal(
                          loc=b0[..., tf.newaxis] +
                          b1[..., tf.newaxis] * predictors,
                          scale=y_sigma),
                      tfd.Normal(
                          loc=mu_out[..., tf.newaxis],
                          scale=y_sigma + sigma_out[..., tf.newaxis])
                  ]), 1).log_prob(obs)

    unconstraining_bijectors = [
        tfb.Identity(),
        tfb.Identity(),
        tfb.Identity(),
        tfb.Exp(),
        tfb.Sigmoid(tf.constant(0., dtype), .5),
    ]
    make_transform_hmc_kernel_fn = gen_make_transform_hmc_kernel_fn(
        unconstraining_bijectors, num_leapfrog_steps=10)

    @tf.function(autograph=False, experimental_compile=True)
    def run_smc():
      # Ensure we're really in graph mode.
      assert hasattr(tf.constant([]), 'graph')

      return tfp.experimental.mcmc.sample_sequential_monte_carlo(
          prior_jd.log_prob,
          likelihood_log_prob_fn,
          prior_jd.sample(1000, seed=seed),
          make_kernel_fn=make_transform_hmc_kernel_fn,
          optimal_accept=0.7,
          max_num_steps=50,
          parallel_iterations=1,
          seed=seed)

    n_stage, (b0, b1, mu_out, sigma_out, weight), _ = run_smc()

    self.assertTrue(self.evaluate(n_stage), 15)

    # Compare the SMC posterior with the result from a carefully calibrated HMC.
    self.assertAllClose(tf.reduce_mean(b0), 0.016, atol=0.005, rtol=0.005)
    self.assertAllClose(tf.reduce_mean(b1), 1.245, atol=0.005, rtol=0.005)
    self.assertAllClose(tf.reduce_mean(weight), 0.27, atol=0.01, rtol=0.01)
    self.assertAllClose(tf.reduce_mean(mu_out), 0.13, atol=0.1, rtol=0.1)
    self.assertAllClose(tf.reduce_mean(sigma_out), 0.46, atol=0.5, rtol=0.5)

    self.assertAllClose(tf.math.reduce_std(b0), 0.031, atol=0.005, rtol=0.005)
    self.assertAllClose(tf.math.reduce_std(b1), 0.068, atol=0.005, rtol=0.005)
    self.assertAllClose(tf.math.reduce_std(weight), 0.1, atol=0.01, rtol=0.01)


if __name__ == '__main__':
  tf.test.main()

import logging
logging.disable(logging.CRITICAL)
import numpy as np
import scipy as sp
import scipy.sparse.linalg as spLA
import copy
import time as timer
import torch
import torch.nn as nn
from torch.autograd import Variable
import copy

# samplers
import mjrl.samplers.trajectory_sampler as trajectory_sampler
import mjrl.samplers.batch_sampler as batch_sampler

# utility functions
import mjrl.utils.process_samples as process_samples
from mjrl.utils.logger import DataLog
from mjrl.utils.cg_solve import cg_solve

# Import NPG
from mjrl.algos.npg_cg_delta import NPG
from adacurv.torch.utils.convert_gradients import gradients_to_vector, vector_to_gradients

class TRPO(NPG):
    def __init__(self, env, policy, baseline, optim,
                 kl_dist=0.01,
                 FIM_invert_args={'iters': 10, 'damping': 1e-4},
                 hvp_sample_frac=1.0,
                 seed=None,
                 save_logs=False,
                 normalized_step_size=0.01):
        """
        All inputs are expected in mjrl's format unless specified
        :param normalized_step_size: Normalized step size (under the KL metric). Twice the desired KL distance
        :param kl_dist: desired KL distance between steps. Overrides normalized_step_size.
        :param const_learn_rate: A constant learn rate under the L2 metric (won't work very well)
        :param FIM_invert_args: {'iters': # cg iters, 'damping': regularization amount when solving with CG
        :param hvp_sample_frac: fraction of samples (>0 and <=1) to use for the Fisher metric (start with 1 and reduce if code too slow)
        :param seed: random seed
        """

        self.env = env
        self.policy = policy
        self.baseline = baseline
        self.optim = optim
        self.kl_dist = kl_dist if kl_dist is not None else 0.5*normalized_step_size
        self.seed = seed
        self.save_logs = save_logs
        self.FIM_invert_args = FIM_invert_args
        self.hvp_subsample = hvp_sample_frac
        self.running_score = None
        self.n_steps = 0
        if save_logs: self.logger = DataLog()

    def train_from_paths(self, paths):

        # Concatenate from all the trajectories
        observations = np.concatenate([path["observations"] for path in paths])
        actions = np.concatenate([path["actions"] for path in paths])
        advantages = np.concatenate([path["advantages"] for path in paths])
        # Advantage whitening
        advantages = (advantages - np.mean(advantages)) / (np.std(advantages) + 1e-6)
        # NOTE : advantage should be zero mean in expectation
        # normalized step size invariant to advantage scaling,
        # but scaling can help with least squares

        self.n_steps += len(advantages)

        # cache return distributions for the paths
        path_returns = [sum(p["rewards"]) for p in paths]
        mean_return = np.mean(path_returns)
        std_return = np.std(path_returns)
        min_return = np.amin(path_returns)
        max_return = np.amax(path_returns)
        base_stats = [mean_return, std_return, min_return, max_return]
        self.running_score = mean_return if self.running_score is None else \
                             0.9*self.running_score + 0.1*mean_return  # approx avg of last 10 iters
        if self.save_logs: self.log_rollout_statistics(paths)

        # Keep track of times for various computations
        t_gLL = 0.0
        t_FIM = 0.0

        # Optimization algorithm
        # --------------------------
        self.optim.zero_grad()

        surr_before = self.CPI_surrogate(observations, actions, advantages).data.numpy().ravel()[0]

        # VPG
        ts = timer.time()
        vpg_grad = self.flat_vpg(observations, actions, advantages)
        vector_to_gradients(Variable(torch.from_numpy(vpg_grad).float()), self.policy.trainable_params)
        t_gLL += timer.time() - ts

        # NPG
        # Note: unlike the standard NPG, negation is not needed here since the optimizer does not
        # apply the update step.
        ts = timer.time()
        closure = self.kl_closure(self.policy, observations, actions, self.policy_kl_fn)
        info = self.optim.step(closure, execute_update=False)
        npg_grad = info['natural_grad'].data.numpy()
        t_FIM += timer.time() - ts

        # Step size computation
        # --------------------------
        n_step_size = 2.0 * self.kl_dist
        alpha = np.sqrt(np.abs(n_step_size / (np.dot(vpg_grad.T, npg_grad) + 1e-20)))

        # Policy update
        # --------------------------
        curr_params = self.policy.get_param_values()
        for k in range(100):
            new_params = curr_params + alpha * npg_grad
            self.policy.set_param_values(new_params, set_new=True, set_old=False)
            kl_dist = self.kl_old_new(observations, actions).data.numpy().ravel()[0]
            surr_after = self.CPI_surrogate(observations, actions, advantages).data.numpy().ravel()[0]
            if kl_dist < self.kl_dist:
                break
            else:
                alpha = 0.9*alpha # backtrack
                # print("Step size too high. Backtracking. | kl = %f | surr diff = %f" % \
                      # (kl_dist, surr_after-surr_before) )
            if k == 99:
                alpha = 0.0

        new_params = curr_params + alpha * npg_grad
        self.policy.set_param_values(new_params, set_new=True, set_old=False)
        kl_dist = self.kl_old_new(observations, actions).data.numpy().ravel()[0]
        surr_after = self.CPI_surrogate(observations, actions, advantages).data.numpy().ravel()[0]
        self.policy.set_param_values(new_params, set_new=True, set_old=True)

        # Log information
        if self.save_logs:
            self.logger.log_kv('alpha', alpha)
            self.logger.log_kv('delta', n_step_size)
            self.logger.log_kv('time_vpg', t_gLL)
            self.logger.log_kv('time_npg', t_FIM)
            self.logger.log_kv('kl_dist', kl_dist)
            self.logger.log_kv('surr_improvement', surr_after - surr_before)
            self.logger.log_kv('running_score', self.running_score)
            self.logger.log_kv('steps', self.n_steps)


        return base_stats

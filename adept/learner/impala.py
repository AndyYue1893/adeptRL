# Copyright (C) 2018 Heron Systems, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.
import torch

from adept.utils.util import listd_to_dlist, dlist_to_listd

from .base import LearnerModule
from .base.dm_return_scale import DeepMindReturnScaler


class ImpalaLearner(LearnerModule):
    """
    Reference implementation:
    Use https://github.com/deepmind/scalable_agent/blob/master/vtrace.py
    """
    args = {
        'discount': 0.99,
        'minimum_importance_value': 1.0,
        'minimum_importance_policy': 1.0,
        'entropy_weight': 0.01
    }

    def __init__(
        self,
        discount,
        minimum_importance_value,
        minimum_importance_policy,
        entropy_weight
    ):
        self.discount = discount
        self.minimum_importance_value = minimum_importance_value
        self.minimum_importance_policy = minimum_importance_policy
        self.entropy_weight = entropy_weight

    @classmethod
    def from_args(cls, args):
        return cls(
            discount=args.discount,
            minimum_importance_value=args.minimum_importance_value,
            minimum_importance_policy=args.minimum_importance_policy,
            entropy_weight=args.entropy_weight
        )

    def compute_loss(self, network, experiences, next_obs, internals):
        # estimate value of next state
        with torch.no_grad():
            results, _ = network(next_obs, internals)
            b_last_values = results['critic'].squeeze(1).data

        # Gather host log_probs
        r_log_probs = []
        for action, log_softs in zip(experiences.actions, experiences.log_softmaxes):
            b_log_probs = []
            for act_tensor, log_soft in zip(action.values(), log_softs.unbind(1)):
                log_prob = log_soft.gather(1, act_tensor.unsqueeze(1))
                b_log_probs.append(log_prob)
            r_log_probs.append(torch.stack(b_log_probs))

        r_log_probs_learner = torch.stack(r_log_probs)
        r_log_probs_actor = torch.stack(experiences.log_probs)
        r_rewards = torch.stack(experiences.rewards)
        r_values = torch.stack(experiences.values)
        r_terminals = torch.stack(experiences.terminals)
        r_entropies = torch.stack(experiences.entropies)
        r_dterminal_masks = self.discount * (1. - r_terminals.float())

        with torch.no_grad():
            r_log_diffs = r_log_probs_learner - r_log_probs_actor
            vtrace_target, pg_advantage, importance = self._vtrace_returns(
                r_log_diffs, r_dterminal_masks, r_rewards, r_values,
                b_last_values, self.minimum_importance_value,
                self.minimum_importance_policy
            )

        # using torch.no_grad so detach is unnecessary
        value_loss = 0.5 * torch.mean(
            (vtrace_target - r_values).pow(2)
        )
        policy_loss = torch.mean(-r_log_probs_learner * pg_advantage)
        entropy_loss = torch.mean(-r_entropies) * self.entropy_weight

        losses = {
            'value_loss': value_loss,
            'policy_loss': policy_loss,
            'entropy_loss': entropy_loss
        }
        metrics = {'importance': importance.mean()}
        return losses, metrics

    @staticmethod
    def _vtrace_returns(
        log_prob_diffs, discount_terminal_mask, rewards, values,
        estimated_value, minimum_importance_value, minimum_importance_policy
    ):
        """
        :param log_prob_diffs:
        :param discount_terminal_mask: should be shape [seq, batch] of
        discount * (1 - terminal)
        :param rewards:
        :param values:
        :param estimated_value:
        :param minimum_importance_value:
        :param minimum_importance_policy:
        :return:
        """
        importance = torch.exp(log_prob_diffs)
        clamped_importance_value = importance.clamp(
            max=minimum_importance_value
        )
        # if multiple actions take the average, (dim 3 is seq, batch, # actions)
        if clamped_importance_value.dim() == 3:
            clamped_importance_value = clamped_importance_value.mean(-1)

        # create nstep vtrace return
        # first create d_tV of function 1 in the paper
        values_t_plus_1 = torch.cat((values[1:], estimated_value.unsqueeze(0)))
        diff_value_per_step = clamped_importance_value * (
            rewards + discount_terminal_mask * values_t_plus_1 - values
        )

        # reverse over the values to create the summed importance weighted
        # return everything on the right side of the plus in function 1 of
        # the paper
        vs_minus_v_xs = []
        nstep_v = 0.0
        # TODO: this uses a different clamping if != 1
        if minimum_importance_policy != 1 or minimum_importance_value != 1:
            raise NotImplementedError()

        for i in reversed(range(diff_value_per_step.shape[0])):
            nstep_v = diff_value_per_step[i] + discount_terminal_mask[
                i] * clamped_importance_value[i] * nstep_v
            vs_minus_v_xs.append(nstep_v)
        # reverse to a forward in time list
        vs_minus_v_xs = torch.stack(list(reversed(vs_minus_v_xs)))

        # Add V(s) to finish computation of v_s
        v_s = values + vs_minus_v_xs

        # advantage is pg_importance * (v_s of t+1 - values)
        clamped_importance_pg = importance.clamp(max=minimum_importance_policy)

        v_s_tp1 = torch.cat((v_s[1:], estimated_value.unsqueeze(0)))
        advantage = rewards + discount_terminal_mask * v_s_tp1 - values

        # if multiple actions broadcast the advantage to be weighted by the
        # different actions importance
        # (dim 3 is seq, batch, # actions)
        if clamped_importance_pg.dim() == 3:
            advantage = advantage.unsqueeze(-1)

        weighted_advantage = clamped_importance_pg * advantage
        return v_s, weighted_advantage, importance

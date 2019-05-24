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
from collections import OrderedDict

import torch
from torch.nn import functional as F

from adept.expcaches.rollout import RolloutCache
from adept.agents.agent_module import AgentModule


class DQN(AgentModule):
    args = {
        'nb_rollout': 20,
        'discount': 0.99,
        'egreedy_steps': 1000000
    }

    def __init__(
        self,
        network,
        device,
        reward_normalizer,
        gpu_preprocessor,
        engine,
        action_space,
        nb_env,
        nb_rollout,
        discount,
        egreedy_steps
    ):
        super(DQN, self).__init__(
            network,
            device,
            reward_normalizer,
            gpu_preprocessor,
            engine,
            action_space,
            nb_env
        )
        self.discount, self.egreedy_steps = discount, egreedy_steps / nb_env
        self._act_count = 0

        self._exp_cache = RolloutCache(
            nb_rollout, device, reward_normalizer,
            ['values', 'log_probs', 'entropies']
        )
        self._action_keys = list(sorted(action_space.keys()))

    @classmethod
    def from_args(
        cls, args, network, device, reward_normalizer, gpu_preprocessor, engine,
        action_space, nb_env=None
    ):
        if nb_env is None:
            nb_env = args.nb_env

        return cls(
            network, device, reward_normalizer, gpu_preprocessor, engine,
            action_space,
            nb_env=nb_env,
            nb_rollout=args.nb_rollout,
            discount=args.discount,
            egreedy_steps=args.egreedy_steps
        )

    @property
    def exp_cache(self):
        return self._exp_cache

    @staticmethod
    def output_space(action_space):
        head_dict = {**action_space}
        return head_dict

    def act(self, obs):
        self.network.train()
        self._act_count += 1
        return self._act_gym(obs)

    def _act_gym(self, obs):
        predictions, internals = self.network(
            self.gpu_preprocessor(obs, self.device), self.internals
        )
        batch_size = predictions[self._action_keys[0]].shape[0]

        # reduce feature dim, build action_key dim
        actions = OrderedDict()
        values = []
        # TODO support multi-dimensional action spaces?
        for key in self._action_keys:
            # possible sample
            if self._act_count < self.egreedy_steps:
                epsilon = 1 - (0.9 / self.egreedy_steps) * self._act_count
                if self._act_count % 100 == 0:
                    print(self._act_count, epsilon)
            else:
                epsilon = 0.1

            # TODO: if random action, it's random across all envs, make it single
            if epsilon > torch.rand(1):
                action = torch.randint(self.action_space[key][0], (batch_size, 1), dtype=torch.long).to(self.device)
            else:
                action = predictions[key].argmax(dim=-1, keepdim=True)

            actions[key] = action.squeeze(1).cpu().numpy()
            values.append(predictions[key].gather(1, action))

        values = torch.cat(values, dim=1)

        self.exp_cache.write_forward(values=values)
        self.internals = internals
        return actions

    def act_eval(self, obs):
        self.network.eval()
        return self._act_eval_gym(obs)

    def _act_eval_gym(self, obs):
        raise NotImplementedError()
        with torch.no_grad():
            predictions, internals = self.network(
                self.gpu_preprocessor(obs, self.device), self.internals
            )

            # reduce feature dim, build action_key dim
            actions = OrderedDict()
            for key in self._action_keys:
                logit = predictions[key]
                prob = F.softmax(logit, dim=1)
                action = torch.argmax(prob, 1)
                actions[key] = action.cpu().numpy()

        self.internals = internals
        return actions

    def compute_loss(self, rollouts, next_obs):
        # estimate value of next state
        with torch.no_grad():
            next_obs_on_device = self.gpu_preprocessor(next_obs, self.device)
            results, _ = self.network(next_obs_on_device, self.internals)
            last_values = []
            for k in self._action_keys:
                max_val, _ = torch.max(results[k], 1, keepdim=True)
                last_values.append(max_val.squeeze(1))
        last_values = torch.stack([torch.max(results[k], 1)[0].data for k in self._action_keys], dim=1)

        # compute nstep return and advantage over batch
        batch_values = torch.stack(rollouts.values)
        value_targets = self._compute_returns_advantages(last_values, rollouts.rewards, rollouts.terminals)

        # batched value loss
        value_loss = 0.5 * torch.mean((value_targets - batch_values).pow(2))

        losses = {'value_loss': value_loss}
        metrics = {}
        return losses, metrics

    def _compute_returns_advantages(self, estimated_value, rewards, terminals):
        next_value = estimated_value
        # First step of nstep reward target is estimated value of t+1
        target_return = estimated_value
        nstep_target_returns = []
        for i in reversed(range(len(rewards))):
            # unsqueeze over action dim so it isn't broadcasted
            reward = rewards[i].unsqueeze(-1)
            terminal = terminals[i].unsqueeze(-1)

             # Nstep return is always calculated for the critic's target
            target_return = reward + self.discount * target_return * terminal
            nstep_target_returns.append(target_return)

        # reverse lists
        nstep_target_returns = torch.stack(list(reversed(nstep_target_returns))).data
        return nstep_target_returns


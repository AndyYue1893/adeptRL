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
from time import time

from adept.utils import listd_to_dlist, dtensor_to_dev
from adept.container.base.mixin import WritesSummaries, SavesModels, LogsAndSummarizesRewards


class Local(
    WritesSummaries, LogsAndSummarizesRewards, SavesModels
):
    """
    A Local container trains an agent on a single GPU.
    """
    def __init__(
        self, agent, environment, network, make_optimizer, epoch_len, nb_env,
        logger, summary_writer, summary_frequency, saver, device
    ):
        super().__init__()
        self.agent = agent
        self.environment = environment
        self.device = device

        self._network = network.to(device)
        self._optimizer = make_optimizer(self.network.parameters())
        self._epoch_len = epoch_len
        self._nb_env = nb_env
        self._logger = logger
        self._summary_writer = summary_writer
        self._saver = saver
        self._summary_frequency = summary_frequency

    @property
    def network(self):
        return self._network

    @property
    def optimizer(self):
        return self._optimizer

    @property
    def epoch_len(self):
        return self._epoch_len

    @property
    def nb_env(self):
        return self._nb_env

    @property
    def logger(self):
        return self._logger

    @property
    def summary_writer(self):
        return self._summary_writer

    @property
    def summary_frequency(self):
        return self._summary_frequency

    @property
    def saver(self):
        return self._saver

    @property
    def world_size(self):
        return 1

    def run(self, max_steps=float('inf'), initial_count=0):
        self.set_local_step_count(initial_count)
        self.set_next_save(initial_count)
        self.network.train()

        next_obs = dtensor_to_dev(self.environment.reset(), self.device)
        internals = listd_to_dlist([
            self.network.new_internals(self.device) for _ in
            range(self.nb_env)
        ])
        self.start_time = time()
        while self.local_step_count < max_steps:
            obs = next_obs
            # Build rollout
            actions, internals = self.agent.act(self.network, obs, internals)
            next_obs, rewards, terminals, infos = self.environment.step(actions)
            next_obs = dtensor_to_dev(next_obs, self.device)

            self.agent.observe(
                obs,
                rewards.to(self.device),
                terminals.to(self.device),
                infos
            )
            for i, terminal in enumerate(terminals):
                if terminal:
                    for k, v in self.network.new_internals(self.device).items():
                        internals[k][i] = v
            # Perform state updates
            terminal_rewards, terminal_infos = self.update_buffers(
                rewards, terminals, infos
            )
            self.log_episode_results(
                terminal_rewards, terminal_infos, self.local_step_count,
                initial_step_count=initial_count
            )
            self.write_reward_summaries(terminal_rewards, self.local_step_count)
            self.save_model_if_epoch(self.local_step_count)

            # Learn
            if self.agent.is_ready():
                loss_dict, metric_dict = self.agent.compute_loss(
                    self.network, next_obs, internals
                )
                total_loss = torch.sum(
                    torch.stack(tuple(loss for loss in loss_dict.values()))
                )

                self.optimizer.zero_grad()
                total_loss.backward()
                self.optimizer.step()

                self.agent.clear()
                for k, vs in internals.items():
                    internals[k] = [v.detach() for v in vs]

                # write summaries
                self.write_summaries(
                    total_loss, loss_dict, metric_dict, self.local_step_count
                )
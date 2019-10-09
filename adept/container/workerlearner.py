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
import os
from glob import glob
from itertools import chain
from time import time

import ray
import torch
from torch import distributed as dist
from torch.utils.tensorboard import SummaryWriter

from adept.manager import SubProcEnvManager
from adept.network import ModularNetwork
from adept.registry import REGISTRY
from adept.utils import listd_to_dlist, dtensor_to_dev
from adept.utils.logging import SimpleModelSaver
from adept.utils.util import DotDict
from .base import Container


def gpu_id(local_rank, gpu_ids):
    device_count = len(gpu_ids)
    if local_rank == 0:
        return gpu_ids[0]
    elif device_count == 1:
        return gpu_ids[0]
    else:
        gpu_idx = (local_rank % (device_count - 1)) + 1
        return gpu_ids[gpu_idx]


class Learner(Container):

    def __init__(
            self,
            args,
            logger,
            log_id_dir,
            initial_step_count,
            rank,
            learner_ranks,
            worker_ranks
    ):
        args = DotDict(args)
        world_size = len(learner_ranks) + len(worker_ranks)

        dist.init_process_group(
            'nccl',
            init_method='tcp://{}:{}'.format(args.nccl_addr, args.nccl_port),
            rank=rank,
            world_size=world_size
        )
        groups = {}
        for learner_rank in learner_ranks:
            for worker_rank in worker_ranks:
                g = dist.new_group([learner_rank, worker_rank])
                if learner_rank == rank:
                    groups[worker_rank] = g
        learner_group = dist.new_group(learner_ranks)

        # ENV (temporary)
        env_cls = REGISTRY.lookup_env(args.env)
        env = env_cls.from_args(args, 0)
        env.close()

        # NETWORK
        torch.manual_seed(args.seed)
        gpu_id = ray.get_gpu_ids()[0]
        print(f'Learner {rank} assigned to {gpu_id}')
        device = torch.device("cuda")
        output_space = REGISTRY.lookup_output_space(
            args.actor_host, env.action_space
        )
        if args.custom_network:
            net_cls = REGISTRY.lookup_network(args.custom_network)
        else:
            net_cls = ModularNetwork
        net = net_cls.from_args(
            args,
            env.observation_space,
            output_space,
            env.gpu_preprocessor,
            REGISTRY
        )
        if rank == 0:
            print('Network parameters: ' + str(self.count_parameters(net)))

        def optim_fn(x):
            return torch.optim.RMSprop(x, lr=args.lr, eps=1e-5, alpha=0.99)

        # LEARNER / EXP
        rwd_norm = REGISTRY.lookup_reward_normalizer(
            args.rwd_norm).from_args(args)
        actor_cls = REGISTRY.lookup_actor(args.actor_host)
        builder = actor_cls.exp_spec_builder(
            env.observation_space,
            env.action_space,
            net.internal_space(),
            args.nb_env * args.nb_learn_batch
        )
        w_builder = REGISTRY.lookup_actor(args.actor_worker).exp_spec_builder(
            env.observation_space,
            env.action_space,
            net.internal_space(),
            args.nb_env
        )
        actor = actor_cls.from_args(args, env.action_space)
        learner_cls = REGISTRY.lookup_learner(args.learner)

        learner = learner_cls.from_args(args, rwd_norm)
        exp_cls = REGISTRY.lookup_exp(args.exp).from_args(args, builder)

        self.actor = actor
        self.learner = learner
        self.exp = exp_cls.from_args(args, builder).to(device)
        self.worker_exps = [
            exp_cls.from_args(args, w_builder).to(device)
            for _ in range(args.nb_learn_batch)
        ]
        self.batch_size = args.nb_env * args.nb_learn_batch
        self.nb_learn_batch = args.nb_learn_batch
        self.nb_env = args.nb_env
        self.nb_worker = len(groups)
        self.nb_step = args.nb_step
        self.network = net.to(device)
        self.optimizer = optim_fn(self.network.parameters())
        self.device = device
        self.initial_step_count = initial_step_count
        self.log_id_dir = log_id_dir
        self.epoch_len = args.epoch_len
        self.summary_freq = args.summary_freq
        self.logger = logger
        self.summary_writer = SummaryWriter(
            os.path.join(log_id_dir, 'rank{}'.format(rank))
        )
        self.saver = SimpleModelSaver(log_id_dir)
        self.groups = groups
        self.learner_group = learner_group
        self.rank = rank

        self.exp_handles = None

        if args.load_network:
            self.network = self.load_network(self.network, args.load_network)
            logger.info('Reloaded network from {}'.format(args.load_network))
        if args.load_optim:
            self.optimizer = self.load_optim(self.optimizer, args.load_optim)
            logger.info('Reloaded optimizer from {}'.format(args.load_optim))

        if self.nb_learn_batch > self.nb_worker:
            self.logger.warn('More learn batches than workers, reducing '
                             'learn batches to {}'.format(self.nb_worker))
            self.nb_learn_batch = self.nb_worker

        self.network.train()
        self.start_time = time()
        self.next_save = self.init_next_save(initial_step_count, self.epoch_len)
        self.prev_step_t = time()

    def step(self, step_count, worker_ranks):

        # make sure exp_handles is sync'ed before stepping
        for handle in chain.from_iterable(self.exp_handles):
            handle.wait()

        self.exp.write_exps([self.worker_exps[i] for i in range(len(worker_ranks))])

        r = self.exp.read()
        internals = {k: ts[0].unbind(0) for k, ts in r.internals.items()}
        for obs, rewards, terminals in zip(
                r.observations,
                r.rewards,
                r.terminals
        ):
            _, h_exp, internals = self.actor.act(self.network, obs, internals)
            self.exp.write_actor(h_exp, no_env=True)

            if step_count >= self.next_save and self.rank == 0:
                self.saver.save_state_dicts(
                    self.network, step_count, self.optimizer
                )
                self.next_save += self.epoch_len

        # Backprop
        loss_dict, metric_dict = self.learner.compute_loss(
            self.network, self.exp.read(), r.next_observation, internals
        )
        total_loss = torch.sum(
            torch.stack(tuple(loss for loss in loss_dict.values()))
        )

        self.optimizer.zero_grad()
        total_loss.backward()
        self.optimizer.step()

        self.exp.clear()
        [exp.clear() for exp in self.worker_exps]  # necessary?

        # write summaries
        cur_step_t = time()
        if cur_step_t - self.prev_step_t > self.summary_freq:
            self.write_summaries(
                self.summary_writer, step_count, total_loss,
                loss_dict, metric_dict, self.network.named_parameters()
            )
            self.prev_step_t = cur_step_t
        return self.rank
        # return r.rewards, r.terminals

    def sync_exps(self, worker_ranks):
        handles = []
        print(worker_ranks)
        for i, worker_rank in enumerate(worker_ranks):
            handles.append(self.worker_exps[i].sync(
                worker_rank,
                self.groups[worker_rank],
                async_op=True
            ))
        self.exp_handles = handles
        return self.rank

    def sync_network(self, worker_ranks):
        handles = []
        for worker_rank in worker_ranks:
            handles.append(self.network.sync(
                self.rank,
                self.groups[worker_rank],
                async_op=True
            ))
        return self.rank

    def close(self):
        return None


class Worker(Container):
    """
    Actor Learner Architecture worker.
    """

    def __init__(
            self,
            args,
            logger,
            log_id_dir,
            initial_step_count,
            rank,
            learner_ranks,
            worker_ranks
    ):
        args = DotDict(args)
        world_size = len(learner_ranks) + len(worker_ranks)

        dist.init_process_group(
            'nccl',
            init_method='tcp://{}:{}'.format(args.nccl_addr, args.nccl_port),
            rank=rank,
            world_size=world_size
        )
        groups = {}
        for learner_rank in learner_ranks:
            for worker_rank in worker_ranks:
                g = dist.new_group([learner_rank, worker_rank])
                if worker_rank == rank:
                    groups[learner_rank] = g
        dist.new_group(learner_ranks)

        seed = args.seed + args.nb_env * (rank - len(learner_ranks))
        print('Using {} for rank {} seed.'.format(seed, rank))

        # ENV
        engine = REGISTRY.lookup_engine(args.env)
        env_cls = REGISTRY.lookup_env(args.env)
        env_mgr = SubProcEnvManager.from_args(args, engine, env_cls, seed=seed)

        # NETWORK
        torch.manual_seed(args.seed)
        gpu_id = ray.get_gpu_ids()[0]
        print(f'Worker {rank} assigned to {gpu_id}')
        device = torch.device("cuda")
        output_space = REGISTRY.lookup_output_space(
            args.actor_host, env_mgr.action_space
        )
        if args.custom_network:
            net_cls = REGISTRY.lookup_network(args.custom_network)
        else:
            net_cls = ModularNetwork
        net = net_cls.from_args(
            args,
            env_mgr.observation_space,
            output_space,
            env_mgr.gpu_preprocessor,
            REGISTRY
        )
        actor_cls = REGISTRY.lookup_actor(args.actor_worker)
        actor = actor_cls.from_args(args, env_mgr.action_space)
        builder = actor_cls.exp_spec_builder(
            env_mgr.observation_space,
            env_mgr.action_space,
            net.internal_space(),
            env_mgr.nb_env
        )
        exp = REGISTRY.lookup_exp(args.exp).from_args(args, builder)

        # Properties
        self.actor = actor
        self.exp = exp.to(device)
        self.nb_step = args.nb_step
        self.env_mgr = env_mgr
        self.nb_env = args.nb_env
        self.network = net.to(device)
        self.device = device
        self.initial_step_count = initial_step_count
        self.log_id_dir = log_id_dir
        self.epoch_len = args.epoch_len
        self.logger = logger
        self.groups = groups
        self.rank = rank

        # State
        self.network_handles = None
        self.obs = dtensor_to_dev(self.env_mgr.reset(), self.device)
        self.internals = listd_to_dlist([
            self.network.new_internals(self.device) for _ in
            range(self.nb_env)
        ])

        # Initialization
        if args.load_network:
            self.network = self.load_network(self.network, args.load_network)
            logger.info('Reloaded network from {}'.format(args.load_network))
        if args.load_optim:
            self.optimizer = self.load_optim(self.optimizer, args.load_optim)
            logger.info('Reloaded optimizer from {}'.format(args.load_optim))

        self.network.train()

    def step(self):

        self.exp.clear()
        # don't step until network has been sync'ed
        if self.network_handles:
            for h in self.network_handles:
                h.wait()

        for _ in range(len(self.exp)):
            with torch.no_grad():
                actions, exp, self.internals = self.actor.act(
                    self.network, self.obs, self.internals)
            self.exp.write_actor(exp)

            next_obs, rewards, terminals, infos = self.env_mgr.step(actions)

            self.exp.write_env(
                self.obs,
                rewards.to(self.device).float(),
                terminals.to(self.device).float(),
                infos
            )

            for i, terminal in enumerate(terminals):
                if terminal:
                    for k, v in self.network.new_internals(self.device).items():
                        self.internals[k][i] = v
            self.obs = dtensor_to_dev(next_obs, self.device)

        self.exp.write_next_obs(self.obs)
        return self.rank

    def sync_exp(self, learner_rank):
        self.exp.sync(
            self.rank,
            self.groups[learner_rank],
            async_op=True
        )
        return self.rank

    def sync_network(self, learner_rank):
        handles = self.network.sync(
            learner_rank,
            self.groups[learner_rank],
            async_op=True
        )
        self.network_handles = handles
        return self.rank

    def close(self):
        self.env_mgr.close()
        return self.rank

import abc
from collections import OrderedDict

import torch
from torch.nn import functional as F


class _BasePolicy(metaclass=abc.ABCMeta):
    """
    An abstract policy. A policy converts logits into actions and extra fields
    fields necessary for loss computation.
    """

    def __init__(self, action_space):
        self._action_sapce = action_space
        self._action_keys = list(sorted(action_space.keys()))

    def act(self, logits, available_actions=None):
        """
        ActionKey = str
        Action = Tensor (cpu)
        Extras = Dict[str, Tensor], extra fields with tensor's needed for loss
        computation. e.g.:
            log_probs: torch.Tensor
            entropies: torch.Tensor

        logits: Dict[ActionKey, torch.Tensor]
        :param available_actions:
            None if not needed
            torch.Tensor (N, NB_ACTION), one hot
        :return: Tuple[Action, Extras]
        """
        raise NotImplementedError


class ActorCriticHelper(_BasePolicy, metaclass=abc.ABCMeta):
    """
    A helper class for actor critic policies.
    """

    def act(self, logits, available_actions=None):
        """
        :param logits: Dict[ActionKey, torch.Tensor]
        :param available_actions:
            None if not needed
            torch.Tensor (N, NB_ACTION), one hot
        :return:
        """
        raise NotImplementedError

    def flatten_to_2d(self, logit):
        """
        :param logits: Tensor of arbitrary dim
        :return: logits flattened to (N, X)
        """
        size = logit.size()
        dim = logit.dim()

        if dim == 3:
            n, f, l = size
            logit = logit.view(n, f * l)
        elif dim == 4:
            n, f, h, w = size
            logit = logit.view(n, f * h * w)
        elif dim == 5:
            n, f, d, h, w = size
            logit = logit.view(n, f * d * h * w)
        return logit

    def softmax(self, logit):
        """
        :param logit: torch.Tensor (N, X)
        :return: torch.Tensor (N, X)
        """
        return F.softmax(logit, dim=1)

    def log_softmax(self, logit):
        """
        :param logit: torch.Tensor (N, X)
        :return: torch.Tensor (N, X)
        """
        return F.log_softmax(logit, dim=1)

    def log_probability(self, log_softmax, action):
        return log_softmax.gather(1, action.unsqueeze(1))

    def entropy(self, log_softmax, softmax):
        return -(
            log_softmax * softmax
        ).sum(1, keepdim=True)

    def sample_action(self, softmax):
        """
        Samples an action from a softmax distribution.

        :param softmax: torch.Tensor (N, X)
        :return: torch.Tensor (N)
        """
        return softmax.multinomial(1).squeeze(1)

    def select_action(self, softmax):
        """
        Selects the action with the highest probability.

        :param softmax:
        :return:
        """
        return torch.argmax(softmax, dim=1)


class ActorCriticPolicy(ActorCriticHelper):
    def act(self, logits, available_actions=None):
        """
        :param logits: Dict[ActionKey, torch.Tensor]
        :param available_actions: torch.Tensor (N, NB_ACTION), one hot
        :return:
        """
        actions = OrderedDict()
        log_probs = []
        entropies = []

        for key in self._action_keys:
            logit = self.flatten_to_2d(logits[key])

            log_softmax, softmax = self.log_softmax(logit), self.softmax(logit)
            entropy = self.entropy(log_softmax, softmax)
            entropies.append(entropy)
            action = self.sample_action(softmax)
            log_probs.append(self.log_probability(log_softmax, action))
            actions[key] = action.cpu()

        log_probs = torch.cat(log_probs, dim=1)
        entropies = torch.cat(entropies, dim=1)

        return actions, {
            'log_probs': log_probs,
            'entropies': entropies
        }


class ActorCriticEvalPolicy(ActorCriticHelper):
    def act(self, logits, available_actions=None):
        """
        :param logits: Dict[ActionKey, torch.Tensor]
        :param available_actions:
            None if not needed
            torch.Tensor (N, NB_ACTION), one hot
        :return:
        """
        with torch.no_grad():
            actions = OrderedDict()
            for key in self._action_keys:
                logit = self.flatten_to_2d(logits[key])
                actions[key] = self.select_action(self.softmax(logit)).cpu()
            return actions, {}

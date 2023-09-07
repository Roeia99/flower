"""Create global evaluation function.

Optionally, also define a new Server class (please note this is not needed in most
settings).
"""
from collections import OrderedDict
from typing import Callable, Dict, Optional, Tuple

import torch
from flwr.common import NDArrays, Scalar
from hydra.utils import instantiate
from omegaconf import DictConfig
from torch.utils.data import DataLoader

from fedexp.models import test


def gen_evaluate_fn(
        test_loader: DataLoader,
        model: DictConfig,
        device,
) -> Callable[
    [int, NDArrays, Dict[str, Scalar]], Optional[Tuple[float, Dict[str, Scalar]]]
]:
    """Generates the function for centralized evaluation.

    Parameters
    ----------
    model: DictConfig
        The model details to evaluate.
    test_loader : DataLoader
        The dataloader to test the model with.
    device : torch.device
        The device to test the model on.

    Returns
    -------
    Callable[ [int, NDArrays, Dict[str, Scalar]], Optional[Tuple[float, Dict[str, Scalar]]] ]
        The centralized evaluation function.
    """
    def evaluate(
            server_round: int,
            parameters_ndarrays: NDArrays,
            config: Dict[str, Scalar]
    ) -> Optional[Tuple[float, Dict[str, Scalar]]]:
        """Use the entire CIFAR-10/100 test set for evaluation."""

        net = instantiate(model)
        params_dict = zip(net.state_dict().keys(), parameters_ndarrays)
        state_dict = OrderedDict({k: torch.Tensor(v) for k, v in params_dict})
        net.load_state_dict(state_dict, strict=True)
        net.to(device)

        loss, accuracy = test(net, test_loader, device=device)
        return loss, {"accuracy": accuracy}

    return evaluate

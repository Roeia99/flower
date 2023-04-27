# Copyright 2020 Adap GmbH. All Rights Reserved.
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
# ==============================================================================

import concurrent.futures
import timeit
from logging import INFO, WARNING
from typing import Dict, List, Optional, Tuple

from flwr.common.logger import log
from flwr.common.parameter import parameters_to_ndarrays, ndarrays_to_parameters
from flwr.common.sec_agg import sec_agg_primitives
from flwr.common.typing import (AskKeysIns, AskKeysRes,
                                AskVectorsIns, AskVectorsRes,
                                FitIns, Parameters, Scalar,
                                SetupParamIns, SetupParamRes,
                                ShareKeysIns, ShareKeysPacket, ShareKeysRes,
                                UnmaskVectorsIns, UnmaskVectorsRes)
from flwr.server import Server, ClientManager
from flwr.server.client_proxy import ClientProxy
from flwr.server.server import FitResultsAndFailures
from flwr.server.strategy.secagg import SecAggStrategy

SetupParamResultsAndFailures = Tuple[
    List[Tuple[ClientProxy, SetupParamRes]], List[BaseException]
]
AskKeysResultsAndFailures = Tuple[
    List[Tuple[ClientProxy, AskKeysRes]], List[BaseException]
]
ShareKeysResultsAndFailures = Tuple[
    List[Tuple[ClientProxy, ShareKeysRes]], List[BaseException]
]
AskVectorsResultsAndFailures = Tuple[
    List[Tuple[ClientProxy, AskVectorsRes]], List[BaseException]
]
UnmaskVectorsResultsAndFailures = Tuple[
    List[Tuple[ClientProxy, UnmaskVectorsRes]], List[BaseException]
]


class SecAggServer(Server):
    """Flower secure aggregation server."""

    def __init__(self, *, client_manager: ClientManager, strategy: Optional[SecAggStrategy]) -> None:
        super().__init__(client_manager=client_manager, strategy=strategy)

    def fit_round(
            self,
            server_round: int,
            timeout: Optional[float],
    ) -> Optional[
        Tuple[Optional[Parameters], Dict[str, Scalar], FitResultsAndFailures]
    ]:
        total_time = 0
        total_time = total_time - timeit.default_timer()
        # Sample clients
        client_instruction_list = self.strategy.configure_fit(server_round=server_round,
                                                              parameters=self.parameters,
                                                              client_manager=self._client_manager)
        setup_param_clients: Dict[int, ClientProxy] = {}
        client_instructions: Dict[int, FitIns] = {}
        for idx, value in enumerate(client_instruction_list):
            setup_param_clients[idx] = value[0]
            client_instructions[idx] = value[1]

        # Get sec_agg parameters from strategy
        log(INFO, "Get sec_agg_param_dict from strategy")
        sec_agg_param_dict = self.strategy.get_sec_agg_param()
        sec_agg_param_dict["sample_num"] = len(client_instruction_list)
        sec_agg_param_dict = process_sec_agg_param_dict(sec_agg_param_dict)

        # === Stage 0: Setup ===
        # Give rnd, sample_num, share_num, threshold, client id
        log(INFO, "SecAgg Stage 0: Setting up Params")
        total_time = total_time + timeit.default_timer()
        setup_param_results_and_failures = setup_param(
            clients=setup_param_clients,
            sec_agg_param_dict=sec_agg_param_dict
        )
        total_time = total_time - timeit.default_timer()
        setup_param_results = setup_param_results_and_failures[0]
        ask_keys_clients: Dict[int, ClientProxy] = {}
        if len(setup_param_results) < sec_agg_param_dict['min_num']:
            raise Exception("Not enough available clients after setup param stage")
        for idx, client in setup_param_clients.items():
            if client in [result[0] for result in setup_param_results]:
                ask_keys_clients[idx] = client

        # === Stage 1: Ask Public Keys ===
        log(INFO, "SecAgg Stage 1: Asking Keys")
        total_time = total_time + timeit.default_timer()
        ask_keys_results_and_failures = ask_keys(ask_keys_clients)
        total_time = total_time - timeit.default_timer()
        public_keys_dict: Dict[int, AskKeysRes] = {}
        ask_keys_results = ask_keys_results_and_failures[0]
        if len(ask_keys_results) < sec_agg_param_dict['min_num']:
            raise Exception("Not enough available clients after ask keys stage")
        share_keys_clients: Dict[int, ClientProxy] = {}

        # Build public keys dict
        for idx, client in ask_keys_clients.items():
            if client in [result[0] for result in ask_keys_results]:
                pos = [result[0] for result in ask_keys_results].index(client)
                public_keys_dict[idx] = ask_keys_results[pos][1]
                share_keys_clients[idx] = client

        # === Stage 2: Share Keys ===
        log(INFO, "SecAgg Stage 2: Sharing Keys")
        total_time = total_time + timeit.default_timer()
        share_keys_results_and_failures = share_keys(
            share_keys_clients, public_keys_dict, sec_agg_param_dict['sample_num'], sec_agg_param_dict['share_num']
        )
        total_time = total_time - timeit.default_timer()
        share_keys_results = share_keys_results_and_failures[0]
        if len(share_keys_results) < sec_agg_param_dict['min_num']:
            raise Exception("Not enough available clients after share keys stage")

        # Build forward packet list dictionary
        total_packet_list: List[ShareKeysPacket] = []
        forward_packet_list_dict: Dict[int, List[ShareKeysPacket]] = {}
        ask_vectors_clients: Dict[int, ClientProxy] = {}
        for idx, client in share_keys_clients.items():
            if client in [result[0] for result in share_keys_results]:
                pos = [result[0] for result in share_keys_results].index(client)
                ask_vectors_clients[idx] = client
                packet_list = share_keys_results[pos][1].share_keys_res_list
                total_packet_list += packet_list

        for idx in ask_vectors_clients.keys():
            forward_packet_list_dict[idx] = []

        for packet in total_packet_list:
            destination = packet.destination
            if destination in ask_vectors_clients.keys():
                forward_packet_list_dict[destination].append(packet)

        # === Stage 3: Ask Vectors ===
        log(INFO, "SecAgg Stage 3: Asking Vectors")
        total_time = total_time + timeit.default_timer()
        ask_vectors_results_and_failures = ask_vectors(
            ask_vectors_clients, forward_packet_list_dict, client_instructions)
        total_time = total_time - timeit.default_timer()
        ask_vectors_results = ask_vectors_results_and_failures[0]
        if len(ask_vectors_results) < sec_agg_param_dict['min_num']:
            raise Exception("Not enough available clients after ask vectors stage")
        # Get shape of vector sent by first client
        masked_vector = sec_agg_primitives.weights_zero_generate(
            [i.shape for i in parameters_to_ndarrays(ask_vectors_results[0][1].parameters)])
        # Add all collected masked vectors and compuute available and dropout clients set
        unmask_vectors_clients: Dict[int, ClientProxy] = {}
        dropout_clients = ask_vectors_clients.copy()
        for idx, client in ask_vectors_clients.items():
            if client in [result[0] for result in ask_vectors_results]:
                pos = [result[0] for result in ask_vectors_results].index(client)
                unmask_vectors_clients[idx] = client
                dropout_clients.pop(idx)
                client_parameters = ask_vectors_results[pos][1].parameters
                masked_vector = sec_agg_primitives.weights_addition(
                    masked_vector, parameters_to_ndarrays(client_parameters))
        # === Stage 4: Unmask Vectors ===
        log(INFO, "SecAgg Stage 4: Unmasking Vectors")
        total_time = total_time + timeit.default_timer()
        unmask_vectors_results_and_failures = unmask_vectors(
            unmask_vectors_clients, dropout_clients, sec_agg_param_dict['sample_num'], sec_agg_param_dict['share_num'])
        unmask_vectors_results = unmask_vectors_results_and_failures[0]
        total_time = total_time - timeit.default_timer()
        # Build collected shares dict
        collected_shares_dict: Dict[int, List[bytes]] = {}
        for idx in ask_vectors_clients.keys():
            collected_shares_dict[idx] = []

        if len(unmask_vectors_results) < sec_agg_param_dict['min_num']:
            raise Exception("Not enough available clients after unmask vectors stage")
        for result in unmask_vectors_results:
            unmask_vectors_res = result[1]
            for owner_id, share in unmask_vectors_res.share_dict.items():
                collected_shares_dict[owner_id].append(share)

        # Remove mask for every client who is available before ask vectors stage,
        # Divide vector by first element
        for client_id, share_list in collected_shares_dict.items():
            if len(share_list) < sec_agg_param_dict['threshold']:
                raise Exception(
                    "Not enough shares to recover secret in unmask vectors stage")
            secret = sec_agg_primitives.combine_shares(share_list=share_list)
            if client_id in unmask_vectors_clients.keys():
                # seed is an available client's b
                private_mask = sec_agg_primitives.pseudo_rand_gen(
                    secret, sec_agg_param_dict['mod_range'], sec_agg_primitives.weights_shape(masked_vector))
                masked_vector = sec_agg_primitives.weights_subtraction(
                    masked_vector, private_mask)
            else:
                # seed is a dropout client's sk1
                neighbor_list: List[int] = []
                if sec_agg_param_dict['share_num'] == sec_agg_param_dict['sample_num']:
                    neighbor_list = list(ask_vectors_clients.keys())
                    neighbor_list.remove(client_id)
                else:
                    for i in range(-int(sec_agg_param_dict['share_num'] / 2),
                                   int(sec_agg_param_dict['share_num'] / 2) + 1):
                        if i != 0 and (
                                (i + client_id) % sec_agg_param_dict['sample_num']) in ask_vectors_clients.keys():
                            neighbor_list.append((i + client_id) %
                                                 sec_agg_param_dict['sample_num'])

                for neighbor_id in neighbor_list:
                    shared_key = sec_agg_primitives.generate_shared_key(
                        sec_agg_primitives.bytes_to_private_key(secret),
                        sec_agg_primitives.bytes_to_public_key(public_keys_dict[neighbor_id].pk1))
                    pairwise_mask = sec_agg_primitives.pseudo_rand_gen(
                        shared_key, sec_agg_param_dict['mod_range'], sec_agg_primitives.weights_shape(masked_vector))
                    if client_id > neighbor_id:
                        masked_vector = sec_agg_primitives.weights_addition(
                            masked_vector, pairwise_mask)
                    else:
                        masked_vector = sec_agg_primitives.weights_subtraction(
                            masked_vector, pairwise_mask)
        masked_vector = sec_agg_primitives.weights_mod(
            masked_vector, sec_agg_param_dict['mod_range'])
        # Divide vector by number of clients who have given us their masked vector
        # i.e. those participating in final unmask vectors stage
        total_weights_factor, masked_vector = sec_agg_primitives.factor_weights_extract(
            masked_vector)
        masked_vector = sec_agg_primitives.weights_divide(
            masked_vector, total_weights_factor)
        aggregated_vector = sec_agg_primitives.reverse_quantize(
            masked_vector, sec_agg_param_dict['clipping_range'], sec_agg_param_dict['target_range'])
        aggregated_parameters = ndarrays_to_parameters(aggregated_vector)
        total_time = total_time + timeit.default_timer()
        f = open("log.txt", "a")
        f.write(f"Server time without communication:{total_time} \n")
        f.write(f"first element {aggregated_vector[0].flatten()[0]}\n\n\n")
        f.close()
        return aggregated_parameters, None, None


def process_sec_agg_param_dict(sec_agg_param_dict: Dict[str, Scalar]) -> Dict[str, Scalar]:
    # min_num will be replaced with intended min_num based on sample_num
    # if both min_frac or min_num not provided, we take maximum of either 2 or 0.9 * sampled
    # if either one is provided, we use that
    # Otherwise, we take the maximum
    # Note we will eventually check whether min_num>=2
    if 'min_frac' not in sec_agg_param_dict:
        if 'min_num' not in sec_agg_param_dict:
            sec_agg_param_dict['min_num'] = max(
                2, int(0.9*sec_agg_param_dict['sample_num']))
    else:
        if 'min_num' not in sec_agg_param_dict:
            sec_agg_param_dict['min_num'] = int(
                sec_agg_param_dict['min_frac']*sec_agg_param_dict['sample_num'])
        else:
            sec_agg_param_dict['min_num'] = max(sec_agg_param_dict['min_num'], int(
                sec_agg_param_dict['min_frac']*sec_agg_param_dict['sample_num']))

    if 'share_num' not in sec_agg_param_dict:
        # Complete graph
        sec_agg_param_dict['share_num'] = sec_agg_param_dict['sample_num']
    elif sec_agg_param_dict['share_num'] % 2 == 0 and sec_agg_param_dict['share_num'] != sec_agg_param_dict['sample_num']:
        # we want share_num of each node to be either odd or sample_num
        log(WARNING, "share_num value changed due to sample num and share_num constraints! See documentation for reason")
        sec_agg_param_dict['share_num'] += 1

    if 'threshold' not in sec_agg_param_dict:
        sec_agg_param_dict['threshold'] = max(
            2, int(sec_agg_param_dict['share_num'] * 0.9))

    # Maximum number of example trained set to 1000
    if 'max_weights_factor' not in sec_agg_param_dict:
        sec_agg_param_dict['max_weights_factor'] = 1000

    # Quantization parameters
    if 'clipping_range' not in sec_agg_param_dict:
        sec_agg_param_dict['clipping_range'] = 3

    if 'target_range' not in sec_agg_param_dict:
        sec_agg_param_dict['target_range'] = 16777216

    if 'mod_range' not in sec_agg_param_dict:
        sec_agg_param_dict['mod_range'] = sec_agg_param_dict['sample_num'] * \
            sec_agg_param_dict['target_range'] * \
            sec_agg_param_dict['max_weights_factor']

    if 'timeout' not in sec_agg_param_dict:
        sec_agg_param_dict['timeout'] = 30

    log(
        INFO,
        f"SecAgg parameters: {sec_agg_param_dict}",
    )

    assert (
            sec_agg_param_dict['sample_num'] >= 2
            and 2 <= sec_agg_param_dict['min_num'] <= sec_agg_param_dict['sample_num']
            and sec_agg_param_dict['sample_num'] >= sec_agg_param_dict['share_num'] >= sec_agg_param_dict[
                'threshold'] >= 2
            and (sec_agg_param_dict['share_num'] % 2 == 1 or
                 sec_agg_param_dict['share_num'] == sec_agg_param_dict['sample_num'])
            and sec_agg_param_dict['target_range'] * sec_agg_param_dict['sample_num'] * sec_agg_param_dict['max_weights_factor'] <= sec_agg_param_dict['mod_range']
    ), "SecAgg parameters not accepted"
    return sec_agg_param_dict


def setup_param(
    clients: Dict[int, ClientProxy],
    sec_agg_param_dict: Dict[str, Scalar]
) -> SetupParamResultsAndFailures:
    def sec_agg_param_dict_with_sec_agg_id(sec_agg_param_dict: Dict[str, Scalar], sec_agg_id: int):
        new_sec_agg_param_dict = sec_agg_param_dict.copy()
        new_sec_agg_param_dict[
            'sec_agg_id'] = sec_agg_id
        return new_sec_agg_param_dict
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(
                lambda p: setup_param_client(*p),
                (
                    c,
                    SetupParamIns(
                        sec_agg_param_dict=sec_agg_param_dict_with_sec_agg_id(
                            sec_agg_param_dict, idx),
                    ),
                ),
            )
            for idx, c in clients.items()
        ]
        concurrent.futures.wait(futures)
    results: List[Tuple[ClientProxy, SetupParamRes]] = []
    failures: List[BaseException] = []
    for future in futures:
        failure = future.exception()
        if failure is not None:
            failures.append(failure)
        else:
            # Success case
            result = future.result()
            results.append(result)
    return results, failures


def setup_param_client(client: ClientProxy, setup_param_msg: SetupParamIns) -> Tuple[ClientProxy, SetupParamRes]:
    setup_param_res = client.setup_param(setup_param_msg)
    return client, setup_param_res


def ask_keys(clients: Dict[int, ClientProxy]) -> AskKeysResultsAndFailures:
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [executor.submit(ask_keys_client, c) for c in clients.values()]
        concurrent.futures.wait(futures)
    results: List[Tuple[ClientProxy, AskKeysRes]] = []
    failures: List[BaseException] = []
    for future in futures:
        failure = future.exception()
        if failure is not None:
            failures.append(failure)
        else:
            # Success case
            result = future.result()
            results.append(result)
    return results, failures


def ask_keys_client(client: ClientProxy) -> Tuple[ClientProxy, AskKeysRes]:
    ask_keys_res = client.ask_keys(AskKeysIns())
    return client, ask_keys_res


def share_keys(clients: Dict[int, ClientProxy],
               public_keys_dict: Dict[int, AskKeysRes],
               sample_num: int, share_num: int) -> ShareKeysResultsAndFailures:
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(
                lambda p: share_keys_client(*p),
                (client, idx, public_keys_dict, sample_num, share_num),
            )
            for idx, client in clients.items()
        ]
        concurrent.futures.wait(futures)
    results: List[Tuple[ClientProxy, ShareKeysRes]] = []
    failures: List[BaseException] = []
    for future in futures:
        failure = future.exception()
        if failure is not None:
            failures.append(failure)
        else:
            # Success case
            result = future.result()
            results.append(result)
    return results, failures


def share_keys_client(client: ClientProxy, idx: int, public_keys_dict: Dict[int, AskKeysRes], sample_num: int, share_num: int) -> Tuple[ClientProxy, ShareKeysRes]:
    if share_num == sample_num:
        # complete graph
        return client, client.share_keys(ShareKeysIns(public_keys_dict=public_keys_dict))
    local_dict: Dict[int, AskKeysRes] = {}
    for i in range(-int(share_num / 2), int(share_num / 2) + 1):
        if ((i + idx) % sample_num) in public_keys_dict.keys():
            local_dict[(i + idx) % sample_num] = public_keys_dict[
                (i + idx) % sample_num
            ]

    return client, client.share_keys(ShareKeysIns(public_keys_dict=local_dict))


def ask_vectors(clients: Dict[int, ClientProxy],
                forward_packet_list_dict: Dict[int, List[ShareKeysPacket]],
                client_instructions: Dict[int, FitIns]) -> AskVectorsResultsAndFailures:
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(
                lambda p: ask_vectors_client(*p),
                (client, forward_packet_list_dict[idx], client_instructions[idx]),
            )
            for idx, client in clients.items()
        ]
        concurrent.futures.wait(futures)
    results: List[Tuple[ClientProxy, AskVectorsRes]] = []
    failures: List[BaseException] = []
    for future in futures:
        failure = future.exception()
        if failure is not None:
            failures.append(failure)
        else:
            # Success case
            result = future.result()
            results.append(result)
    return results, failures


def ask_vectors_client(client: ClientProxy, forward_packet_list: List[ShareKeysPacket], fit_ins: FitIns) -> Tuple[ClientProxy, AskVectorsRes]:

    return client, client.ask_vectors(AskVectorsIns(ask_vectors_in_list=forward_packet_list, fit_ins=fit_ins))


def unmask_vectors(clients: Dict[int, ClientProxy],
                   dropout_clients: Dict[int, ClientProxy],
                   sample_num: int, share_num: int) -> UnmaskVectorsResultsAndFailures:
    with concurrent.futures.ThreadPoolExecutor() as executor:
        futures = [
            executor.submit(
                lambda p: unmask_vectors_client(*p),
                (client, idx, list(clients.keys()), list(
                    dropout_clients.keys()), sample_num, share_num),
            )
            for idx, client in clients.items()
        ]
        concurrent.futures.wait(futures)
    results: List[Tuple[ClientProxy, UnmaskVectorsRes]] = []
    failures: List[BaseException] = []
    for future in futures:
        failure = future.exception()
        if failure is not None:
            failures.append(failure)
        else:
            # Success case
            result = future.result()
            results.append(result)
    return results, failures


def unmask_vectors_client(client: ClientProxy, idx: int, clients: List[ClientProxy], dropout_clients: List[ClientProxy], sample_num: int, share_num: int) -> Tuple[ClientProxy, UnmaskVectorsRes]:
    if share_num == sample_num:
        # complete graph
        return client, client.unmask_vectors(UnmaskVectorsIns(available_clients=clients, dropout_clients=dropout_clients))
    local_clients: List[int] = []
    local_dropout_clients: List[int] = []
    for i in range(-int(share_num / 2), int(share_num / 2) + 1):
        if ((i + idx) % sample_num) in clients:
            local_clients.append((i + idx) % sample_num)
        if ((i + idx) % sample_num) in dropout_clients:
            local_dropout_clients.append((i + idx) % sample_num)
    return client, client.unmask_vectors(UnmaskVectorsIns(available_clients=local_clients, dropout_clients=local_dropout_clients))

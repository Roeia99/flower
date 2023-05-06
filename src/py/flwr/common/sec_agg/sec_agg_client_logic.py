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
import pickle
import timeit
from logging import ERROR, INFO, WARNING
from typing import Dict, List, Tuple

from flwr.common.logger import log
from flwr.common.parameter import ndarrays_to_parameters, parameters_to_ndarrays
from flwr.common.sec_agg import sec_agg_primitives
from flwr.common.typing import AskKeysIns, AskVectorsIns, AskVectorsRes, SetupParamIns, SetupParamRes, ShareKeysIns, \
    ShareKeysPacket, ShareKeysRes, UnmaskVectorsIns, UnmaskVectorsRes, NDArrays, AskKeysRes


def setup_param(client, setup_param_ins: SetupParamIns):
    total_time = -timeit.default_timer()
    # Assigning parameter values to object fields
    sec_agg_param_dict = setup_param_ins.sec_agg_param_dict
    client.sample_num = sec_agg_param_dict['sample_num']
    client.sec_agg_id = sec_agg_param_dict['sec_agg_id']
    client.share_num = sec_agg_param_dict['share_num']
    client.threshold = sec_agg_param_dict['threshold']
    client.clipping_range = sec_agg_param_dict['clipping_range']
    client.target_range = sec_agg_param_dict['target_range']
    client.mod_range = sec_agg_param_dict['mod_range']
    client.max_weights_factor = sec_agg_param_dict['max_weights_factor']

    # Testing , to be removed================================================
    client.test = 0
    if 'test' in sec_agg_param_dict and sec_agg_param_dict['test'] == 1:
        client.test = 1
        client.test_vector_shape = [(sec_agg_param_dict['test_vector_dimension'],)]
        client.test_dropout_value = sec_agg_param_dict['test_dropout_value']
    # End =================================================================

    # key is the sec_agg_id of another client (int)
    # value is the secret share we possess that contributes to the client's secret (bytes)
    client.b_share_dict = {}
    client.sk1_share_dict = {}
    client.shared_key_2_dict = {}
    log(INFO, "SecAgg Stage 0 Completed: Parameters Set Up")
    total_time = total_time + timeit.default_timer()
    if client.sec_agg_id == 3:
        f = open("log.txt", "a")
        f.write(f"Client without communication stage 0:{total_time} \n")
        f.close()
    return SetupParamRes()


def ask_keys(client, ask_keys_ins: AskKeysIns) -> AskKeysRes:
    total_time = -timeit.default_timer()
    # Create 2 sets private public key pairs
    # One for creating pairwise masks
    # One for encrypting message to distribute shares
    client.sk1, client.pk1 = sec_agg_primitives.generate_key_pairs()
    client.sk2, client.pk2 = sec_agg_primitives.generate_key_pairs()
    client.priv, client.pub = sec_agg_primitives.generate_key_pairs()

    log(INFO, "SecAgg Stage 1 Completed: Created Key Pairs")
    total_time = total_time + timeit.default_timer()
    # msg = pickle.dumps([client.pk1, client.pk2])
    client_pk1=sec_agg_primitives.public_key_to_bytes(client.pk1)
    client_pk2=sec_agg_primitives.public_key_to_bytes(client.pk2)
    msg = pickle.dumps([client_pk1, client_pk2])

    return AskKeysRes(
        pk1=client_pk1,
        pk2=client_pk2,
        signature=sec_agg_primitives.signMsg(msg, client.priv),
        sig_pub=sec_agg_primitives.public_key_to_bytes(client.pub)
    )


def share_keys(client, share_keys_in: ShareKeysIns) -> ShareKeysRes:
    total_time = -timeit.default_timer()
    # Distribute shares for private mask seed and first private key

    client.public_keys_dict = share_keys_in.public_keys_dict
    # check size is larger than threshold
    if len(client.public_keys_dict) < client.threshold:
        raise Exception("Available neighbours number smaller than threshold")
    # check if all public keys received are unique
    pk_list: List[bytes] = []
    for i in client.public_keys_dict.values():
        pk_list.append(i.pk1)
        pk_list.append(i.pk2)
    if len(set(pk_list)) != len(pk_list):
        raise Exception("Some public keys are identical")

    # sanity check that own public keys are correct in dict
    if client.public_keys_dict[client.sec_agg_id].pk1 != sec_agg_primitives.public_key_to_bytes(client.pk1) \
            or client.public_keys_dict[client.sec_agg_id].pk2 != sec_agg_primitives.public_key_to_bytes(client.pk2):
        raise Exception(
            "Own public keys are displayed in dict incorrectly, should not happen!")

    #verify signatures
    for i in client.public_keys_dict.values():
        msg = pickle.dumps([i.pk1,i.pk2])
        pk= sec_agg_primitives.bytes_to_public_key(i.sig_pub)
        sec_agg_primitives.verifySig(msg,i.signature,pk)


    # Generate private mask seed
    client.b = sec_agg_primitives.rand_bytes(32)

    # Create shares
    b_shares = sec_agg_primitives.create_shares(
        client.b, client.threshold, client.share_num
    )
    sk1_shares = sec_agg_primitives.create_shares(
        sec_agg_primitives.private_key_to_bytes(
            client.sk1), client.threshold, client.share_num
    )

    share_keys_res = ShareKeysRes(share_keys_res_list=[])

    for idx, p in enumerate(client.public_keys_dict.items()):
        client_sec_agg_id, client_public_keys = p
        if client_sec_agg_id == client.sec_agg_id:
            client.b_share_dict[client.sec_agg_id] = b_shares[idx]
            client.sk1_share_dict[client.sec_agg_id] = sk1_shares[idx]
        else:
            shared_key = sec_agg_primitives.generate_shared_key(
                client.sk2, sec_agg_primitives.bytes_to_public_key(client_public_keys.pk2))
            client.shared_key_2_dict[client_sec_agg_id] = shared_key
            plaintext = sec_agg_primitives.share_keys_plaintext_concat(
                client.sec_agg_id, client_sec_agg_id, b_shares[idx], sk1_shares[idx])
            ciphertext = sec_agg_primitives.encrypt(shared_key, plaintext)
            share_keys_packet = ShareKeysPacket(
                source=client.sec_agg_id, destination=client_sec_agg_id, ciphertext=ciphertext)
            share_keys_res.share_keys_res_list.append(share_keys_packet)

    log(INFO, "SecAgg Stage 2 Completed: Sent Shares via Packets")
    total_time = total_time + timeit.default_timer()
    if client.sec_agg_id == 3:
        f = open("log.txt", "a")
        f.write(f"Client without communication stage 2:{total_time} \n")
        f.close()
    return share_keys_res


def ask_vectors(client, ask_vectors_ins: AskVectorsIns) -> AskVectorsRes:
    # Receive shares and fit model
    packet_list = ask_vectors_ins.ask_vectors_in_list
    fit_ins = ask_vectors_ins.fit_ins
    available_clients: List[int] = []

    if len(packet_list) + 1 < client.threshold:
        raise Exception("Available neighbours number smaller than threshold")

    # decode all packets and verify all packets are valid. Save shares received
    for packet in packet_list:
        source = packet.source
        available_clients.append(source)
        destination = packet.destination
        ciphertext = packet.ciphertext
        if destination != client.sec_agg_id:
            raise Exception(
                "Received packet meant for another user. Not supposed to happen")
        shared_key = client.shared_key_2_dict[source]
        plaintext = sec_agg_primitives.decrypt(shared_key, ciphertext)
        try:
            plaintext_source, plaintext_destination, plaintext_b_share, plaintext_sk1_share = sec_agg_primitives.share_keys_plaintext_separate(
                plaintext)
        except:
            raise Exception(
                "Decryption of ciphertext failed. Not supposed to happen")
        if plaintext_source != source:
            raise Exception(
                "Received packet source is different from intended source. Not supposed to happen")
        if plaintext_destination != destination:
            raise Exception(
                "Received packet destination is different from intended destination. Not supposed to happen")
        client.b_share_dict[source] = plaintext_b_share
        client.sk1_share_dict[source] = plaintext_sk1_share

    # fit client
    # fit_res = client.client.fit(parameters_to_ndarrays(fit_ins.parameters), fit_ins.config)
    # fit_res test here
    parameters = fit_ins.parameters
    weights_factor = 1
    #
    # parameters = fit_res.parameters
    weights = parameters_to_ndarrays(parameters)
    # weights_factor = fit_res.num_examples

    # Quantize weight update vector
    quantized_weights = sec_agg_primitives.quantize(
        weights, client.clipping_range, client.target_range)

    # weights factor cannoot exceed maximum
    if weights_factor > client.max_weights_factor:
        weights_factor = client.max_weights_factor
        log(WARNING, "weights_factor exceeds allowed range and has been clipped. Either increase max_weights_factor, "
                     "or train with fewer data. (Or server is performing unweighted aggregation)")

    quantized_weights = sec_agg_primitives.weights_multiply(
        quantized_weights, weights_factor)
    quantized_weights = sec_agg_primitives.factor_weights_combine(
        weights_factor, quantized_weights)

    dimensions_list: List[Tuple] = [a.shape for a in quantized_weights]

    # add private mask
    private_mask = sec_agg_primitives.pseudo_rand_gen(
        client.b, client.mod_range, dimensions_list)
    quantized_weights = sec_agg_primitives.weights_addition(
        quantized_weights, private_mask)

    for client_id in available_clients:
        # add pairwise mask
        shared_key = sec_agg_primitives.generate_shared_key(
            client.sk1, sec_agg_primitives.bytes_to_public_key(client.public_keys_dict[client_id].pk1))
        pairwise_mask = sec_agg_primitives.pseudo_rand_gen(
            shared_key, client.mod_range, dimensions_list)
        if client.sec_agg_id > client_id:
            quantized_weights = sec_agg_primitives.weights_addition(
                quantized_weights, pairwise_mask)
        else:
            quantized_weights = sec_agg_primitives.weights_subtraction(
                quantized_weights, pairwise_mask)

    # Take mod of final weight update vector and return to server
    quantized_weights = sec_agg_primitives.weights_mod(
        quantized_weights, client.mod_range)
    log(INFO, "SecAgg Stage 3 Completed: Sent Vectors")
    return AskVectorsRes(parameters=ndarrays_to_parameters(quantized_weights))


#TODO add consistency check client

def unmask_vectors(client, unmask_vectors_ins: UnmaskVectorsIns) -> UnmaskVectorsRes:
    total_time = -timeit.default_timer()
    # Send private mask seed share for every avaliable client (including itclient)
    # Send first private key share for building pairwise mask for every dropped client
    available_clients = unmask_vectors_ins.available_clients
    if len(available_clients) < client.threshold:
        raise Exception("Available neighbours number smaller than threshold")
    dropout_clients = unmask_vectors_ins.dropout_clients
    share_dict: Dict[int, bytes] = {}
    for idx in available_clients:
        share_dict[idx] = client.b_share_dict[idx]
    for idx in dropout_clients:
        share_dict[idx] = client.sk1_share_dict[idx]
    log(INFO, "SecAgg Stage 4 Completed: Sent Shares for Unmasking")
    total_time = total_time + timeit.default_timer()
    if client.sec_agg_id == 3:
        f = open("log.txt", "a")
        f.write(f"Client without communication stage 4:{total_time} \n")
        f.close()
    return UnmaskVectorsRes(share_dict=share_dict)

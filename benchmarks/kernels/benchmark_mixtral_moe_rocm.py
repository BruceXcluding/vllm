import argparse
import json
import os
import sys

import torch
import torch.distributed as dist
import torch.multiprocessing as mp
import torch.nn.functional as F
import triton
import triton.language as tl
from tqdm import tqdm
from tuning_utils import (
    get_full_tuning_space,
    prune_configs,
    union_of_list_of_dicts,
)

from vllm import _custom_ops as ops
from vllm.model_executor.layers.fused_moe import (
    get_config_file_name,
    invoke_fused_moe_kernel,
    moe_align_block_size,
)


def main(args):
    world_size = args.numGPU
    mp.spawn(wrapper, args=(args, ), nprocs=world_size, join=False)


def wrapper(rank, args):
    dist.init_process_group("nccl", world_size=args.numGPU, rank=rank)
    device_id = rank

    batches = [
        1, 2, 4, 8, 16, 24, 32, 48, 64, 96, 128, 256, 512, 1024, 1536, 2048,
        3072, 4096
    ]
    for i in range(device_id, len(batches), args.numGPU):
        tune_batch(batches[i], model=args.model, TP=args.modelTP)


def tune_batch(bs, model, TP):
    device_id = torch.distributed.get_rank()

    if model == '8x7B':
        d_model = 4096
        model_intermediate_size = 14336
    elif model == '8x22B':
        d_model = 6144
        model_intermediate_size = 16384
    else:
        raise ValueError(f'Unsupported Mixtral model {model}')

    num_total_experts = 8
    top_k = 2
    tp_size = TP
    num_calls = 100

    full_configs = get_full_tuning_space()
    M1 = bs * 2
    N1 = model_intermediate_size * 2 // tp_size
    K1 = d_model
    prune_configs_1 = prune_configs(M1, N1, K1, full_configs)

    M2 = bs * 2
    N2 = d_model
    K2 = model_intermediate_size // tp_size
    prune_configs_2 = prune_configs(M2, N2, K2, full_configs)

    configs = union_of_list_of_dicts(prune_configs_1, prune_configs_2)

    best_config = None
    best_time_us = 1e20

    progress_bar = tqdm(total=len(configs),
                        desc=f"bs={bs:4d} device={device_id}",
                        position=device_id)

    with torch.cuda.device(device_id):
        for config in configs:
            progress_bar.update(1)
            # warmup
            try:
                run_timing(
                    num_calls=num_calls,
                    bs=bs,
                    d_model=d_model,
                    num_total_experts=num_total_experts,
                    top_k=top_k,
                    tp_size=tp_size,
                    model_intermediate_size=model_intermediate_size,
                    config=config,
                )
            except triton.runtime.autotuner.OutOfResources:
                continue

            # benchmark
            kernel_dur_ms = run_timing(
                num_calls=num_calls,
                bs=bs,
                d_model=d_model,
                num_total_experts=num_total_experts,
                top_k=top_k,
                tp_size=tp_size,
                model_intermediate_size=model_intermediate_size,
                config=config,
            )

            kernel_dur_us = 1000 * kernel_dur_ms

            if kernel_dur_us < best_time_us:
                best_config = config
                best_time_us = kernel_dur_us

    filename = get_config_file_name(num_total_experts,
                                    model_intermediate_size // tp_size,
                                    dtype=None)
    print(f"writing config to file {filename}")
    existing_content = {}
    if os.path.exists(filename):
        with open(filename, "r") as f:
            existing_content = json.load(f)
    existing_content[str(bs)] = best_config
    with open(filename, "w") as f:
        json.dump(existing_content, f, indent=4)
        f.write("\n")


def run_timing(
    num_calls: int,
    bs: int,
    d_model: int,
    num_total_experts: int,
    top_k: int,
    tp_size: int,
    model_intermediate_size: int,
    config,
) -> float:
    shard_intermediate_size = model_intermediate_size // tp_size

    device_ = "cuda"
    dtype_ = torch.float16

    hidden_states = torch.rand(
        (bs, d_model),
        device=device_,
        dtype=dtype_,
    )

    w1 = torch.rand(
        (num_total_experts, 2 * shard_intermediate_size, d_model),
        device=hidden_states.device,
        dtype=hidden_states.dtype,
    )

    w2 = torch.rand(
        (num_total_experts, d_model, shard_intermediate_size),
        device=hidden_states.device,
        dtype=hidden_states.dtype,
    )

    gating_output = F.softmax(
        torch.rand(
            (bs, num_total_experts),
            device=hidden_states.device,
            dtype=torch.float32,
        ),
        dim=-1,
    )

    ###### Stuff from fused moe ######

    assert (hidden_states.shape[0] == gating_output.shape[0]
            ), "Number of tokens mismatch"
    assert hidden_states.shape[1] == w1.shape[2], "Hidden size mismatch"
    assert gating_output.shape[1] == w1.shape[0], "Number of experts mismatch"
    assert hidden_states.is_contiguous(), "Hidden_states must be contiguous"
    assert w1.is_contiguous(), "Expert weights1 must be contiguous"
    assert w2.is_contiguous(), "Expert weights2 must be contiguous"
    assert hidden_states.dtype in [
        torch.float32, torch.float16, torch.bfloat16
    ]
    M, _ = hidden_states.shape
    E, N, _ = w1.shape
    topk_ = 2
    topk_weights = torch.empty(M,
                               topk_,
                               dtype=torch.float32,
                               device=hidden_states.device)
    topk_ids = torch.empty(M,
                           topk_,
                           dtype=torch.int32,
                           device=hidden_states.device)
    token_expert_indicies = torch.empty(M,
                                        topk_,
                                        dtype=torch.int32,
                                        device=hidden_states.device)
    ops.topk_softmax(
        topk_weights,
        topk_ids,
        token_expert_indicies,
        gating_output.float(),
    )
    del token_expert_indicies  # Not used. Will be used in the future.

    topk_weights = topk_weights / topk_weights.sum(dim=-1, keepdim=True)

    intermediate_cache1 = torch.empty(
        (M, topk_ids.shape[1], N),
        device=hidden_states.device,
        dtype=hidden_states.dtype,
    )
    intermediate_cache2 = torch.empty(
        (M * topk_ids.shape[1], N // 2),
        device=hidden_states.device,
        dtype=hidden_states.dtype,
    )
    intermediate_cache3 = torch.empty(
        (M, topk_ids.shape[1], w2.shape[1]),
        device=hidden_states.device,
        dtype=hidden_states.dtype,
    )

    sorted_token_ids, expert_ids, num_tokens_post_padded = moe_align_block_size(
        topk_ids, config["BLOCK_SIZE_M"], E)

    ##################################

    start_event = torch.cuda.Event(enable_timing=True)
    end_event = torch.cuda.Event(enable_timing=True)

    start_event.record()
    for _ in range(num_calls):
        invoke_fused_moe_kernel(
            hidden_states,
            w1,
            intermediate_cache1,
            None,  # a1_scale
            None,  # w1_scale
            topk_weights,
            topk_ids,
            sorted_token_ids,
            expert_ids,
            num_tokens_post_padded,
            False,
            topk_ids.shape[1],
            config,
            compute_type=(tl.bfloat16 if hidden_states.dtype == torch.bfloat16
                          else tl.float16),
            use_fp8_w8a8=False,
            use_int8_w8a16=False)

        ops.silu_and_mul(intermediate_cache2, intermediate_cache1.view(-1, N))

        invoke_fused_moe_kernel(
            intermediate_cache2,
            w2,
            intermediate_cache3,
            None,  # a2_scale
            None,  # w2_scale
            topk_weights,
            topk_ids,
            sorted_token_ids,
            expert_ids,
            num_tokens_post_padded,
            True,
            1,
            config,
            compute_type=(tl.bfloat16 if hidden_states.dtype == torch.bfloat16
                          else tl.float16),
            use_fp8_w8a8=False,
            use_int8_w8a16=False)

    end_event.record()
    end_event.synchronize()

    dur_ms = start_event.elapsed_time(end_event) / num_calls
    return dur_ms


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        prog="benchmark_mixtral_moe_rocm",
        description="Distributed tuning script for the fused_moe kernel.")
    parser.add_argument('--model',
                        type=str,
                        choices=['8x7B', '8x22B'],
                        help='The Mixtral model to benchmark')
    parser.add_argument(
        "--modelTP",
        type=int,
        choices=[8, 4, 2, 1],
        help="Specify the TP value that the model will actually run on",
        required=True,
    )
    parser.add_argument(
        "--numGPU",
        type=int,
        choices=[8, 4, 2, 1],
        help="Total number of GPUs to use for tuning",
        required=True,
    )
    args = parser.parse_args()
    if "LOCAL_RANK" not in os.environ:
        print("Please use torchrun to launch this multi-gpu script. E.g:")
        print("\ttorchrun benchmark_mixtral_moe_rocm.py",
              "--model 8x7B --modelTP 4 --numGPU 2")
        print("Exiting...")
        exit()

    print(f"Running tuning for {args.model} model")
    print(f"Model TP is set to: {args.modelTP}")
    print(f"GPUs being used for tuning: {args.numGPU}")
    sys.exit(main(args))

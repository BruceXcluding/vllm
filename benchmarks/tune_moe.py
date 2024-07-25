import argparse
import json
import os
import sys

import torch
import torch.nn.functional as F
import triton
import triton.language as tl
from tqdm import tqdm

import vllm._moe_C as moe_kernels
from vllm._C import ops
from fused_moe import run_timing



def parse_args():
    parser = argparse.ArgumentParser(
        prog="tune_moe",
        description="Tune the fused_moe kernel")
    parser.add_argument(
        "-bs",
        type=int,
        required=True,
    )
    parser.add_argument(
        "-d_model",
        type=int,
        required=True,
    )
    parser.add_argument(
        "-num_expt",
        type=int,
        required=True,
    )
    parser.add_argument(
        "-top_k",
        type=int,
        required=True,
    )
    parser.add_argument(
        "-tp_size",
        type=int,
        required=True,
    )
    parser.add_argument(
        "-inter_size",
        type=int,
        required=True,
    )
    # parser.add_argument(
    #     "-config_file",
    #     type=str,
    #     required=True,
    # )
    args = parser.parse_args()
    return args


def main():
    args = parse_args()
    bs = args.bs
    d_model = args.d_model
    num_total_experts = args.num_expt
    top_k = args.top_k
    tp_size = args.tp_size
    model_intermediate_size = args.inter_size
    # config_file_name = args.config_file
    # with open(config_file_name, 'r') as f:
    #     configs = json.load(f)
    # config = configs[str(bs)]
    # tune_time = config["time"]
    # print(f"tune_time = {tune_time} (us)")
    # config.pop("time")

    run_timing(
        bs=bs,
        d_model=d_model,
        num_total_experts=num_total_experts,
        top_k=top_k,
        tp_size=tp_size,
        model_intermediate_size=model_intermediate_size,
        # config=config,
    )

if __name__ == "__main__":
    main()
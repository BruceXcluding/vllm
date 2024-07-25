import pandas as pd
import argparse

def parse_args():
    parser = argparse.ArgumentParser(
        prog="get_kernel_time",
        description="Tune the fused_moe kernel")
    parser.add_argument(
        "-rocprof_file",
        type=str,
        required=True,
    )
    parser.add_argument(
        "-ki",
        type=int,
        required=True,
    )
    args = parser.parse_args()
    return args

def main():
    args = parse_args()
    result_file_name = args.rocprof_file
    kernel_index = args.ki
    df_prof = pd.read_csv(result_file_name)
    df = df_prof[df_prof['KernelName'].str.contains("fused_moe_kernel.kd")]
    time_list = df['DurationNs'].tail(20).values.tolist()
    # print(f"df_list = {time_list}")
    kernel_time = [round(time_list[i]/1000, 2) for i in range(len(time_list)) if i % 2 == kernel_index]
    # kernel2_time = [round(time_list[i]/1000, 2) for i in range(len(time_list)) if i % 2 == 1]

    print(f"kerle_{kernel_index}_time (us): {kernel_time}")

if __name__ == "__main__":
    main()
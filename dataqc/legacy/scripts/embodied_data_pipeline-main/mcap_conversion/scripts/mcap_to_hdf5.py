#!/usr/bin/env python3
# -- coding: UTF-8
"""
Run the full MCAP -> ALOHA files -> sync.txt -> HDF5 pipeline.

Example:
    DATA_ROOT=../../data
    python3 mcap_to_hdf5.py \
        --mcapPath "$DATA_ROOT/raw_mcap/data_1" \
        --output "$DATA_ROOT/hdf5_files"
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DATA_TOOLS_DIR = SCRIPT_DIR.parent
DEFAULT_ALOHA_YAML = DATA_TOOLS_DIR / "topic_configs" / "aloha_data_params.yaml"


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Convert MCAP data to HDF5 by calling mcap_to_aloha_data.py, data_sync.py, and data_to_hdf5.py."
    )
    parser.add_argument(
        "--mcapPath",
        required=True,
        help="Input .mcap file or a directory containing .mcap files.",
    )
    output_group = parser.add_mutually_exclusive_group(required=True)
    output_group.add_argument(
        "--output",
        help="Output directory. The HDF5 file is saved as <output>/<episodeName>.hdf5.",
    )
    output_group.add_argument(
        "--outputHdf5",
        help="Deprecated compatibility option: output HDF5 file path, or an output directory.",
    )
    parser.add_argument(
        "--workDir",
        default="",
        help="Directory for intermediate ALOHA files. Default: <output directory>/mcap_to_hdf5_work.",
    )
    parser.add_argument(
        "--episodeName",
        default="",
        help="Episode name used by data_sync.py and data_to_hdf5.py. Default: input directory name.",
    )
    parser.add_argument(
        "--type",
        default="aloha",
        help="Dataset config type used by data_sync.py and data_to_hdf5.py. Default: aloha.",
    )
    parser.add_argument(
        "--alohaYaml",
        default=str(DEFAULT_ALOHA_YAML),
        help="YAML topic mapping used by mcap_to_aloha_data.py.",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable used to run the three existing scripts. Default: current Python.",
    )
    parser.add_argument(
        "--timeDiffLimit",
        type=float,
        default=0.03,
        help="Timestamp sync tolerance passed to data_sync.py. Default: 0.03.",
    )
    parser.add_argument(
        "--embedData",
        action="store_true",
        help="Store image arrays inside HDF5 instead of file indexes. This can use much more memory and disk.",
    )
    parser.add_argument(
        "--useCameraPointCloud",
        action="store_true",
        help="Pass --useCameraPointCloud True to data_to_hdf5.py.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Overwrite the generated HDF5 file if it already exists.",
    )
    parser.add_argument(
        "--allowPartialRecover",
        action="store_true",
        help=(
            "Allow mcap_to_aloha_data.py to convert a partially recovered MCAP when rosbag2_py "
            "cannot read the original file. Default refuses partial recovery."
        ),
    )
    parser.add_argument(
        "--recordTimeTopic",
        dest="record_time_topics",
        action="append",
        default=[],
        help="Use MCAP record time for this exact topic; repeatable.",
    )
    parser.add_argument(
        "--dryRun",
        action="store_true",
        help="Print commands and paths without running conversion.",
    )
    return parser.parse_args(argv)


def collect_mcap_files(mcap_path):
    if mcap_path.is_file():
        if mcap_path.suffix != ".mcap":
            raise ValueError(f"Input file is not an .mcap file: {mcap_path}")
        return [mcap_path]

    if not mcap_path.is_dir():
        raise FileNotFoundError(f"Input path does not exist: {mcap_path}")

    direct_files = sorted(mcap_path.glob("*.mcap"))
    if direct_files:
        return direct_files

    recursive_files = sorted(mcap_path.rglob("*.mcap"))
    if recursive_files:
        return recursive_files

    raise FileNotFoundError(f"No .mcap files found under: {mcap_path}")


def infer_episode_name(mcap_path, mcap_files):
    if mcap_path.is_file():
        return mcap_path.parent.name
    return mcap_path.name or mcap_files[0].parent.name


def resolve_output_path(args, episode_name):
    if args.output:
        return Path(args.output).expanduser().resolve() / f"{episode_name}.hdf5"

    output_path = Path(args.outputHdf5).expanduser().resolve()
    if output_path.suffix.lower() in {".hdf5", ".h5"}:
        return output_path
    return output_path / f"{episode_name}.hdf5"


def resolve_topic_yaml(dataset_type, aloha_yaml):
    requested = Path(aloha_yaml).expanduser().resolve()
    if dataset_type != "aloha" and requested == DEFAULT_ALOHA_YAML.resolve():
        typed_yaml = DATA_TOOLS_DIR / "topic_configs" / f"{dataset_type}_data_params.yaml"
        if typed_yaml.exists():
            return typed_yaml.resolve()
    return requested


def prepare_staged_input(mcap_files, stage_episode_dir, dry_run=False):
    if dry_run:
        return

    if stage_episode_dir.exists():
        shutil.rmtree(stage_episode_dir)
    stage_episode_dir.mkdir(parents=True, exist_ok=True)
    used_names = set()
    for idx, source in enumerate(mcap_files):
        target_name = source.name
        if target_name in used_names:
            target_name = f"{idx:04d}_{source.name}"
        used_names.add(target_name)

        target = stage_episode_dir / target_name
        if target.exists() or target.is_symlink():
            target.unlink()
        os.symlink(source.resolve(), target)


def reset_output_episode(episode_dir, dry_run=False):
    if dry_run or not episode_dir.exists():
        return
    shutil.rmtree(episode_dir)


def run_command(cmd, dry_run=False):
    print("+ " + " ".join(str(part) for part in cmd))
    if dry_run:
        return
    subprocess.run(cmd, cwd=str(SCRIPT_DIR), check=True)


def copy_or_move_hdf5(generated_hdf5, output_hdf5, overwrite, dry_run=False):
    if generated_hdf5.resolve() == output_hdf5.resolve():
        return

    if output_hdf5.exists():
        if not overwrite:
            raise FileExistsError(f"Output already exists, use --overwrite to replace it: {output_hdf5}")
        if not dry_run:
            output_hdf5.unlink()

    if dry_run:
        print(f"+ copy {generated_hdf5} {output_hdf5}")
        return

    output_hdf5.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(generated_hdf5, output_hdf5)


def build_mcap_to_aloha_command(args, stage_input_dir, work_dir, topic_yaml):
    command = [
        args.python,
        "mcap_to_aloha_data.py",
        "--datasetDir",
        str(stage_input_dir),
        "--targetDir",
        str(work_dir),
        "--alohaYaml",
        str(topic_yaml),
    ]
    if args.allowPartialRecover:
        command.append("--allowPartialRecover")
    for topic in args.record_time_topics:
        command.extend(["--recordTimeTopic", topic])
    return command


def main(argv=None):
    args = parse_args(argv)

    mcap_path = Path(args.mcapPath).expanduser().resolve()
    mcap_files = collect_mcap_files(mcap_path)
    episode_name = args.episodeName or infer_episode_name(mcap_path, mcap_files)
    output_hdf5 = resolve_output_path(args, episode_name)
    topic_yaml = resolve_topic_yaml(args.type, args.alohaYaml)

    if output_hdf5.exists() and not args.overwrite:
        raise FileExistsError(f"Output already exists, use --overwrite to replace it: {output_hdf5}")

    work_dir = Path(args.workDir).expanduser().resolve() if args.workDir else output_hdf5.parent / "mcap_to_hdf5_work"
    stage_input_dir = work_dir / "_input"
    stage_episode_dir = stage_input_dir / episode_name
    aloha_root = work_dir / "aloha"
    episode_dir = aloha_root / episode_name

    generated_hdf5 = episode_dir / f"{episode_name}.hdf5"
    if args.embedData:
        generated_hdf5 = output_hdf5.parent / f"{episode_name}.hdf5"

    print(f"Input MCAP files: {len(mcap_files)}")
    for file_path in mcap_files:
        print(f"  {file_path}")
    print(f"Episode name: {episode_name}")
    print(f"Work dir: {work_dir}")
    print(f"Output HDF5: {output_hdf5}")
    print(f"Topic YAML: {topic_yaml}")
    if not args.embedData:
        print("Mode: index HDF5. Keep the intermediate ALOHA directory because image datasets store relative file paths.")
    else:
        print("Mode: embedded HDF5. Images are read into the HDF5 file.")

    if not args.dryRun:
        output_hdf5.parent.mkdir(parents=True, exist_ok=True)

    reset_output_episode(episode_dir, args.dryRun)
    prepare_staged_input(mcap_files, stage_episode_dir, args.dryRun)

    mcap_to_aloha_cmd = build_mcap_to_aloha_command(
        args,
        stage_input_dir,
        work_dir,
        topic_yaml,
    )
    run_command(mcap_to_aloha_cmd, args.dryRun)

    run_command(
        [
            args.python,
            "data_sync.py",
            "--type",
            args.type,
            "--datasetDir",
            str(aloha_root),
            "--episodeName",
            episode_name,
            "--timeDiffLimit",
            str(args.timeDiffLimit),
            "--paramsFile",
            str(topic_yaml),
        ],
        args.dryRun,
    )

    data_to_hdf5_cmd = [
        args.python,
        "data_to_hdf5.py",
        "--type",
        args.type,
        "--datasetDir",
        str(aloha_root),
        "--episodeName",
        episode_name,
        "--paramsFile",
        str(topic_yaml),
    ]
    if args.useCameraPointCloud:
        data_to_hdf5_cmd.extend(["--useCameraPointCloud", "True"])
    if args.embedData:
        data_to_hdf5_cmd.extend(["--useIndex", "", "--targetDir", str(output_hdf5.parent)])

    run_command(data_to_hdf5_cmd, args.dryRun)

    copy_or_move_hdf5(generated_hdf5, output_hdf5, args.overwrite, args.dryRun)
    print(f"Done: {output_hdf5}")


if __name__ == "__main__":
    main()

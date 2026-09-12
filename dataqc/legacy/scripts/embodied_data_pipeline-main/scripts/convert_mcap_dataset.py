#!/usr/bin/env python3
"""Run MCAP -> HDF5 episode conversion for one dataset name inside data/."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import subprocess
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_ROOT = ROOT / "data"
SCRIPT_DIR = ROOT / "mcap_conversion" / "scripts"
DEFAULT_ALOHA_YAML = ROOT / "mcap_conversion" / "topic_configs" / "aloha_data_params.yaml"
GENERATED_INPUT_PREFIXES = ("hdf5_episodes", "qc_reports", "lerobot", "logs")
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from camera_layouts import CAMERA_LAYOUTS, episode_artifacts_complete


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Batch convert data/raw_mcap/<dataset_name> to data/hdf5_episodes/<dataset_name>, "
            "or convert a custom input root to a custom output root."
        )
    )
    parser.add_argument("--dataset-name", required=True, help="Dataset folder name, for example grasp_bottle2.")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT, help="Data root. Default: ./data.")
    parser.add_argument(
        "--input-root",
        type=Path,
        default=None,
        help="Input .mcap file or directory. Default: <data-root>/raw_mcap/<dataset-name>.",
    )
    parser.add_argument(
        "--output-root",
        type=Path,
        default=None,
        help="Output episode root. Default: <data-root>/hdf5_episodes/<dataset-name>.",
    )
    parser.add_argument(
        "--profile",
        default="robot_profiles/aloha.yaml",
        help="Robot profile YAML. Default: robot_profiles/aloha.yaml.",
    )
    parser.add_argument("--type", default="aloha", help="Conversion config type. Default: aloha.")
    parser.add_argument(
        "--aloha-yaml",
        default=str(DEFAULT_ALOHA_YAML),
        help="Topic mapping passed to mcap_to_icra_episode.py.",
    )
    parser.add_argument(
        "--camera-layout",
        choices=tuple(CAMERA_LAYOUTS),
        default="three_camera",
        help="Camera/video layout. Default: three_camera.",
    )
    parser.add_argument("--text", default="", help="Optional task text passed to mcap_to_icra_episode.py.")
    parser.add_argument("--textZh", default="", help="Optional Chinese task text passed to mcap_to_icra_episode.py.")
    parser.add_argument("--python", default="python3", help="Python executable used to run conversion.")
    parser.add_argument("--timeDiffLimit", type=float, default=0.03, help="Timestamp sync tolerance.")
    parser.add_argument("--fps", type=float, default=30.0, help="Output MP4 FPS.")
    parser.add_argument(
        "--gpu-encode-videos",
        action="store_true",
        help="Use ffmpeg NVENC for output MP4 transcode, with CPU H.264 fallback.",
    )
    parser.add_argument("--gpu-device", default="0", help="GPU index used by NVENC. Default: 0.")
    parser.add_argument(
        "--gpu-video-encoder",
        choices=("h264_nvenc", "hevc_nvenc", "av1_nvenc"),
        default="h264_nvenc",
        help="ffmpeg NVENC encoder used with --gpu-encode-videos. Default: h264_nvenc.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=1,
        help="Number of episode conversions to run in parallel. Default: 1.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite managed outputs.")
    parser.add_argument("--cleanupIntermediate", action="store_true", help="Remove _work after conversion.")
    parser.add_argument(
        "--allowPartialRecover",
        action="store_true",
        help="Allow conversion from a partially recovered MCAP if the original cannot be read by rosbag2_py.",
    )
    parser.add_argument(
        "--record-time-topic",
        action="append",
        default=[],
        help="Use MCAP record time for this exact topic; repeatable.",
    )
    parser.add_argument("--dryRun", action="store_true", help="Print commands without running.")
    return parser.parse_args(argv)


def episode_id_from_name(name: str) -> int:
    digits = "".join(ch for ch in name if ch.isdigit())
    return int(digits) if digits else 0


def run(cmd: list[str], *, dry_run: bool) -> None:
    print("+ " + " ".join(cmd))
    if not dry_run:
        subprocess.run(cmd, cwd=SCRIPT_DIR, check=True)


def output_complete(out_dir: Path, camera_layout: str = "three_camera") -> bool:
    return episode_artifacts_complete(out_dir, camera_layout)


def update_existing_task(out_dir: Path, task_text: str) -> None:
    task_text = task_text.strip()
    if not task_text:
        return
    meta_path = out_dir / "meta" / "episode_meta.json"
    h5_path = out_dir / "states" / "aligned_joints.h5"
    if meta_path.is_file():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            meta = {}
        if not isinstance(meta, dict):
            meta = {}
        meta["task"] = task_text
        meta["tasks"] = [task_text]
        meta["full_instructions_en"] = [task_text]
        tmp_path = meta_path.with_suffix(meta_path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp_path.replace(meta_path)
    if h5_path.is_file():
        import h5py  # type: ignore

        with h5py.File(h5_path, "a") as file_obj:
            file_obj.attrs["task"] = task_text


def managed_output_exists(out_dir: Path) -> bool:
    managed_paths = [
        out_dir / "states" / "aligned_joints.h5",
        out_dir / "videos",
        out_dir / "meta",
        out_dir / "_work",
    ]
    return any(path.exists() or path.is_symlink() for path in managed_paths)


def contains_mcap(path: Path) -> bool:
    if path.is_file():
        return path.suffix == ".mcap"
    if path.is_dir():
        return any(path.rglob("*.mcap"))
    return False


def is_episode_input_candidate(path: Path) -> bool:
    if path.is_dir() and path.name.startswith(GENERATED_INPUT_PREFIXES):
        return False
    return path.exists() and contains_mcap(path)


def resolve_profile_path(profile: str | Path) -> Path:
    profile_path = Path(profile)
    if not profile_path.is_absolute():
        profile_path = ROOT / profile_path
    return profile_path.resolve()


def build_episode_command(
    args: argparse.Namespace,
    mcap_input: Path,
    out_dir: Path,
    *,
    profile_path: Path | None = None,
    overwrite: bool | None = None,
) -> list[str]:
    name = mcap_input.stem if mcap_input.is_file() and mcap_input.suffix == ".mcap" else mcap_input.name
    command = [
        args.python,
        "mcap_to_icra_episode.py",
        "--mcapPath",
        str(mcap_input),
        "--output",
        str(out_dir),
        "--episodeName",
        name,
        "--episodeId",
        str(episode_id_from_name(name)),
        "--profile",
        str(profile_path or resolve_profile_path(args.profile)),
        "--type",
        args.type,
        "--alohaYaml",
        str(Path(args.aloha_yaml).expanduser().resolve()),
        "--cameraLayout",
        args.camera_layout,
        "--timeDiffLimit",
        str(args.timeDiffLimit),
        "--fps",
        str(args.fps),
    ]
    if args.text:
        command.extend(["--text", args.text])
    if args.textZh:
        command.extend(["--textZh", args.textZh])
    if args.gpu_encode_videos:
        command.extend(
            [
                "--gpu-encode-videos",
                "--gpu-device",
                args.gpu_device,
                "--gpu-video-encoder",
                args.gpu_video_encoder,
            ]
        )
    if args.overwrite if overwrite is None else overwrite:
        command.append("--overwrite")
    if args.allowPartialRecover:
        command.append("--allowPartialRecover")
    if args.cleanupIntermediate:
        command.append("--cleanupIntermediate")
    for topic in args.record_time_topic:
        command.extend(["--recordTimeTopic", topic])
    return command


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.jobs < 1:
        raise ValueError("--jobs must be >= 1")

    data_root = args.data_root.expanduser().resolve()
    input_root = args.input_root.expanduser().resolve() if args.input_root else data_root / "raw_mcap" / args.dataset_name
    output_root = (
        args.output_root.expanduser().resolve()
        if args.output_root
        else data_root / "hdf5_episodes" / args.dataset_name
    )
    if not input_root.exists():
        if args.dryRun:
            print(f"Input root would be: {input_root}")
            print(f"Output root would be: {output_root}")
            print("No conversion commands were expanded because the input root does not exist.")
            return 0
        raise FileNotFoundError(input_root)
    output_root.mkdir(parents=True, exist_ok=True)
    profile_path = resolve_profile_path(args.profile)

    if input_root.is_file():
        episode_inputs = [input_root] if contains_mcap(input_root) else []
    else:
        episode_inputs = sorted(
            (path for path in input_root.iterdir() if is_episode_input_candidate(path)),
            key=lambda p: p.name,
        )
    if not episode_inputs:
        raise FileNotFoundError(f"No episode inputs found under {input_root}")

    conversion_jobs: list[tuple[str, list[str]]] = []
    for mcap_input in episode_inputs:
        name = mcap_input.stem if mcap_input.is_file() and mcap_input.suffix == ".mcap" else mcap_input.name
        out_dir = output_root / name
        if output_complete(out_dir, args.camera_layout) and not args.overwrite:
            update_existing_task(out_dir, args.text)
            print(f"==== skip existing {name} ====")
            continue
        episode_overwrite = args.overwrite
        if managed_output_exists(out_dir) and not episode_overwrite:
            print(f"==== overwrite incomplete existing {name} ====")
            episode_overwrite = True
        cmd = build_episode_command(
            args,
            mcap_input,
            out_dir,
            profile_path=profile_path,
            overwrite=episode_overwrite,
        )
        conversion_jobs.append((name, cmd))

    if args.dryRun or args.jobs == 1:
        for name, cmd in conversion_jobs:
            print(f"==== convert {name} ====")
            run(cmd, dry_run=args.dryRun)
        return 0

    print(f"Parallel jobs: {args.jobs}")

    def run_conversion(item: tuple[str, list[str]]) -> str:
        name, cmd = item
        print(f"==== convert {name} ====")
        run(cmd, dry_run=False)
        return name

    failures: list[tuple[str, BaseException]] = []
    with ThreadPoolExecutor(max_workers=args.jobs) as executor:
        futures = {executor.submit(run_conversion, item): item[0] for item in conversion_jobs}
        for future in as_completed(futures):
            name = futures[future]
            try:
                future.result()
            except BaseException as exc:
                failures.append((name, exc))
                print(f"==== failed {name}: {exc} ====")
            else:
                print(f"==== done {name} ====")

    if failures:
        print("Failed conversions:")
        for name, exc in failures:
            print(f"  {name}: {exc}")
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

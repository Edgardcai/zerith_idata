import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

import cv2
import h5py
import numpy as np

CAMS = ["cam_high", "cam_left_wrist", "cam_right_wrist"]
NAMES = (
    [f"left_joint_{i}" for i in range(1, 8)]
    + ["left_gripper"]
    + [f"right_joint_{i}" for i in range(1, 8)]
    + [
        "right_gripper",
        "lift_m",
        "waist_pitch",
        "waist_yaw",
        "head_yaw",
        "head_pitch",
        "base_vx",
        "base_wz",
    ]
)
PARTS = {
    "arm/position": 14,
    "effector/position": 2,
    "waist/position": 3,
    "head/position": 2,
    "base/velocity": 2,
}


def clean(x):
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, np.ndarray):
        return clean(x.tolist())
    if isinstance(x, np.generic):
        return clean(x.item())
    if isinstance(x, float) and not np.isfinite(x):
        return None
    if isinstance(x, bytes):
        return x.decode(errors="replace")
    if isinstance(x, Path):
        return str(x)
    return x


def write_json(p, data):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(
        json.dumps(clean(data), ensure_ascii=False, indent=2, allow_nan=False)
    )
    os.replace(tmp, p)


def read_json(p, default=None):
    p = Path(p)
    return (
        json.loads(p.read_text())
        if p.exists()
        else ({} if default is None else default)
    )


def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1024 * 1024), b""):
            h.update(b)
    return h.hexdigest()


def fingerprint(root):
    root = Path(root)
    paths = sorted(
        [
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix in (".hdf5", ".h5", ".mp4", ".json")
        ]
    )
    return {
        str(p.relative_to(root)): dict(
            size=p.stat().st_size, mtime=p.stat().st_mtime_ns, sha256=sha(p)
        )
        for p in paths
    }


def discover(root):
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("数据目录不存在")
    roots = {p.parent for p in root.rglob("episode.hdf5")}
    for name in ("aligned_joints.h5", "aligned_joints.hdf5"):
        roots.update(p.parent.parent for p in root.rglob(name) if p.parent.name == 'states')
    return [str(p) for p in sorted(roots, key=lambda p: [
        int(x) if x.isdigit() else x for x in re.split(r"(\d+)", str(p))])]


def is_simulation(root):
    from .simulation import path_for
    return path_for(root) is not None


def hdf5_path(root):
    from .simulation import path_for
    sim = path_for(root)
    real = Path(root) / 'episode.hdf5'
    if sim and real.is_file():
        raise ValueError('目录同时含有真机和仿真 HDF5，请分开存放')
    return sim or real



def vectors(f, prefix):
    parts = {k: np.asarray(f[prefix + "/" + k], dtype=np.float64) for k in PARTS}
    for k, n in PARTS.items():
        if parts[k].ndim != 2 or parts[k].shape[1] != n:
            raise ValueError(f"{prefix}/{k} 应为 (T,{n})，实际 {parts[k].shape}")
    a, g, w, h, b = [parts[k] for k in PARTS]
    return np.concatenate([a[:, :7], g[:, :1], a[:, 7:], g[:, 1:], w, h, b], axis=1)


def load(root):
    root = Path(root)
    path = hdf5_path(root)
    if path.name != 'episode.hdf5':
        from .simulation import read
        return read(root)
    with h5py.File(path, "r") as f:
        state, action = vectors(f, "observation/state"), vectors(f, "action")
        t = np.asarray(f["timestamp/t"], dtype=float) / 1000
        attrs = clean(dict(f.attrs))
        trans = np.asarray(f.get("subtask_transitions", []), dtype=int).tolist()
    return dict(
        root=root,
        state=state,
        action=action,
        t=t,
        attrs=attrs,
        n=len(state),
        transitions=trans,
        task=str(attrs.get("task_name") or read_json(root / "collection_task.json").get('config',{}).get('task_name') or read_json(root / "episode_meta.json").get('task') or ''),
        meta=read_json(root / "episode_meta.json"),
        collection=read_json(root / "collection_task.json"),
    )


def video_path(root, cam):
    if is_simulation(root):
        from .simulation import video
        return video(root, cam)
    return Path(root) / "videos/rs" / f"{cam}.mp4"


def frame(root, cam, i, max_width=768):
    cap = open_video(video_path(root, cam))
    cap.set(cv2.CAP_PROP_POS_FRAMES, int(i))
    ok, img = cap.read()
    cap.release()
    if not ok:
        raise ValueError(f"{cam} 第 {i} 帧读取失败")
    if max_width and img.shape[1] > max_width:
        img = cv2.resize(
            img, (max_width, round(img.shape[0] * max_width / img.shape[1]))
        )
    return img


ITEM = r"(?!\s)(?:(?!\b(?:with|then|grasp)\b)[^\r\n])+?(?<!\s)"
DOUBLE = re.compile(
    r"Grasp ("
    + ITEM
    + r") with the left hand and then grasp ("
    + ITEM
    + r") with the right hand"
)
SINGLE = re.compile(r"Grasp (" + ITEM + r") with the (left|right) hand")


def parse_task(text):
    # Raw recordings with or without a final period share the same semantics.
    text=text[:-1] if text.endswith('.') else text
    if "  " in text:
        return None
    m = DOUBLE.fullmatch(text)
    if m:
        return {"left": m[1], "right": m[2]}
    m = SINGLE.fullmatch(text)
    return {m[2]: m[1]} if m else None


def normalized_task(text):
    return " ".join(text.split())


def spans(mask):
    v = np.r_[False, np.asarray(mask, dtype=bool), False].astype(int)
    starts = np.where(np.diff(v) == 1)[0]
    ends = np.where(np.diff(v) == -1)[0]
    return [(int(s), int(e)) for s, e in zip(starts, ends)]


def stationary_spans(s, a):
    if len(s) < 2 or s.shape != a.shape:
        return []
    still = (np.max(abs(np.diff(s, axis=0)), axis=1) <= 0.01) & (
        np.max(abs(np.diff(a, axis=0)), axis=1) <= 0.01
    )
    base = np.maximum(
        np.max(abs(s[:, 21:23]), axis=1), np.max(abs(a[:, 21:23]), axis=1)
    )
    still &= (base[:-1] <= 0.0001) & (base[1:] <= 0.0001)
    return [(b, e + 1) for b, e in spans(still)]


def gripper_events(a):
    out = []
    for hand, col in [("left", 7), ("right", 15)]:
        closed = False
        for i, v in enumerate(a[:, col]):
            if not closed and v >= 0.8:
                out.append(dict(hand=hand, kind="close", frame=i))
                closed = True
            elif closed and v <= 0.7:
                out.append(dict(hand=hand, kind="open", frame=i))
                closed = False
    return out


def open_video(path):
    """Set FFmpeg decoder threads explicitly inside conversion workers."""
    if os.environ.get('DATAQC_WORKER_THREADS')=='1':
        return cv2.VideoCapture(str(path),cv2.CAP_FFMPEG,[cv2.CAP_PROP_N_THREADS,1])
    return cv2.VideoCapture(str(path))


def encode_selection(src, dst, indices):
    """Decode exact source frames; prefer NVENC and preserve presentation order."""
    from .video_encoding import selected_encoder, encoder_args, nvenc_slot, _DEVICE
    selected = list(map(int, indices))
    if not selected or selected != sorted(set(selected)) or selected[0] < 0:
        raise ValueError("视频帧索引必须非空、递增且不重复")
    args, label = selected_encoder()
    try:
        if 'h264_nvenc' in args:
            with nvenc_slot(_DEVICE.get()):_encode_selected_frames(src,dst,selected,args)
        else:_encode_selected_frames(src,dst,selected,args)
    except (BrokenPipeError, VideoEncodingError):
        if 'h264_nvenc' not in args:
            raise
        print("GPU 视频编码失败，使用 CPU 重试同一帧序列", flush=True)
        _encode_selected_frames(src, dst, selected, encoder_args('cpu'))


class VideoEncodingError(ValueError):
    pass


def _encode_selected_frames(src, dst, indices, encoder):
    import tempfile
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    cap = open_video(src)
    ok, img = cap.read()
    if not ok:
        cap.release()
        raise ValueError("视频不能解码")
    h, w = img.shape[:2]
    cmd = ['ffmpeg', '-v', 'error', '-nostdin', '-y', '-f', 'rawvideo',
           '-pix_fmt', 'bgr24', '-s', f'{w}x{h}', '-r', '30', '-i', 'pipe:0',
           '-an', '-filter_threads', '1', *encoder, '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(dst)]
    proc = None
    try:
        with tempfile.TemporaryFile() as errors:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errors)
            selected = set(indices)
            count = 0
            i = 0
            while ok and i <= indices[-1]:
                if i in selected:
                    proc.stdin.write(img.tobytes())
                    count += 1
                i += 1
                if i <= indices[-1]:
                    ok, img = cap.read()
            proc.stdin.close()
            rc = proc.wait(timeout=120)
            errors.seek(0)
            error = errors.read().decode(errors='replace')
            if rc:
                raise VideoEncodingError(f"视频编码失败：{error[:300]}")
            if count != len(indices):
                raise ValueError(f"视频导出不完整 {count}/{len(indices)}")
    finally:
        cap.release()
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass

"""NVENC video encoding with an explicit CPU option and a checked fallback."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import subprocess
import os

_DEVICE = ContextVar('video_encoding_device', default='0')


@contextmanager
def video_encoding(device):
    token = _DEVICE.set(str(device or 'cpu'))
    try:
        yield
    finally:
        _DEVICE.reset(token)


def encoder_args(device=None):
    device = str(_DEVICE.get() if device is None else device)
    if device in ('', 'cpu', 'none'):
        return ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '18', '-threads', os.environ.get('DATAQC_WORKER_THREADS','2')]
    device = device.removeprefix('cuda:')
    if device == 'cuda':
        device = '0'
    return ['-c:v', 'h264_nvenc', '-gpu', device, '-preset', 'p4', '-tune', 'hq', '-rc', 'constqp', '-qp', '18', '-threads', '1']


@lru_cache(maxsize=8)
def available_encoder(device):
    args = encoder_args(device)
    if 'h264_nvenc' not in args:
        return args, 'CPU / libx264'
    try:
        with nvenc_slot(device):
            test = subprocess.run(['ffmpeg', '-v', 'error', '-nostdin', '-filter_threads', '1', '-f', 'lavfi', '-i',
                                   'color=size=640x480:rate=30', '-frames:v', '1', *args,
                                   '-pix_fmt', 'yuv420p', '-f', 'null', '-'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
        if test.returncode == 0:
            return args, f'GPU {device} / NVENC H.264'
    except (OSError, subprocess.TimeoutExpired):
        pass
    return encoder_args('cpu'), 'CPU / libx264（GPU 编码不可用，已回退）'


def selected_encoder():
    return available_encoder(_DEVICE.get())


def can_copy_video(path):
    """Only reuse videos that already match the LeRobot H.264 output contract."""
    import json
    try:
        result = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0',
                                 '-show_entries', 'stream=codec_name,pix_fmt,r_frame_rate',
                                 '-of', 'json', str(path)], capture_output=True, text=True, timeout=20, check=True)
        stream = json.loads(result.stdout)['streams'][0]
        return (stream.get('codec_name') == 'h264' and stream.get('pix_fmt') == 'yuv420p'
                and stream.get('r_frame_rate') == '30/1')
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError):
        return False


@contextmanager
def nvenc_slot(device='0'):
    """Eight process-safe slots per GPU, also shared across workbench jobs."""
    import fcntl
    import os
    import time
    from pathlib import Path
    import hashlib
    gpu=str(device).removeprefix('cuda:')
    if gpu=='cuda':gpu='0'
    directory=Path('/tmp')/f'dataqc-nvenc-{os.getuid()}'/hashlib.sha256(gpu.encode()).hexdigest()[:16]
    directory.mkdir(parents=True,exist_ok=True)
    held=None
    try:
        while held is None:
            for i in range(8):
                candidate=open(directory/f'{i}.lock','a')
                try:fcntl.flock(candidate,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:candidate.close();continue
                held=candidate;break
            if held is None:time.sleep(.05)
        yield
    finally:
        if held is not None:
            fcntl.flock(held,fcntl.LOCK_UN);held.close()

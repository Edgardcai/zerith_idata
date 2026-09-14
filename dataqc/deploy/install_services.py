"""Render systemd user units for this checkout; --apply installs and starts them."""
import argparse
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def systemd_path(path):
    value = str(Path(path).expanduser().absolute())
    if any(c in value for c in '\n\r\x00'):
        raise ValueError('路径不能包含控制字符')
    return value.replace('\\', '\\x5c').replace(' ', '\\x20').replace('"', '\\x22').replace('%', '%%')


def render(output, python, runtime, port=8091, real_root='/data/zerith_data', sim_root='/data/sim_data', binary_path=None):
    if isinstance(port, bool) or not 1 <= int(port) <= 65535:
        raise ValueError('端口必须介于 1 和 65535')
    output = Path(output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    for name in ('dataqc-web', 'dataqc-worker'):
        text = (ROOT / 'deploy' / (name + '.service.in')).read_text()
        for key, value in dict(PROJECT_ROOT=ROOT, PYTHON=python, RUNTIME=runtime).items():
            text = text.replace('@' + key + '@', systemd_path(value))
        text = text.replace('@PORT@', str(port))
        text = text.replace('@REAL_ROOT@', systemd_path(real_root)).replace('@SIM_ROOT@', systemd_path(sim_root))
        # Explicit PATH also selects the deployment's ffmpeg/ffprobe binaries.
        paths = [Path(python).absolute().parent, *([Path(binary_path)] if binary_path else []), Path('/usr/local/bin'), Path('/usr/bin'), Path('/bin')]
        text = text.replace('@PATH@', ':'.join(systemd_path(p) for p in paths))
        path = output / (name + '.service')
        path.write_text(text)
        print(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', type=Path, default=ROOT / '.venv/bin/python')
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/service-preview')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--port', type=int, default=8091)
    parser.add_argument('--real-root', type=Path, default=Path('/data/zerith_data'))
    parser.add_argument('--sim-root', type=Path, default=Path('/data/sim_data'))
    parser.add_argument('--binary-path', type=Path, help='ffmpeg/ffprobe 所在目录')
    args = parser.parse_args()
    if not args.python.is_file():
        parser.error('请先创建 Python 3.11 环境并安装项目依赖')
    target = Path.home() / '.config/systemd/user' if args.apply else args.output
    render(target, args.python, args.runtime, args.port, args.real_root, args.sim_root, args.binary_path)
    if args.apply:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'dataqc-web', 'dataqc-worker'], check=True)

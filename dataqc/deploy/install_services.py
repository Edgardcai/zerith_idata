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


def render(output, python, runtime):
    output = Path(output).expanduser()
    output.mkdir(parents=True, exist_ok=True)
    for name in ('dataqc-web', 'dataqc-worker'):
        text = (ROOT / 'deploy' / (name + '.service.in')).read_text()
        for key, value in dict(PROJECT_ROOT=ROOT, PYTHON=python, RUNTIME=runtime).items():
            text = text.replace('@' + key + '@', systemd_path(value))
        path = output / (name + '.service')
        path.write_text(text)
        print(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', type=Path, default=ROOT / '.venv/bin/python')
    parser.add_argument('--runtime', type=Path, default=ROOT / 'runtime')
    parser.add_argument('--output', type=Path, default=ROOT / 'runtime/service-preview')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.python.is_file():
        parser.error('请先创建 Python 3.11 环境并安装项目依赖')
    target = Path.home() / '.config/systemd/user' if args.apply else args.output
    render(target, args.python, args.runtime)
    if args.apply:
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemctl', '--user', 'enable', '--now', 'dataqc-web', 'dataqc-worker'], check=True)

#!/usr/bin/env bash
set -euo pipefail

CONDA_BIN="${CONDA_BIN:-/home/robot/miniconda3/bin/conda}"
PROJECT_DIR="${PROJECT_DIR:-/home/robot/control}"

if ! "$CONDA_BIN" env list | awk '{print $1}' | grep -qx xiaoda-tts; then
  "$CONDA_BIN" create -y -n xiaoda-tts python=3.12 pip
fi
if ! "$CONDA_BIN" env list | awk '{print $1}' | grep -qx xiaoda-asr; then
  "$CONDA_BIN" create -y -n xiaoda-asr python=3.10 pip
fi

/home/robot/miniconda3/envs/xiaoda-tts/bin/pip install \
  -r "$PROJECT_DIR/chinese_speech/requirements-tts.txt"
/home/robot/miniconda3/envs/xiaoda-asr/bin/pip install \
  -r "$PROJECT_DIR/chinese_speech/requirements-asr.txt"

mkdir -p /home/robot/.config/systemd/user
install -m 0644 "$PROJECT_DIR/chinese_speech/systemd/zerith-chinese-asr.service" \
  /home/robot/.config/systemd/user/zerith-chinese-asr.service
install -m 0644 "$PROJECT_DIR/chinese_speech/systemd/zerith-chinese-tts.service" \
  /home/robot/.config/systemd/user/zerith-chinese-tts.service
systemctl --user daemon-reload

echo "Installed. Models download automatically on first service start."
echo "Start with: systemctl --user enable --now zerith-chinese-asr zerith-chinese-tts"

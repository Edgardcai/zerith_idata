#!/usr/bin/env bash
set -euo pipefail

VOICE_ROOT=/home/robot/control/voice_assistant
PROJECT_ROOT=/home/robot/control
VOICE_ENV=/home/robot/miniconda3/envs/xiaoda-voice
CONDA_BIN=/home/robot/miniconda3/bin/conda
MODEL_ROOT="$VOICE_ROOT/models"
KWS_NAME=sherpa-onnx-kws-zipformer-zh-en-3M-2025-12-20
KWS_ARCHIVE="$MODEL_ROOT/$KWS_NAME.tar.bz2"
KWS_URL="https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/$KWS_NAME.tar.bz2"
VAD_URL=https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx

if [[ ! -x "$CONDA_BIN" ]]; then
  echo "找不到 Conda：$CONDA_BIN" >&2
  exit 1
fi

if [[ ! -x "$VOICE_ENV/bin/python" ]]; then
  "$CONDA_BIN" create -y -n xiaoda-voice python=3.12 pip
fi

"$VOICE_ENV/bin/python" -m pip install --upgrade -r "$VOICE_ROOT/requirements.txt"
mkdir -p "$MODEL_ROOT"

if [[ ! -f "$MODEL_ROOT/$KWS_NAME/encoder-epoch-13-avg-2-chunk-8-left-64.int8.onnx" ]]; then
  curl -fL --retry 4 --connect-timeout 15 -o "$KWS_ARCHIVE" "$KWS_URL"
  tar -xjf "$KWS_ARCHIVE" -C "$MODEL_ROOT"
  unlink "$KWS_ARCHIVE"
fi

install -m 0644 "$VOICE_ROOT/keywords_xiaoda.txt" "$MODEL_ROOT/$KWS_NAME/keywords_xiaoda.txt"

if [[ ! -f "$MODEL_ROOT/silero_vad.onnx" ]]; then
  curl -fL --retry 4 --connect-timeout 15 -o "$MODEL_ROOT/silero_vad.onnx" "$VAD_URL"
fi

cd "$PROJECT_ROOT"
"$VOICE_ENV/bin/python" -m voice_assistant doctor

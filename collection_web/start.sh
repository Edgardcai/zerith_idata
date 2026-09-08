#!/usr/bin/env bash
set -euo pipefail
COLLECTION_WEB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export NO_PROXY="localhost,127.0.0.1,::1${NO_PROXY:+,$NO_PROXY}"
export no_proxy="$NO_PROXY"
if [[ ! -x "$COLLECTION_WEB_DIR/runtime/joint_observer" || "$COLLECTION_WEB_DIR/joint_observer.cpp" -nt "$COLLECTION_WEB_DIR/runtime/joint_observer" ]]; then
  mkdir -p "$COLLECTION_WEB_DIR/runtime"
  g++ -std=c++17 -O2 -Wall -Wextra \
    -I/home/robot/H1_SDK_1.3.9/robot_SDK/include/ZCM_Data \
    "$COLLECTION_WEB_DIR/joint_observer.cpp" -o "$COLLECTION_WEB_DIR/runtime/joint_observer" \
    $(pkg-config --cflags --libs zcm)
fi
exec "$COLLECTION_WEB_DIR/.venv/bin/python" "$COLLECTION_WEB_DIR/app.py" "$@"

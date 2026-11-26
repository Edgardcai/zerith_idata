#!/bin/sh
# Minimal factory launcher shape for the renderer test. Never executed by the test.
ROBOT_BIN_PATH="$(cd "$(dirname "$0")" && pwd)"
tmux new-session -d "cd $ROBOT_BIN_PATH && ./Motion_Control; exec sh"
tmux new-window -d "cd $ROBOT_BIN_PATH && ./SDKService; exec sh"
tmux new-window -d "cd $ROBOT_BIN_PATH && ./teleop; exec sh"
tmux new-window -d "cd $ROBOT_BIN_PATH && ./server; exec sh"
tmux new-window -d "cd $ROBOT_BIN_PATH && ./wifi_config; exec sh"
tmux new-window -d "cd $ROBOT_BIN_PATH && ./robot_state; exec sh"

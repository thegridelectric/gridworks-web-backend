#!/bin/bash

# Script to start the REST API in a tmux session
SESSION_NAME="api"
RUN_CMD='cd ~/gridworks-web-backend && unset VIRTUAL_ENV && uv run python -m api'

# If tmux is not installed, run without tmux
if ! command -v tmux &>/dev/null; then
    echo "tmux is not installed. Running without tmux."
    eval "$RUN_CMD"
    exit 0
fi

# Check if tmux session already exists
if tmux has-session -t "$SESSION_NAME" 2>/dev/null; then
    echo "Session '$SESSION_NAME' already exists. Attaching..."
    tmux attach-session -t "$SESSION_NAME"
else
    echo "Creating new tmux session '$SESSION_NAME'..."

    tmux new-session -d -s "$SESSION_NAME" -c "$(pwd)"
    sleep 0.5

    tmux send-keys -t "$SESSION_NAME" "$RUN_CMD" C-m

    tmux attach-session -t "$SESSION_NAME"
fi

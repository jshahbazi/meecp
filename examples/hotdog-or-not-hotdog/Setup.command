#!/bin/zsh
set -eu
APP_DIRECTORY=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIRECTORY"
if ! command -v uv >/dev/null 2>&1; then
  print 'Setup needs uv. Install it from https://docs.astral.sh/uv/getting-started/installation/'
  read -r '?Press Return to close. '
  exit 1
fi
uv sync --locked --group build
uv run --locked --group build python -P "$APP_DIRECTORY/prepare_model.py"
print 'Setup finished. Open Launch.command to use the app.'

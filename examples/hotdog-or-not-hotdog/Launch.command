#!/bin/zsh
set -eu
APP_DIRECTORY=$(cd -- "$(dirname -- "$0")" && pwd)
cd "$APP_DIRECTORY"
if [[ -x "$APP_DIRECTORY/runtime/bin/python3" ]]; then
  exec "$APP_DIRECTORY/runtime/bin/python3" -P "$APP_DIRECTORY/run.py" "$@"
elif [[ -x "$APP_DIRECTORY/.venv/bin/python" ]]; then
  exec "$APP_DIRECTORY/.venv/bin/python" -P "$APP_DIRECTORY/run.py" "$@"
else
  print 'Run Setup.command in this folder first, then open Launch.command again.'
  read -r '?Press Return to close. '
fi

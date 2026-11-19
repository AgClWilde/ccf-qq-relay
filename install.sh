#!/bin/sh
set -eu
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ -f "$script_dir/bootstrap.py" ]; then
  exec python3 "$script_dir/bootstrap.py" install "$@"
fi
exec python3 "$script_dir/../bootstrap.py" install "$@"

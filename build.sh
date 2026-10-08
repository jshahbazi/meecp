#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s [OUTPUT.app]\n' "${0##*/}"
    printf 'Build the current checkout without running tests.\n'
    printf 'Defaults to a fresh app bundle under dist/. Relative paths use the repository root.\n'
}

if [[ $# -gt 1 ]]; then
    usage >&2
    exit 2
fi
if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$repo_root"

if ! command -v uv >/dev/null 2>&1; then
    printf 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/\n' >&2
    exit 1
fi

output=${1:-"dist/meecp-$(date +%Y%m%d-%H%M%S)-$$.app"}
if [[ $output != /* ]]; then
    output="$repo_root/$output"
fi

uv python install 3.12.13
uv run --locked --python 3.12.13 python scripts/build_meecp_app.py --output "$output"

printf '\nBuilt app: %s\n' "$output"
printf 'Launch: open %q\n' "$output"

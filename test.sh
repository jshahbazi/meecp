#!/usr/bin/env bash
set -euo pipefail

usage() {
    printf 'Usage: %s\n' "${0##*/}"
    printf 'Run the Python suites, Swift tests, and native app self-tests.\n'
    printf 'Stops at the first failure. Installer lifecycle tests remain opt-in.\n'
}

if [[ $# -eq 1 && ( $1 == --help || $1 == -h ) ]]; then
    usage
    exit 0
fi
if [[ $# -ne 0 ]]; then
    usage >&2
    exit 2
fi

repo_root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
cd "$repo_root"

if ! command -v uv >/dev/null 2>&1; then
    printf 'uv is required. Install it from https://docs.astral.sh/uv/getting-started/installation/\n' >&2
    exit 1
fi

printf 'Running host Python tests...\n'
uv run --locked --python 3.12.13 python -m pytest -q

printf '\nRunning hotdog demo Python tests...\n'
(
    cd examples/hotdog-or-not-hotdog
    uv run --locked --python 3.12.13 --group build python -m pytest -q
)

printf '\nRunning Swift tests...\n'
swift test

printf '\nRunning native app self-tests...\n'
.build/debug/McpmeMenuBar --native-self-test
.build/debug/McpmeMenuBar --dashboard-ui-self-test
.build/debug/McpmeMenuBar --management-ui-self-test
.build/debug/McpmeMenuBar --pi-setup-ui-self-test

printf '\nAll standard tests passed.\n'

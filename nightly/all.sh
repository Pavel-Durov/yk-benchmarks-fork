#!/usr/bin/env bash
# Full nightly workflow: sync → build → bench. Stops on first phase failure.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/nightly.sh" sync  "$@"
"$HERE/nightly.sh" build "$@"
"$HERE/nightly.sh" bench "$@"

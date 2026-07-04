#!/usr/bin/env bash
# Cron entry point: sync + build + bench against nightly/config.json.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/nightly.sh" sync;  sync_rc=$?
"$HERE/nightly.sh" build; build_rc=$?
"$HERE/nightly.sh" bench; bench_rc=$?
exit $(( sync_rc | build_rc | bench_rc ))

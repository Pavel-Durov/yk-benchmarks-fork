#!/usr/bin/env bash
# Config-driven runner for haste benchmarks on a remote bench host.
# Subcommands: sync | run
# Usage: nightly.sh <sync|run> [--config path]
# ponytail: single-file bash + jq; split into lib.sh only if it grows past ~200 lines.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/config.json"

# Reuse one SSH connection across all ssh/rsync calls in this run.
SSH_CTL="/tmp/nightly-ssh-%C"
SSH_OPTS=(-o ControlMaster=auto -o "ControlPath=$SSH_CTL" -o ControlPersist=60s)

log()  { printf '[nightly %s] %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die()  { printf 'nightly: %s\n' "$*" >&2; exit 1; }

parse_args() {
    CMD="${1:-}"
    [ -n "$CMD" ] || die "usage: nightly.sh <sync|build|bench> [--config path]"
    shift
    while [ $# -gt 0 ]; do
        case "$1" in
            --config) CONFIG="$2"; shift 2 ;;
            *) die "unknown arg: $1" ;;
        esac
    done
    [ -f "$CONFIG" ] || die "config not found: $CONFIG"
    command -v jq >/dev/null   || die "jq is required"
    command -v git >/dev/null  || die "git is required"
    command -v rsync >/dev/null || die "rsync is required"
    command -v ssh >/dev/null  || die "ssh is required"
}

# Required keys per target — fail fast with the offending target name.
validate_config() {
    local n; n=$(jq '.targets | length' "$CONFIG")
    [ "$n" -gt 0 ] || die "config has no targets"
    jq -r '.remote.host // empty' "$CONFIG" >/dev/null 2>&1 || true
    [ -n "$(jq -r '.remote.host // empty' "$CONFIG")" ] || die "remote.host missing"
    [ -n "$(jq -r '.remote.root // empty' "$CONFIG")" ] || die "remote.root missing"
    local i name tmpl
    for i in $(seq 0 $((n-1))); do
        for k in name repo build template; do
            if [ -z "$(jq -r ".targets[$i].$k // empty" "$CONFIG")" ]; then
                name=$(jq -r ".targets[$i].name // \"index $i\"" "$CONFIG")
                die "target '$name' missing key: $k"
            fi
        done
        tmpl=$(jq -r ".targets[$i].template" "$CONFIG")
        [ -f "$HERE/templates/haste_$tmpl.toml" ] \
            || die "target '$(jq -r ".targets[$i].name" "$CONFIG")' unknown template: $tmpl"
    done
}

remote_dest() {
    local user host
    user=$(jq -r '.remote.user // empty' "$CONFIG")
    host=$(jq -r '.remote.host' "$CONFIG")
    if [ -n "$user" ]; then printf '%s@%s' "$user" "$host"
    else printf '%s' "$host"; fi
}

clone_one() {
    local name repo ref ws
    name=$(jq -r ".targets[$1].name" "$CONFIG")
    repo=$(jq -r ".targets[$1].repo" "$CONFIG")
    ref=$( jq -r ".targets[$1].ref // \"main\"" "$CONFIG")
    ws="$HERE/workspace/$name"
    if [ -d "$ws/.git" ]; then
        log "update $name @ $ref (fetching)"
        git -C "$ws" fetch --all --tags --prune || return 1
        log "update $name (checkout + pull)"
        git -C "$ws" checkout "$ref" || return 1
        git -C "$ws" pull --ff-only origin "$ref" 2>/dev/null || true
    else
        log "clone $name @ $ref (fresh, shallow)"
        git clone --depth 1 --branch "$ref" --shallow-submodules "$repo" "$ws" || return 1
    fi
    log "submodules $name"
    git -C "$ws" submodule update --init --recursive --depth 1 || return 1
    log "clone/update $name done ($(du -sh "$ws" 2>/dev/null | cut -f1))"
}

push_one() {
    local name remote_root dest ws
    name=$(jq -r ".targets[$1].name" "$CONFIG")
    ws="$HERE/workspace/$name"
    if [ ! -d "$ws/.git" ]; then
        log "skip push $name (no local clone)"
        return 1
    fi
    remote_root=$(jq -r '.remote.root' "$CONFIG")
    dest="$(remote_dest):$remote_root/$name/"
    log "push $name → $dest ($(du -sh --exclude=.git "$ws" 2>/dev/null | cut -f1))"
    rsync -az --delete --mkpath --exclude '.git/' --info=stats1,progress2 \
        -e "ssh ${SSH_OPTS[*]}" "$ws"/ "$dest"
}

build_one() {
    local name build remote_root dest_dir cmd
    name=$( jq -r ".targets[$1].name"  "$CONFIG")
    build=$(jq -r ".targets[$1].build" "$CONFIG")
    remote_root=$(jq -r '.remote.root' "$CONFIG")
    dest_dir="$BUILD_DIR/$name"
    mkdir -p "$dest_dir"
    log "build $name"
    cmd="cd $(printf '%q' "$remote_root/$name") && ($build)"
    printf '%s\n' "$cmd" > "$dest_dir/cmd"
    ssh "${SSH_OPTS[@]}" "$(remote_dest)" "bash -lc $(printf '%q' "$cmd")" \
        > "$dest_dir/stdout.log" 2> "$dest_dir/stderr.log"
}

bench_one() {
    local name tmpl remote_root dest_dir rendered exports cmd
    name=$( jq -r ".targets[$1].name"     "$CONFIG")
    tmpl=$( jq -r ".targets[$1].template" "$CONFIG")
    remote_root=$(jq -r '.remote.root' "$CONFIG")
    dest_dir="$RESULTS_DIR/$name"
    mkdir -p "$dest_dir"
    # Build `export K=V` list for envsubst from target.env.
    exports=$(jq -r ".targets[$1].env // {} | to_entries
                     | map(\"export \" + .key + \"=\" + (.value|@sh)) | .[]" "$CONFIG")
    rendered="nightly/rendered/haste_${name}.toml"
    log "bench $name"
    # shellcheck disable=SC2016
    cmd=$(cat <<EOF
cd $(printf '%q' "$remote_root/yk-benchmarks-fork") &&
mkdir -p nightly/rendered &&
$exports
envsubst < $(printf '%q' "nightly/templates/haste_$tmpl.toml") > $(printf '%q' "$rendered") &&
haste bench -f $(printf '%q' "$rendered") -c $(printf '%q' "nightly_$name") --order declaration
EOF
)
    printf '%s\n' "$cmd" > "$dest_dir/cmd"
    ssh "${SSH_OPTS[@]}" "$(remote_dest)" "bash -lc $(printf '%q' "$cmd")" \
        > "$dest_dir/stdout.log" 2> "$dest_dir/stderr.log"
}

sync_self() {
    # Push this repo (yk-benchmarks-fork) to the remote so haste_toml paths resolve.
    local repo_root remote_root dest
    repo_root="$(cd "$HERE/.." && pwd)"
    remote_root=$(jq -r '.remote.root' "$CONFIG")
    dest="$(remote_dest):$remote_root/yk-benchmarks-fork/"
    log "sync yk-benchmarks-fork (self) → $dest"
    rsync -az --delete --mkpath \
        --exclude 'nightly/workspace/' \
        --exclude 'nightly/results/' \
        --exclude '.git/' \
        --info=stats1,progress2 \
        -e "ssh ${SSH_OPTS[*]}" \
        "$repo_root"/ "$dest"
}

cmd_sync() {
    local n i rc=0
    n=$(jq '.targets | length' "$CONFIG")
    log "sync: $n targets, remote $(remote_dest):$(jq -r '.remote.root' "$CONFIG")"
    log "phase 1/2: clone/update $n repos locally"
    for i in $(seq 0 $((n-1))); do
        if ! clone_one "$i"; then
            die "clone FAILED for target $(jq -r ".targets[$i].name" "$CONFIG")"
        fi
    done
    log "phase 2/2: push repo + $n targets to remote"
    if ! sync_self; then log "push FAILED for yk-benchmarks-fork"; rc=1; fi
    for i in $(seq 0 $((n-1))); do
        if ! push_one "$i"; then
            log "push FAILED for target index $i"
            rc=1
        fi
    done
    log "sync: done (rc=$rc)"
    return $rc
}

cmd_build() {
    BUILD_DIR="$HERE/results/$(date -u +%Y%m%dT%H%M%SZ)-build"
    mkdir -p "$BUILD_DIR"
    log "build logs: $BUILD_DIR"
    local n rc=0
    n=$(jq '.targets | length' "$CONFIG")
    for i in $(seq 0 $((n-1))); do
        if ! build_one "$i"; then
            log "build FAILED for target $(jq -r ".targets[$i].name" "$CONFIG")"
            rc=1
        fi
    done
    return $rc
}

cmd_bench() {
    RESULTS_DIR="$HERE/results/$(date -u +%Y%m%dT%H%M%SZ)"
    mkdir -p "$RESULTS_DIR"
    log "results dir: $RESULTS_DIR"
    local n rc=0
    n=$(jq '.targets | length' "$CONFIG")
    for i in $(seq 0 $((n-1))); do
        if ! bench_one "$i"; then
            log "bench FAILED for target $(jq -r ".targets[$i].name" "$CONFIG")"
            rc=1
        fi
    done
    return $rc
}

main() {
    parse_args "$@"
    validate_config
    case "$CMD" in
        sync)  cmd_sync ;;
        build) cmd_build ;;
        bench) cmd_bench ;;
        *) die "unknown subcommand: $CMD" ;;
    esac
}

main "$@"

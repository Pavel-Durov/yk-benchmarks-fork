# nightly

Config-driven runner for haste benchmarks on a remote bench host (`bencher16`).
Everything lives here so upstream merges never conflict with this workflow.

## Prerequisites

- Local: `bash`, `jq`, `python3` (≥3.11 for stdlib `tomllib`), `git`, `rsync`, `ssh`
- ssh key auth already working against the remote host
- Remote: whatever each target's build needs, plus `haste` on `$PATH`

## Usage

    ./nightly.sh sync   [--config path]   # clone/update + rsync to remote
    ./nightly.sh run    [--config path]   # ssh-execute build+run, capture logs
    ./run-nightly.sh                      # sync then run, exit combined status

Default config path: `nightly/config.toml`.

## Config schema

See `config.example.json`. Each target needs: `name`, `repo`, `build`, `run`,
`haste_toml`. `ref` defaults to `main`. `haste_toml` is exported as
`$HASTE_TOML` inside the remote shell, so the `run` command can reference it.

`remote.host` and `remote.root` are required. `remote.user` is optional.

## Where results land

`nightly/results/<UTC-timestamp>/<target-name>/{stdout.log,stderr.log}`.
Never overwritten. Prune by mtime if disk fills up.

## Cron example

    5 2 * * *  cd /home/pd/yk-benchmarks-fork && ./nightly/run-nightly.sh >> /var/log/nightly-haste.log 2>&1

## Adding a target

Append an object to `config.toml`'s `targets` array. Sync + run picks it up on
the next invocation. Sequential, keep-going-on-failure: one bad target does
not abort the rest; the final exit is non-zero if any failed.

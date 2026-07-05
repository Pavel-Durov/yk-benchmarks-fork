#!/usr/bin/env python3
"""Config-driven runner for haste benchmarks on a remote bench host.

Subcommands: sync | build | bench | run
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
SSH_CTL = "/tmp/nightly-ssh-%C"
SSH_OPTS = ["-o", "ControlMaster=auto", "-o", f"ControlPath={SSH_CTL}",
            "-o", "ControlPersist=60s"]


# --- pure helpers (unit-testable) --------------------------------------------

def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def validate_config(cfg: dict, templates_dir: Path) -> None:
    """Raise ValueError on any missing/invalid field. No I/O beyond template check."""
    remote = cfg.get("remote") or {}
    if not remote.get("host"):
        raise ValueError("remote.host missing")
    if not remote.get("root"):
        raise ValueError("remote.root missing")
    targets = cfg.get("targets") or []
    if not targets:
        raise ValueError("config has no targets")
    for i, t in enumerate(targets):
        name = t.get("name") or f"index {i}"
        for k in ("name", "repo", "build", "template"):
            if not t.get(k):
                raise ValueError(f"target '{name}' missing key: {k}")
        tmpl = templates_dir / f"haste_{t['template']}.toml"
        if not tmpl.is_file():
            raise ValueError(f"target '{name}' unknown template: {t['template']}")


def remote_dest(remote: dict) -> str:
    user, host = remote.get("user"), remote["host"]
    return f"{user}@{host}" if user else host


def env_exports(env: dict) -> str:
    """`export K=V` lines, values shell-quoted. Empty string if env is empty."""
    return "\n".join(f"export {k}={shlex.quote(str(v))}" for k, v in (env or {}).items())


def build_cmd(remote_root: str, name: str, build: str) -> str:
    return f"cd {shlex.quote(f'{remote_root}/{name}')} && ({build})"


def bench_cmd(remote_root: str, name: str, tmpl: str, env: dict) -> str:
    rendered = f"nightly/rendered/haste_{name}.toml"
    exports = env_exports(env)
    template = f"nightly/templates/haste_{tmpl}.toml"
    return (
        f"cd {shlex.quote(f'{remote_root}/yk-benchmarks-fork')} &&\n"
        f"mkdir -p nightly/rendered &&\n"
        f"{exports}\n"
        f"envsubst < {shlex.quote(template)} > {shlex.quote(rendered)} &&\n"
        f"haste bench -f {shlex.quote(rendered)} "
        f"-c {shlex.quote(f'nightly_{name}')} --order declaration"
    )


# --- I/O wrappers ------------------------------------------------------------

def log(msg: str) -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%H:%M:%S")
    print(f"[nightly {now}] {msg}", file=sys.stderr, flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def ssh_run(dest: str, remote_cmd: str, stdout: Path | None = None,
            stderr: Path | None = None, tee: bool = False) -> int:
    args = ["ssh", *SSH_OPTS, dest, f"bash -lc {shlex.quote(remote_cmd)}"]
    if stdout is None:
        return run(args).returncode
    if tee:
        # Emulate bash `> >(tee X) 2> >(tee Y >&2)`: write to file AND terminal.
        with stdout.open("wb") as so, stderr.open("wb") as se:
            p = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            assert p.stdout and p.stderr
            # Interleave crudely: read line by line, prefer stdout.
            import selectors
            sel = selectors.DefaultSelector()
            sel.register(p.stdout, selectors.EVENT_READ, (so, sys.stdout.buffer))
            sel.register(p.stderr, selectors.EVENT_READ, (se, sys.stderr.buffer))
            while sel.get_map():
                for key, _ in sel.select():
                    line = key.fileobj.readline()
                    if not line:
                        sel.unregister(key.fileobj)
                        continue
                    dst_file, dst_term = key.data
                    dst_file.write(line); dst_file.flush()
                    dst_term.write(line); dst_term.flush()
            return p.wait()
    with stdout.open("wb") as so, stderr.open("wb") as se:
        return run(args, stdout=so, stderr=se).returncode


# --- per-target operations ---------------------------------------------------

def clone_one(target: dict, ws: Path) -> None:
    name, repo, ref = target["name"], target["repo"], target.get("ref", "main")
    if (ws / ".git").is_dir():
        log(f"update {name} @ {ref} (fetching)")
        subprocess.check_call(["git", "-C", str(ws), "fetch", "--all", "--tags", "--prune"])
        log(f"update {name} (checkout + pull)")
        subprocess.check_call(["git", "-C", str(ws), "checkout", ref])
        subprocess.call(["git", "-C", str(ws), "pull", "--ff-only", "origin", ref],
                        stderr=subprocess.DEVNULL)
    else:
        log(f"clone {name} @ {ref} (fresh, shallow)")
        subprocess.check_call(["git", "clone", "--depth", "1", "--branch", ref,
                               "--shallow-submodules", repo, str(ws)])
    log(f"submodules {name}")
    subprocess.check_call(["git", "-C", str(ws), "submodule", "update",
                           "--init", "--recursive", "--depth", "1"])


def push_one(target: dict, ws: Path, dest_host: str, remote_root: str) -> None:
    name = target["name"]
    if not (ws / ".git").is_dir():
        log(f"skip push {name} (no local clone)")
        raise RuntimeError(f"no local clone for {name}")
    dest = f"{dest_host}:{remote_root}/{name}/"
    log(f"push {name} → {dest}")
    subprocess.check_call([
        "rsync", "-az", "--delete", "--mkpath", "--info=stats1,progress2",
        "-e", "ssh " + " ".join(SSH_OPTS), f"{ws}/", dest,
    ])


def sync_self(dest_host: str, remote_root: str) -> None:
    repo_root = HERE.parent
    dest = f"{dest_host}:{remote_root}/yk-benchmarks-fork/"
    log(f"sync yk-benchmarks-fork (self) → {dest}")
    subprocess.check_call([
        "rsync", "-az", "--delete", "--mkpath",
        "--exclude", "nightly/workspace/",
        "--exclude", "nightly/results/",
        "--exclude", ".git/",
        "--info=stats1,progress2",
        "-e", "ssh " + " ".join(SSH_OPTS),
        f"{repo_root}/", dest,
    ])


def build_one(target: dict, dest_host: str, remote_root: str, build_dir: Path) -> int:
    name = target["name"]
    out = build_dir / name
    out.mkdir(parents=True, exist_ok=True)
    cmd = build_cmd(remote_root, name, target["build"])
    (out / "cmd").write_text(cmd + "\n")
    log(f"build {name}")
    return ssh_run(dest_host, cmd, out / "stdout.log", out / "stderr.log")


def bench_one(target: dict, dest_host: str, remote_root: str, results_dir: Path) -> int:
    name = target["name"]
    out = results_dir / name
    out.mkdir(parents=True, exist_ok=True)
    cmd = bench_cmd(remote_root, name, target["template"], target.get("env", {}))
    (out / "cmd").write_text(cmd + "\n")
    log(f"bench {name}")
    return ssh_run(dest_host, cmd, out / "stdout.log", out / "stderr.log", tee=True)


# --- subcommands -------------------------------------------------------------

def _parallel(fn, items, max_workers=None) -> list[Exception | None]:
    """Run fn(item) in threads; return per-item exception or None."""
    results: list[Exception | None] = [None] * len(items)
    with cf.ThreadPoolExecutor(max_workers=max_workers or len(items) or 1) as ex:
        futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
        for fut in cf.as_completed(futs):
            i = futs[fut]
            try:
                fut.result()
            except Exception as e:  # noqa: BLE001
                results[i] = e
    return results


def cmd_sync(cfg: dict) -> int:
    remote = cfg["remote"]; targets = cfg["targets"]
    dest_host = remote_dest(remote); root = remote["root"]
    n = len(targets)
    log(f"sync: {n} targets, remote {dest_host}:{root}")

    log(f"phase 1/2: clone/update {n} repos locally (parallel)")
    errs = _parallel(lambda t: clone_one(t, HERE / "workspace" / t["name"]), targets)
    for t, e in zip(targets, errs):
        if e:
            log(f"clone FAILED for target {t['name']}: {e}")
            return 1

    log(f"phase 2/2: push repo + {n} targets to remote (parallel)")
    tasks = [("__self__", lambda: sync_self(dest_host, root))]
    for t in targets:
        ws = HERE / "workspace" / t["name"]
        tasks.append((t["name"], lambda t=t, ws=ws: push_one(t, ws, dest_host, root)))
    errs = _parallel(lambda pair: pair[1](), tasks)
    rc = 0
    for (name, _), e in zip(tasks, errs):
        if e:
            log(f"push FAILED for {name}: {e}"); rc = 1
    log(f"sync: done (rc={rc})")
    return rc


def _timestamped(subdir: str) -> Path:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = HERE / "results" / f"{ts}{subdir}"
    p.mkdir(parents=True, exist_ok=True)
    return p


def cmd_build(cfg: dict) -> int:
    remote = cfg["remote"]; targets = cfg["targets"]
    build_dir = _timestamped("-build")
    log(f"build logs: {build_dir}")
    dest_host, root = remote_dest(remote), remote["root"]
    codes = _parallel(lambda t: build_one(t, dest_host, root, build_dir), targets)
    rc = 0
    for t, e in zip(targets, codes):
        if e:
            log(f"build FAILED for target {t['name']}: {e}"); rc = 1
    return rc


def cmd_bench(cfg: dict) -> int:
    remote = cfg["remote"]; targets = cfg["targets"]
    results_dir = _timestamped("")
    log(f"results dir: {results_dir}")
    dest_host, root = remote_dest(remote), remote["root"]
    rc = 0
    # bench is sequential in the bash version — keep it that way.
    for t in targets:
        try:
            if bench_one(t, dest_host, root, results_dir) != 0:
                log(f"bench FAILED for target {t['name']}"); rc = 1
        except Exception as e:  # noqa: BLE001
            log(f"bench FAILED for target {t['name']}: {e}"); rc = 1
    return rc


# --- entrypoint --------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nightly.py")
    p.add_argument("command", choices=["sync", "build", "bench", "run"])
    p.add_argument("--config", default=str(HERE / "config.toml"))
    args = p.parse_args(argv)

    for tool in ("git", "rsync", "ssh"):
        if not shutil.which(tool):
            log(f"error: {tool} is required"); return 2

    cfg_path = Path(args.config)
    if not cfg_path.is_file():
        log(f"error: config not found: {cfg_path}"); return 2
    cfg = load_config(cfg_path)
    try:
        validate_config(cfg, HERE / "templates")
    except ValueError as e:
        log(f"error: {e}"); return 2

    match args.command:
        case "sync":  return cmd_sync(cfg)
        case "build": return cmd_build(cfg)
        case "bench": return cmd_bench(cfg)
        case "run":   return cmd_build(cfg) or cmd_bench(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())

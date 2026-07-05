"""Assert-based tests for the pure helpers in nightly.py. Run: `python3 test_nightly.py`."""
from __future__ import annotations

import tempfile
from pathlib import Path

import nightly as n


def _mktemplates(tmp: Path, *names: str) -> Path:
    d = tmp / "templates"; d.mkdir()
    for name in names:
        (d / f"haste_{name}.toml").write_text("")
    return d


def test_remote_dest():
    assert n.remote_dest({"host": "h"}) == "h"
    assert n.remote_dest({"host": "h", "user": "u"}) == "u@h"
    assert n.remote_dest({"host": "h", "user": ""}) == "h"


def test_env_exports_empty():
    assert n.env_exports({}) == ""
    assert n.env_exports(None) == ""


def test_env_exports_quotes():
    out = n.env_exports({"A": "1", "B": "a b"})
    assert "export A=1" in out
    assert "export B='a b'" in out


def test_build_cmd_quotes_path():
    c = n.build_cmd("/r", "t 1", "make")
    assert c == "cd '/r/t 1' && (make)"


def test_bench_cmd_includes_pieces():
    c = n.bench_cmd("/r", "t", "som", {"EXE": "/x"})
    assert "cd /r/yk-benchmarks-fork" in c
    assert "export EXE=/x" in c
    assert "envsubst < nightly/templates/haste_som.toml" in c
    assert "haste bench -f nightly/rendered/haste_t.toml -c nightly_t --order declaration" in c


def test_validate_ok():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        tdir = _mktemplates(tmp, "som")
        cfg = {"remote": {"host": "h", "root": "/r"},
               "targets": [{"name": "n", "repo": "r", "build": "b", "template": "som"}]}
        n.validate_config(cfg, tdir)  # no raise


def test_validate_missing_host():
    try: n.validate_config({"remote": {"root": "/r"}, "targets": [{}]}, Path("/x"))
    except ValueError as e: assert "host" in str(e); return
    raise AssertionError("expected ValueError")


def test_validate_no_targets():
    try: n.validate_config({"remote": {"host": "h", "root": "/r"}, "targets": []}, Path("/x"))
    except ValueError as e: assert "no targets" in str(e); return
    raise AssertionError("expected ValueError")


def test_validate_missing_template_file():
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td); tdir = _mktemplates(tmp)  # empty templates dir
        cfg = {"remote": {"host": "h", "root": "/r"},
               "targets": [{"name": "n", "repo": "r", "build": "b", "template": "missing"}]}
        try: n.validate_config(cfg, tdir)
        except ValueError as e: assert "unknown template" in str(e); return
        raise AssertionError("expected ValueError")


def test_load_config_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "c.toml"
        p.write_text('[remote]\nhost = "h"\nroot = "/r"\n\n[[targets]]\nname = "t"\n')
        cfg = n.load_config(p)
        assert cfg["remote"]["host"] == "h"
        assert cfg["targets"][0]["name"] == "t"


if __name__ == "__main__":
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t(); print(f"ok   {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1; print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(1 if failed else 0)

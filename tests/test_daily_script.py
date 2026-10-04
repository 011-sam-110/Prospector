"""Tests for ``deploy/prospector-daily.sh`` with a stub ``prospector`` binary.

The stub prints canned summary lines, so no network and no model are used. The
daily line must count a profile as failed when it fetched nothing or when most
of its requests were refused, and the script must exit non-zero when every
profile failed.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "deploy" / "prospector-daily.sh"
BASH = shutil.which("bash")
if BASH is None or "system32" in BASH.lower():  # no bash, or WSL's launcher
    pytest.skip("needs a POSIX bash", allow_module_level=True)

STUB = r"""#!/usr/bin/env bash
cmd="$1"; shift
case "$cmd" in
  sweep)
    profile="$1"
    line="$(grep "^$profile " "$STUB_SWEEPS" | cut -d" " -f2-)"
    echo "Sweeping $profile"
    echo "$line"
    ;;
  embed) echo "embedded=7 unchanged=0 skipped_empty=0 skipped_gone=0 vectors=7 model=x" ;;
  prune) echo "pruned=0 expired=0 remaining=7" ;;
  semantic-search) echo "searched=3" ;;
esac
exit 0
"""


def _run(tmp_path: Path, sweeps: dict[str, str]):
    home = tmp_path / "app"
    bin_dir = home / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "prospector"
    stub.write_text(STUB, encoding="utf-8", newline="\n")
    stub.chmod(0o755)
    table = tmp_path / "sweeps.txt"
    table.write_text("".join(f"{p} {line}\n" for p, line in sweeps.items()), encoding="utf-8", newline="\n")
    env = dict(os.environ)
    env.update(
        PROSPECTOR_HOME=home.as_posix(),
        PROSPECTOR_DATA=(tmp_path / "data").as_posix(),
        PROSPECTOR_SWEEP_PROFILES=" ".join(sweeps),
        STUB_SWEEPS=table.as_posix(),
    )
    proc = subprocess.run(
        [BASH, SCRIPT.as_posix()], capture_output=True, text=True, env=env, timeout=60
    )
    daily = [l for l in proc.stdout.splitlines() if l.startswith("daily:")]
    assert daily, proc.stdout + proc.stderr
    fields = dict(re.findall(r"(\w+)=(\d+)", daily[-1]))
    return proc.returncode, {k: int(v) for k, v in fields.items()}


GOOD = "fetched=1402 posts=1402 comments=0 transport=rss rss_requests=29 requests_ok=28 requests_403=0 requests_429=1 requests_other=0"
EMPTY = "fetched=0 posts=0 comments=0 transport=rss rss_requests=28 requests_ok=0 requests_403=14 requests_429=14 requests_other=0"
REFUSED = "fetched=40 posts=40 comments=0 transport=rss rss_requests=28 requests_ok=4 requests_403=12 requests_429=12 requests_other=0"


def test_a_profile_that_fetched_nothing_counts_as_failed(tmp_path):
    code, fields = _run(tmp_path, {"good": GOOD, "empty": EMPTY})
    assert fields["profiles_ok"] == 1
    assert fields["profiles_failed"] == 1
    assert fields["fetched"] == 1402
    assert code == 0  # one profile still worked


def test_a_profile_with_mostly_refused_requests_counts_as_failed(tmp_path):
    code, fields = _run(tmp_path, {"good": GOOD, "refused": REFUSED})
    assert fields["profiles_failed"] == 1
    assert fields["fetched"] == 1442
    assert code == 0


def test_the_script_exits_non_zero_when_every_profile_fails(tmp_path):
    code, fields = _run(tmp_path, {"empty": EMPTY, "refused": REFUSED})
    assert fields["profiles_ok"] == 0
    assert fields["profiles_failed"] == 2
    assert code != 0


def test_all_profiles_ok(tmp_path):
    code, fields = _run(tmp_path, {"a": GOOD, "b": GOOD})
    assert fields["profiles_failed"] == 0
    assert fields["embedded"] == 7 and fields["searched"] == 3
    assert code == 0

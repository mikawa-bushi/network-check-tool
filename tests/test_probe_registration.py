"""Every command runs the built-in checks, not only ``check``.

Each case starts netdiag in a fresh interpreter: the probe registry is
process-wide, and other tests have already filled it in this one.
"""

import os
import sqlite3
import subprocess
import sys
from contextlib import closing


def _netdiag(home, *args):
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home), NETDIAG_LANG="en")
    return subprocess.run(
        [sys.executable, "-m", "netdiag", *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )


def test_explain_lists_and_describes_the_built_in_checks(tmp_path):
    listing = _netdiag(tmp_path, "explain")
    assert listing.returncode == 0, listing.stderr
    assert "interface" in listing.stdout and "gateway" in listing.stdout
    dns = _netdiag(tmp_path, "explain", "dns")
    assert dns.returncode == 0, dns.stderr
    assert dns.stdout.startswith("# dns")


def test_watch_runs_its_checks_and_records_them(tmp_path):
    res = _netdiag(
        tmp_path, "watch", "--once", "--no-internet", "--light-probes", "interface"
    )
    assert res.returncode == 0, res.stderr
    with closing(sqlite3.connect(tmp_path / ".netdiag" / "history.db")) as db:
        assert db.execute("select count(*) from samples").fetchone()[0] > 0

"""Every command runs the built-in checks, not only ``check``.

Each case starts netdiag in a fresh interpreter: the probe registry is
process-wide, and other tests have already filled it in this one.
"""

import os
import subprocess
import sys


def _netdiag(home, *args):
    env = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        NETDIAG_LANG="en",
        PYTHONIOENCODING="utf-8",
    )
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

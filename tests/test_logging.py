"""netdiag logs nothing until the CLI opts in with -v or --log-file."""

import logging

import pytest


@pytest.fixture(autouse=True)
def _restore_netdiag_logger():
    """Isolate tests that mutate the process-global ``netdiag`` logger."""
    lg = logging.getLogger("netdiag")
    handlers = lg.handlers[:]
    level = lg.level
    propagate = lg.propagate
    try:
        yield
    finally:
        # Close any handler the test added before restoring, so FileHandlers
        # don't leak their file descriptor past the test.
        for h in lg.handlers[:]:
            if h not in handlers:
                h.close()
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate


def test_library_is_quiet_by_default():
    lg = logging.getLogger("netdiag")
    assert any(
        isinstance(h, logging.NullHandler) for h in lg.handlers
    ), "netdiag logger should carry a NullHandler so it is silent by default"
    assert not any(
        isinstance(h, (logging.StreamHandler, logging.FileHandler))
        and not isinstance(h, logging.NullHandler)
        for h in lg.handlers
    ), "library must not attach a real handler on its own"


def test_setup_logging_verbose_attaches_stream_handler():
    from netdiag.cli import _setup_logging

    _setup_logging(verbose=True)
    lg = logging.getLogger("netdiag")
    assert lg.level == logging.DEBUG
    assert any(
        isinstance(h, logging.StreamHandler) and not isinstance(h, logging.NullHandler)
        for h in lg.handlers
    )


def test_setup_logging_writes_to_file(tmp_path):
    from netdiag.cli import _setup_logging

    log_path = tmp_path / "netdiag.log"
    _setup_logging(log_file=str(log_path))
    logging.getLogger("netdiag.test").warning("hello-from-test")
    for h in logging.getLogger("netdiag").handlers:
        h.flush()

    assert log_path.exists()
    assert "hello-from-test" in log_path.read_text(encoding="utf-8")

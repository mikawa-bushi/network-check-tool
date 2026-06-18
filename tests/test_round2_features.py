"""Round-2 review features (2026-06-17+): F6 notification sinks.

WebhookSink/SlackSink POST via stdlib urllib; DesktopSink shells out to
notify-send/osascript; MultiSink isolates a failing sink. Network and
subprocess calls are stubbed so the tests stay offline and fast.
"""

import json
import urllib.request

import pytest
from click.testing import CliRunner

import netdiag.alerting as al
from netdiag.alerting import DesktopSink, MultiSink, SlackSink, Transition, WebhookSink
from netdiag.cli import cli


def _transition():
    return Transition(
        event="degraded",
        from_state="healthy",
        to_state="degraded",
        ts=1700000000.0,
        cause="dns",
        confidence="high",
        summary="DNS down",
    )


class _FakeResp:
    def close(self):
        pass


def test_webhook_sink_posts_event_dict(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["url"] = req.full_url
        sent["body"] = json.loads(req.data.decode("utf-8"))
        sent["timeout"] = timeout
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    WebhookSink("http://example/hook", timeout=3.0).emit(_transition())
    assert sent["url"] == "http://example/hook"
    assert sent["body"]["event"] == "degraded"
    assert sent["body"]["cause"] == "dns"
    assert sent["timeout"] == 3.0


def test_slack_sink_posts_text_payload(monkeypatch):
    sent = {}

    def fake_urlopen(req, timeout=None):
        sent["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    SlackSink("http://hooks.slack/x").emit(_transition())
    assert set(sent["body"]) == {"text"}
    assert "DEGRADED" in sent["body"]["text"]


def test_desktop_sink_invokes_notify_send(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        al.shutil,
        "which",
        lambda name: "/bin/notify-send" if name == "notify-send" else None,
    )
    monkeypatch.setattr(
        al.subprocess, "run", lambda argv, **kw: calls.setdefault("argv", argv)
    )
    DesktopSink().emit(_transition())
    assert calls["argv"][0] == "notify-send"


def test_desktop_sink_raises_without_a_notifier(monkeypatch):
    monkeypatch.setattr(al.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError):
        DesktopSink().emit(_transition())


def test_multisink_isolates_a_failing_sink():
    delivered = []

    class _Good:
        def emit(self, t):
            delivered.append(t)

    class _Bad:
        def emit(self, t):
            raise ValueError("boom")

    MultiSink([_Bad(), _Good()]).emit(_transition())  # must not raise
    assert len(delivered) == 1


def test_watch_help_lists_notification_channels():
    out = CliRunner().invoke(cli, ["watch", "--help"]).output
    assert "--webhook-url" in out
    assert "--slack-url" in out
    assert "--desktop" in out


# ----------------------------------------------------------- F7: snapshot diff JSON


def test_snapshot_diff_to_dict_only_changed_sources():
    from netdiag.snapshot.diff import diff_snapshots

    base = {
        "captured_at_iso": "t0",
        "sources": {"routes": "default via 10.0.0.1\n", "hosts": "127.0.0.1 lo\n"},
    }
    cur = {
        "captured_at_iso": "t1",
        "sources": {"routes": "default via 10.0.0.2\n", "hosts": "127.0.0.1 lo\n"},
    }
    obj = diff_snapshots(base, cur).to_dict()
    assert obj["any_change"] is True
    assert {c["key"] for c in obj["changed"]} == {"routes"}  # unchanged hosts excluded
    routes = obj["changed"][0]
    assert "default via 10.0.0.2" in routes["added"]
    assert "default via 10.0.0.1" in routes["removed"]
    assert routes["label"]  # human label present


def test_snapshot_diff_to_dict_no_change():
    s = {"captured_at_iso": "t", "sources": {"routes": "x\n"}}
    obj = diff_snapshots_noop(s)
    assert obj["any_change"] is False
    assert obj["changed"] == []


def diff_snapshots_noop(snapshot):
    from netdiag.snapshot.diff import diff_snapshots

    return diff_snapshots(snapshot, dict(snapshot)).to_dict()


def test_snapshot_diff_cli_json(monkeypatch):
    import netdiag.snapshot as snapmod

    monkeypatch.setattr(
        snapmod,
        "load",
        lambda name: {"captured_at_iso": "t0", "sources": {"routes": "a\n"}},
    )
    monkeypatch.setattr(
        snapmod,
        "capture",
        lambda: {"captured_at_iso": "t1", "sources": {"routes": "b\n"}},
    )
    result = CliRunner().invoke(cli, ["snapshot", "diff", "x", "--format", "json"])
    assert result.exit_code == 2  # changed -> exit 2 (mirrors text)
    obj = json.loads(result.output)
    assert obj["any_change"] is True
    assert obj["changed"][0]["key"] == "routes"

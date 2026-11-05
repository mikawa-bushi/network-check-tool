"""Always-on glue: state machine, flap damping, and alert sinks.

The monitor loop produces one observation per tick (healthy or not). On its own
that's too noisy to alert on — a single dropped ping would fire. This module adds
a debounced state machine (``healthy ⇄ degraded``) that only emits an event when
a change is *confirmed* over several ticks, plus pluggable sinks that deliver
those events. The first sink is file/JSONL: zero dependencies, offline, and easy
for other tools to tail.

Severity vocabulary is deliberately small (degraded/recovered/started/stopped)
so the output is trivial to act on.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional, Protocol

logger = logging.getLogger(__name__)


class State(str, Enum):
    UNKNOWN = "unknown"  # before enough observations to commit
    HEALTHY = "healthy"
    DEGRADED = "degraded"


@dataclass
class Transition:
    """A confirmed state change worth telling someone about."""

    event: str  # "degraded" | "recovered" | "started" | "stopped"
    from_state: str
    to_state: str
    ts: float
    cause: Optional[str] = None
    confidence: Optional[str] = None
    summary: str = ""
    evidence: List[str] = field(default_factory=list)
    consequences: List[str] = field(default_factory=list)
    runbook: List[str] = field(default_factory=list)
    duration_s: Optional[float] = None  # how long the prior state lasted
    host: Optional[str] = None  # the monitored device, when the alert is about one
    diagnosis: str = ""  # root-cause explanation, when a diagnosis ran

    @property
    def iso(self) -> str:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.ts))

    def to_dict(self) -> dict:
        d = asdict(self)
        d["iso"] = self.iso
        return d


class StateMachine:
    """Debounced healthy/degraded tracker (flap damping).

    A degraded observation must repeat ``fail_threshold`` times in a row before
    we declare DEGRADED; symmetrically ``recover_threshold`` good ticks are
    required to call it HEALTHY again. This stops a single blip — or a flapping
    link — from emitting a storm of alerts.
    """

    def __init__(
        self,
        fail_threshold: int = 2,
        recover_threshold: int = 2,
        host: Optional[str] = None,
    ) -> None:
        self.fail_threshold = max(1, fail_threshold)
        self.recover_threshold = max(1, recover_threshold)
        self.host = host  # stamped on every Transition
        self.state = State.UNKNOWN
        self._bad_streak = 0
        self._good_streak = 0
        self._since = time.time()  # when the current state began
        self._cause: Optional[str] = None

    def observe(
        self,
        healthy: bool,
        ts: Optional[float] = None,
        cause: Optional[str] = None,
        confidence: Optional[str] = None,
        summary: str = "",
        evidence: Optional[List[str]] = None,
        consequences: Optional[List[str]] = None,
        runbook: Optional[List[str]] = None,
    ) -> Optional[Transition]:
        """Feed one observation; return a Transition iff the state changed."""
        ts = ts if ts is not None else time.time()
        if healthy:
            self._good_streak += 1
            self._bad_streak = 0
        else:
            self._bad_streak += 1
            self._good_streak = 0

        # UNKNOWN is treated like HEALTHY for the purpose of catching a degradation,
        # but it never emits a spurious "recovered" at startup.
        if self.state in (State.UNKNOWN, State.HEALTHY):
            if self._bad_streak >= self.fail_threshold:
                prior = self.state
                duration = ts - self._since
                self.state = State.DEGRADED
                self._since = ts
                self._cause = cause
                return Transition(
                    event="degraded",
                    from_state=prior.value,
                    to_state=self.state.value,
                    ts=ts,
                    cause=cause,
                    confidence=confidence,
                    summary=summary,
                    evidence=evidence or [],
                    consequences=consequences or [],
                    runbook=runbook or [],
                    duration_s=duration if prior != State.UNKNOWN else None,
                    host=self.host,
                )
        elif self.state == State.DEGRADED:
            if self._good_streak >= self.recover_threshold:
                duration = ts - self._since
                self.state = State.HEALTHY
                self._since = ts
                self._cause = None
                return Transition(
                    event="recovered",
                    from_state=State.DEGRADED.value,
                    to_state=self.state.value,
                    ts=ts,
                    summary=summary,
                    duration_s=duration,
                    host=self.host,
                )
        return None

    def snapshot(self) -> dict:
        return {
            "state": self.state.value,
            "since": self._since,
            "since_iso": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(self._since)
            ),
            "cause": self._cause,
        }


# --- sinks ------------------------------------------------------------------


class AlertSink(Protocol):
    def emit(self, transition: Transition) -> None: ...


class FileAlertSink:
    """Append events to a JSONL file (machine) and a .log file (human).

    JSONL is one self-contained JSON object per line — trivially tailed,
    grepped, or shipped to another tool, with no external dependency and no
    network. The human log mirrors it in a readable one-liner.
    """

    DEFAULT_DIR = Path.home() / ".netdiag"

    def __init__(
        self, jsonl_path: Optional[Path] = None, log_path: Optional[Path] = None
    ) -> None:
        base = self.DEFAULT_DIR
        base.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = Path(jsonl_path) if jsonl_path else base / "alerts.jsonl"
        self.log_path = Path(log_path) if log_path else base / "alerts.log"
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, transition: Transition) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(transition.to_dict(), ensure_ascii=False) + "\n")
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(self._format_line(transition) + "\n")

    @staticmethod
    def _format_line(t: Transition) -> str:
        if t.event == "degraded":
            dur = f" (healthy for {t.duration_s:.0f}s)" if t.duration_s else ""
            head = (
                f"[{t.iso}] DEGRADED — 原因: {t.cause or '?'} "
                f"[{t.confidence or '?'}]{dur}"
            )
            if t.summary:
                head += f" :: {t.summary}"
            if t.consequences:
                head += f" | 連動: {', '.join(t.consequences)}"
            return head
        if t.event == "recovered":
            dur = f" (degraded for {t.duration_s:.0f}s)" if t.duration_s else ""
            return f"[{t.iso}] RECOVERED{dur}"
        return f"[{t.iso}] {t.event.upper()} {t.summary}"


class WebhookSink:
    """POST each event as JSON to a webhook URL (stdlib ``urllib`` only).

    The connect/read timeout is mandatory so a stuck endpoint can't hang the
    watcher -- a network monitor must never block on its own notifications.
    """

    def __init__(self, url: str, timeout: float = 5.0) -> None:
        self.url = url
        self.timeout = timeout

    def _post(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        # Close the response so the connection is released promptly.
        urllib.request.urlopen(req, timeout=self.timeout).close()

    def emit(self, transition: Transition) -> None:
        self._post(transition.to_dict())


class SlackSink(WebhookSink):
    """Post a human one-liner to a Slack-style incoming webhook (``{"text": ...}``)."""

    def emit(self, transition: Transition) -> None:
        self._post({"text": FileAlertSink._format_line(transition)})


class DesktopSink:
    """Best-effort OS desktop notification (Linux ``notify-send`` / macOS ``osascript``)."""

    def __init__(self, timeout: float = 5.0) -> None:
        self.timeout = timeout

    def emit(self, transition: Transition) -> None:
        line = FileAlertSink._format_line(transition)
        if shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", "netdiag", line], timeout=self.timeout, check=False
            )
        elif shutil.which("osascript"):
            script = f'display notification {json.dumps(line)} with title "netdiag"'
            subprocess.run(
                ["osascript", "-e", script], timeout=self.timeout, check=False
            )
        else:
            # Surface via MultiSink's logging rather than silently no-op'ing.
            raise RuntimeError("no desktop notifier (notify-send/osascript) found")


class MultiSink:
    """Fan an event out to several sinks; one failing sink never blocks others."""

    def __init__(self, sinks: List[AlertSink]) -> None:
        self.sinks = sinks

    def emit(self, transition: Transition) -> None:
        for s in self.sinks:
            try:
                s.emit(transition)
            except Exception:
                # One sink failing must not stop the others, but the failure
                # must be observable rather than vanishing silently.
                logger.warning(
                    "alert sink %s failed to emit", type(s).__name__, exc_info=True
                )

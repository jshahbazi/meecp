"""Content-free, host-session request counters with bounded rate buckets."""
from __future__ import annotations

import math
import socket
import struct
import time
from collections import deque
from datetime import datetime, timezone

ACTIVITY_FRAME = struct.Struct("!Bd")
ACTIVITY_EVENTS = {1: "received", 2: "succeeded", 3: "failed"}


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Activity:
    """Event-loop owned counters. No request identifiers or payloads are retained."""

    def __init__(self, *, started_at=None, clock=time.monotonic, wall_clock=timestamp):
        self.clock, self.wall_clock = clock, wall_clock
        self.started_at = started_at or wall_clock()
        self.received = self.succeeded = self.failed = self.in_flight = 0
        self.available = True
        self.last_latency_ms = self.last_request_at = None
        self.buckets = deque(maxlen=60)

    def record(self, event: str, latency_ms=None):
        if event == "received":
            if latency_ms is not None:
                raise ValueError("invalid_activity")
            self.received += 1
            self.in_flight += 1
            self.last_request_at = self.wall_clock()
            second = math.floor(self.clock())
            if self.buckets and self.buckets[-1][0] == second:
                self.buckets[-1][1] += 1
            else:
                self.buckets.append([second, 1])
        elif event in {"succeeded", "failed"}:
            if (type(latency_ms) not in {int, float} or not math.isfinite(latency_ms)
                    or latency_ms < 0 or self.in_flight == 0):
                raise ValueError("invalid_activity")
            self.in_flight -= 1
            if event == "succeeded":
                self.succeeded += 1
            else:
                self.failed += 1
            self.last_latency_ms = round(latency_ms, 2)
        else:
            raise ValueError("invalid_activity")

    def abandon(self, count: int):
        """A worker exited before delivering outcomes for its admitted requests."""
        if type(count) is not int or not 0 <= count <= self.in_flight:
            raise ValueError("invalid_activity")
        self.in_flight -= count
        self.failed += count

    def snapshot(self):
        cutoff = math.floor(self.clock()) - 59
        while self.buckets and self.buckets[0][0] < cutoff:
            self.buckets.popleft()
        return {"available": self.available, "received": self.received, "succeeded": self.succeeded, "failed": self.failed,
                "in_flight": self.in_flight,
                "requests_last_minute": sum(count for _, count in self.buckets),
                "last_latency_ms": self.last_latency_ms, "last_request_at": self.last_request_at,
                "started_at": self.started_at}


class ActivityReporter:
    """Nonblocking fixed-size events on a dedicated, bounded private socket.

    If its kernel buffer fills, close the channel. The host then explicitly marks
    these counts unavailable; telemetry never queues or interrupts tool work.
    """
    def __init__(self, channel: socket.socket):
        self.channel = channel
        self.channel.setblocking(False)
        self.failed = False

    def send(self, code, latency=0):
        if self.failed:
            return False
        try:
            if self.channel.send(ACTIVITY_FRAME.pack(code, latency)) != ACTIVITY_FRAME.size:
                raise OSError()
            return True
        except (OSError, ValueError, OverflowError):
            self.failed = True
            self.channel.close()
            return False

    def report(self, event, latency):
        return self.send({"received": 1, "succeeded": 2, "failed": 3}[event], latency or 0)

    def close(self):
        self.send(0)
        self.channel.close()


class RequestActivity:
    """One request's bookkeeping, independent of any HTTP response follower."""

    def __init__(self, collector: Activity, report=None):
        self.collector, self.report = collector, report
        self.started = collector.clock()
        self.done = self.transferred = False

    def begin(self):
        self.collector.record("received")
        if self.report:
            self._report("received", None)

    def _report(self, event, latency):
        try:
            if self.report(event, latency) is False:
                self.collector.available = False
        except Exception:
            self.collector.available = False

    def finish(self, succeeded: bool):
        if self.done:
            return
        self.done = True
        elapsed = max(0, (self.collector.clock() - self.started) * 1000)
        event = "succeeded" if succeeded else "failed"
        self.collector.record(event, elapsed)
        if self.report:
            self._report(event, elapsed)

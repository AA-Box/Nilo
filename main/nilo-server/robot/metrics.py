"""Counters, gauges and histograms, in the Prometheus text format, with no dependency.

    metrics = MetricRegistry()
    metrics.counter("nilo_robot_tool_calls_total", tool="robot_motion_move").inc()
    metrics.histogram("nilo_robot_tool_latency_seconds", tool="robot_motion_move").observe(0.04)
    print(metrics.render())

Why not a client library: the three metric shapes below are the whole surface this
subsystem needs, the exposition format is a dozen lines, and a scrape endpoint that is
*always* available matters more than a feature set — ``prometheus_client`` is not in the
dev dependency slice, so a metrics module built on it would be a module the lint job and
half of CI cannot import.

Everything is synchronous and cheap. Recording a metric never awaits, never allocates a
task and never touches the event loop, because the call sites are an event-bus subscriber
and an audio path, and neither may be slowed down by bookkeeping.

Not thread-safe by design. Metrics are written from the event loop thread only (the
observability subscriber and the API handler that renders them); the one subsystem that
runs off-loop — the action watchdog — hands its work back to the loop before anything is
recorded (``robot/actions/executor.py``). A lock here would be a lock on the audio path
that protects nothing.
"""

from __future__ import annotations

from typing import Any

#: Bucket edges, in seconds. Chosen for what this subsystem measures: a device tool call
#: is tens of milliseconds, a model turn is seconds, and anything past thirty is a
#: timeout somebody needs to see rather than a number to bucket finely.
DEFAULT_BUCKETS: tuple[float, ...] = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

#: Label values are free text from devices and models. Truncated so one robot with a
#: pathological tool name cannot make a scrape unreadable, and so the label set of a
#: long-running process stays bounded.
MAX_LABEL_LENGTH = 64

Labels = tuple[tuple[str, str], ...]


class Counter:
    """A number that only goes up. One per label set."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0.0

    def inc(self, amount: float = 1.0) -> None:
        if amount < 0:
            raise ValueError("a counter cannot decrease")
        self.value += amount


class Gauge:
    """A number that goes up and down: how many robots are connected right now."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0.0

    def set(self, value: float) -> None:
        self.value = float(value)

    def inc(self, amount: float = 1.0) -> None:
        self.value += amount

    def dec(self, amount: float = 1.0) -> None:
        self.value -= amount


class Histogram:
    """Cumulative buckets, a sum and a count — what a latency question is asked of."""

    __slots__ = ("buckets", "counts", "sum", "total")

    def __init__(self, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self.buckets = buckets
        self.counts = [0] * len(buckets)
        self.sum = 0.0
        self.total = 0

    def observe(self, value: float) -> None:
        """Record one measurement. A negative value is ignored rather than fatal.

        Negative durations happen: a monotonic clock read on either side of a suspend, a
        timestamp that arrived from a device with its own clock. Dropping one is a
        missing sample; recording it corrupts the sum for the life of the process.
        """
        if value < 0:
            return
        self.sum += value
        self.total += 1
        for index, edge in enumerate(self.buckets):
            if value <= edge:
                self.counts[index] += 1

    @property
    def average(self) -> float:
        return self.sum / self.total if self.total else 0.0


class MetricRegistry:
    """Every metric in one process, and the text a scrape returns.

    Constructed per runtime rather than as module state, so two tests in one process
    never see each other's numbers (the same rule the event bus and the registry follow).
    """

    def __init__(self, *, buckets: tuple[float, ...] = DEFAULT_BUCKETS) -> None:
        self._buckets = buckets
        self._help: dict[str, str] = {}
        self._counters: dict[str, dict[Labels, Counter]] = {}
        self._gauges: dict[str, dict[Labels, Gauge]] = {}
        self._histograms: dict[str, dict[Labels, Histogram]] = {}

    # -- recording -----------------------------------------------------------------------

    def describe(self, name: str, help_text: str) -> None:
        """Attach a HELP line to a metric name. Optional, and idempotent."""
        self._help[name] = help_text

    def counter(self, name: str, **labels: Any) -> Counter:
        family = self._counters.setdefault(name, {})
        key = _labels(labels)
        found = family.get(key)
        if found is None:
            found = family[key] = Counter()
        return found

    def gauge(self, name: str, **labels: Any) -> Gauge:
        family = self._gauges.setdefault(name, {})
        key = _labels(labels)
        found = family.get(key)
        if found is None:
            found = family[key] = Gauge()
        return found

    def histogram(self, name: str, **labels: Any) -> Histogram:
        family = self._histograms.setdefault(name, {})
        key = _labels(labels)
        found = family.get(key)
        if found is None:
            found = family[key] = Histogram(self._buckets)
        return found

    # -- reading -------------------------------------------------------------------------

    def value(self, name: str, **labels: Any) -> float:
        """One number, for a test or a health check. ``0.0`` when nothing recorded it.

        A histogram answers with its observation count, which is what "did this happen?"
        means for a latency metric.
        """
        key = _labels(labels)
        counter = self._counters.get(name, {}).get(key)
        if counter is not None:
            return counter.value
        gauge = self._gauges.get(name, {}).get(key)
        if gauge is not None:
            return gauge.value
        histogram = self._histograms.get(name, {}).get(key)
        return float(histogram.total) if histogram is not None else 0.0

    def snapshot(self) -> dict[str, Any]:
        """Every metric as plain data, for the management API's JSON view."""
        return {
            "counters": {
                name: {_render_labels(key) or "": series.value for key, series in family.items()}
                for name, family in sorted(self._counters.items())
            },
            "gauges": {
                name: {_render_labels(key) or "": series.value for key, series in family.items()}
                for name, family in sorted(self._gauges.items())
            },
            "histograms": {
                name: {
                    _render_labels(key)
                    or "": {
                        "count": series.total,
                        "sum": round(series.sum, 6),
                        "average": round(series.average, 6),
                    }
                    for key, series in family.items()
                }
                for name, family in sorted(self._histograms.items())
            },
        }

    def render(self) -> str:
        """The Prometheus text exposition format, version 0.0.4."""
        lines: list[str] = []
        for name, counters in sorted(self._counters.items()):
            self._header(lines, name, "counter")
            for key, counter in sorted(counters.items()):
                lines.append(f"{name}{_render_labels(key)} {_number(counter.value)}")
        for name, gauges in sorted(self._gauges.items()):
            self._header(lines, name, "gauge")
            for key, gauge in sorted(gauges.items()):
                lines.append(f"{name}{_render_labels(key)} {_number(gauge.value)}")
        for name, histograms in sorted(self._histograms.items()):
            self._header(lines, name, "histogram")
            for key, histogram in sorted(histograms.items()):
                # `counts` is already cumulative: `observe` increments every bucket the
                # value fits in, which is what `_bucket` means in this format.
                for edge, count in zip(histogram.buckets, histogram.counts, strict=True):
                    lines.append(f"{name}_bucket{_render_labels(key, le=_number(edge))} {count}")
                lines.append(f"{name}_bucket{_render_labels(key, le='+Inf')} {histogram.total}")
                lines.append(f"{name}_sum{_render_labels(key)} {_number(histogram.sum)}")
                lines.append(f"{name}_count{_render_labels(key)} {histogram.total}")
        return "\n".join(lines) + "\n" if lines else ""

    def _header(self, lines: list[str], name: str, kind: str) -> None:
        help_text = self._help.get(name)
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")


def _labels(labels: dict[str, Any]) -> Labels:
    """Normalize a label dictionary into a sorted, bounded, hashable key."""
    return tuple(sorted((str(name), _label_value(value)) for name, value in labels.items() if value is not None))


def _label_value(value: Any) -> str:
    text = str(value)
    return text[:MAX_LABEL_LENGTH] if len(text) > MAX_LABEL_LENGTH else text


def _render_labels(key: Labels, **extra: str) -> str:
    pairs = [*key, *sorted(extra.items())]
    if not pairs:
        return ""
    rendered = ",".join(f'{name}="{_escape(value)}"' for name, value in pairs)
    return "{" + rendered + "}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _number(value: float) -> str:
    """Prometheus wants a bare number; an integral float should not render as ``1.0``."""
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return repr(value)


__all__ = [
    "DEFAULT_BUCKETS",
    "MAX_LABEL_LENGTH",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricRegistry",
]

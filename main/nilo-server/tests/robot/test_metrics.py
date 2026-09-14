"""The metric registry's arithmetic and its exposition format, with no event loop."""

from __future__ import annotations

import pytest

from robot.metrics import DEFAULT_BUCKETS, MAX_LABEL_LENGTH, Counter, Gauge, Histogram, MetricRegistry


def test_a_counter_only_goes_up() -> None:
    counter = Counter()
    counter.inc()
    counter.inc(4)
    assert counter.value == 5
    with pytest.raises(ValueError):
        counter.inc(-1)


def test_a_gauge_moves_both_ways() -> None:
    gauge = Gauge()
    gauge.inc(3)
    gauge.dec()
    gauge.set(7)
    assert gauge.value == 7


def test_histogram_buckets_are_cumulative() -> None:
    histogram = Histogram((0.1, 1.0))
    for value in (0.05, 0.5, 5.0):
        histogram.observe(value)
    assert histogram.counts == [1, 2]  # <=0.1 and <=1.0
    assert histogram.total == 3
    assert histogram.sum == pytest.approx(5.55)
    assert histogram.average == pytest.approx(1.85)


def test_a_negative_observation_is_dropped_rather_than_poisoning_the_sum() -> None:
    """A clock that went backwards costs one sample, not every future answer."""
    histogram = Histogram()
    histogram.observe(-1.0)
    histogram.observe(0.5)
    assert histogram.total == 1
    assert histogram.sum == pytest.approx(0.5)


def test_the_same_labels_are_the_same_series_whatever_order_they_came_in() -> None:
    registry = MetricRegistry()
    registry.counter("calls", tool="move", robot="a").inc()
    registry.counter("calls", robot="a", tool="move").inc()
    assert registry.value("calls", tool="move", robot="a") == 2


def test_a_none_label_is_not_a_label() -> None:
    registry = MetricRegistry()
    registry.counter("calls", tool="move", person=None).inc()
    assert registry.value("calls", tool="move") == 1


def test_a_long_label_value_is_truncated_so_one_device_cannot_ruin_a_scrape() -> None:
    registry = MetricRegistry()
    registry.counter("calls", tool="x" * 400).inc()
    rendered = registry.render()
    assert "x" * MAX_LABEL_LENGTH in rendered
    assert "x" * (MAX_LABEL_LENGTH + 1) not in rendered


def test_value_of_something_nothing_recorded_is_zero() -> None:
    assert MetricRegistry().value("never_seen") == 0.0


def test_a_histogram_answers_value_with_its_observation_count() -> None:
    registry = MetricRegistry()
    registry.histogram("latency").observe(0.2)
    registry.histogram("latency").observe(0.3)
    assert registry.value("latency") == 2


def test_the_exposition_format_is_the_one_prometheus_parses() -> None:
    registry = MetricRegistry(buckets=(0.1, 1.0))
    registry.describe("nilo_calls_total", "How many.")
    registry.counter("nilo_calls_total", tool="move").inc(2)
    registry.gauge("nilo_connected").set(1)
    registry.histogram("nilo_latency_seconds", tool="move").observe(0.5)

    lines = registry.render().splitlines()
    assert "# HELP nilo_calls_total How many." in lines
    assert "# TYPE nilo_calls_total counter" in lines
    assert 'nilo_calls_total{tool="move"} 2' in lines
    assert "# TYPE nilo_connected gauge" in lines
    assert "nilo_connected 1" in lines
    assert 'nilo_latency_seconds_bucket{tool="move",le="0.1"} 0' in lines
    assert 'nilo_latency_seconds_bucket{tool="move",le="1"} 1' in lines
    assert 'nilo_latency_seconds_bucket{tool="move",le="+Inf"} 1' in lines
    assert 'nilo_latency_seconds_sum{tool="move"} 0.5' in lines
    assert 'nilo_latency_seconds_count{tool="move"} 1' in lines


def test_an_empty_registry_renders_nothing_rather_than_a_blank_line() -> None:
    assert MetricRegistry().render() == ""


def test_a_label_value_with_a_quote_or_a_newline_is_escaped() -> None:
    registry = MetricRegistry()
    registry.counter("calls", tool='we"ird\nname').inc()
    rendered = registry.render()
    assert '\\"' in rendered and "\\n" in rendered
    assert rendered.count("\n") == 2  # the TYPE line and the sample, and nothing split


def test_the_snapshot_is_json_able_data() -> None:
    import json

    registry = MetricRegistry()
    registry.counter("calls", tool="move").inc()
    registry.gauge("connected").set(2)
    registry.histogram("latency").observe(0.25)
    snapshot = registry.snapshot()
    assert json.loads(json.dumps(snapshot)) == snapshot
    assert snapshot["counters"]["calls"]["{tool=\"move\"}"] == 1
    assert snapshot["gauges"]["connected"][""] == 2
    assert snapshot["histograms"]["latency"][""]["count"] == 1


def test_two_registries_do_not_share_anything() -> None:
    first, second = MetricRegistry(), MetricRegistry()
    first.counter("calls").inc()
    assert second.value("calls") == 0.0


def test_the_default_buckets_are_ordered_and_cover_a_timeout() -> None:
    assert list(DEFAULT_BUCKETS) == sorted(DEFAULT_BUCKETS)
    assert DEFAULT_BUCKETS[-1] >= 30.0

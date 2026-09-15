import csv
import logging
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import entropy_arb.strategy_recorder as strategy_recorder_module  # noqa: E402
from entropy_arb.strategy_recorder import (  # noqa: E402
    STRATEGY_EVENT_HEADER,
    StrategyEvent,
    StrategyEventRecorder,
)


def event(*, ts=60.1, intent="SKIP", reason="REFERENCE_STALE",
          campaign_id="", direction="sell_entropy", decision_id=""):
    return StrategyEvent(
        ts=ts,
        mode="shadow",
        event="decision",
        intent=intent,
        reason=reason,
        decision_id=decision_id,
        campaign_id=campaign_id,
        entropy_symbol="ANTH",
        entropy_dex="io",
        hedge_symbol="ANTHROPIC",
        hedge_venue="lighter-rh",
        direction=direction,
    )


def read_rows(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_event_records_top_and_marginal_convergence_separately(tmp_path):
    path = tmp_path / "events.csv"
    recorder = StrategyEventRecorder(path)

    recorder.record(StrategyEvent(
        ts=60.1,
        mode="shadow",
        event="decision",
        intent="OPEN",
        top_convergence_bps=32.5,
        convergence_bps=27.5,
    ))
    recorder.close()

    row = read_rows(path)[0]
    assert float(row["top_convergence_bps"]) == pytest.approx(32.5)
    assert float(row["convergence_bps"]) == pytest.approx(27.5)


def test_repeated_skip_is_written_once_per_reason_per_minute(tmp_path):
    path = tmp_path / "strategy-events.csv"
    recorder = StrategyEventRecorder(path)
    recorder.record(event(ts=60.1))
    recorder.record(event(ts=60.9))
    recorder.record(event(ts=120.0))
    recorder.close()

    assert len(read_rows(path)) == 2


def test_skip_reason_campaign_or_direction_change_is_not_coalesced(tmp_path):
    path = tmp_path / "events.csv"
    recorder = StrategyEventRecorder(path)
    recorder.record(event(reason="MODEL_NOT_READY"))
    recorder.record(event(reason="REFERENCE_STALE"))
    recorder.record(event(reason="REFERENCE_STALE", campaign_id="c-1"))
    recorder.record(event(reason="REFERENCE_STALE", campaign_id="c-1",
                          direction="buy_entropy"))
    recorder.close()

    assert len(read_rows(path)) == 4


def test_lifecycle_events_are_never_coalesced(tmp_path):
    path = tmp_path / "events.csv"
    recorder = StrategyEventRecorder(path)
    recorder.record(event(ts=1, intent="OPEN", reason="ENTRY_READY"))
    recorder.record(event(ts=1.1, intent="OPEN", reason="ENTRY_READY"))
    recorder.record(event(ts=2, intent="CLOSE", reason="TARGET_REACHED"))
    recorder.close()

    assert [row["intent"] for row in read_rows(path)] == [
        "OPEN", "OPEN", "CLOSE"]


def test_existing_incompatible_header_is_archived(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text("wrong,header\n", encoding="utf-8")

    recorder = StrategyEventRecorder(path)
    recorder.close()

    assert (tmp_path / "events.csv.old").read_text(
        encoding="utf-8") == "wrong,header\n"
    assert read_rows(path) == []


def test_incomplete_tail_is_archived_without_modifying_original_bytes(
        tmp_path, caplog):
    path = tmp_path / "events.csv"
    recorder = StrategyEventRecorder(path)
    recorder.record(event(intent="OPEN", decision_id="execution-first"))
    recorder.close()
    damaged = path.read_bytes() + b"123,partial"
    path.write_bytes(damaged)

    with caplog.at_level(logging.WARNING):
        restarted = StrategyEventRecorder(path)
        restarted.close()

    archive = tmp_path / "events.csv.old"
    assert archive.read_bytes() == damaged
    assert str(path) in caplog.text
    assert str(archive) in caplog.text
    assert read_rows(path) == []


def test_decision_id_is_deduplicated_across_restart(tmp_path):
    path = tmp_path / "events.csv"
    first = StrategyEventRecorder(path)
    assert first.record(event(
        intent="OPEN",
        decision_id="execution-abc",
    ))
    first.close()

    second = StrategyEventRecorder(path)
    assert not second.record(event(
        ts=61.0,
        intent="OPEN",
        decision_id="execution-abc",
    ))
    second.close()

    assert [row["decision_id"] for row in read_rows(path)] == [
        "execution-abc"]


def test_restart_deduplicates_without_path_read_bytes(tmp_path, monkeypatch):
    path = tmp_path / "events.csv"
    first = StrategyEventRecorder(path)
    assert first.record(event(
        intent="OPEN",
        decision_id="execution-streamed",
    ))
    first.close()

    def fail_read_bytes(self):
        raise AssertionError(f"Path.read_bytes must not be called: {self}")

    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    second = StrategyEventRecorder(path)
    assert not second.record(event(
        ts=61.0,
        intent="OPEN",
        decision_id="execution-streamed",
    ))
    second.close()


def test_restart_inspects_and_appends_through_one_open_handle(
        tmp_path, monkeypatch):
    path = tmp_path / "events.csv"
    original = StrategyEventRecorder(path)
    assert original.record(event(
        intent="OPEN",
        decision_id="execution-original",
    ))
    original.close()

    replacement = tmp_path / "replacement.csv"
    other = StrategyEventRecorder(replacement)
    assert other.record(event(
        intent="OPEN",
        decision_id="execution-replacement",
    ))
    other.close()
    replacement.write_bytes(replacement.read_bytes().rstrip(b"\r\n"))

    real_open = Path.open
    target_open_count = 0

    def swap_before_reopen(self, *args, **kwargs):
        nonlocal target_open_count
        if self == path:
            target_open_count += 1
            if target_open_count == 2:
                os.replace(replacement, path)
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", swap_before_reopen)
    restarted = StrategyEventRecorder(path)
    try:
        assert not restarted.record(event(
            ts=61.0,
            intent="OPEN",
            decision_id="execution-original",
        ))
    finally:
        restarted.close()

    assert target_open_count == 1


def test_restart_streams_reader_without_materializing_rows(
        tmp_path, monkeypatch):
    path = tmp_path / "events.csv"
    first = StrategyEventRecorder(path)
    for index in range(512):
        assert first.record(event(
            ts=60.1 + index,
            intent="OPEN",
            decision_id=f"execution-{index}",
        ))
    first.close()

    real_reader = strategy_recorder_module.csv.reader

    class StreamingReader:
        def __init__(self, delegate):
            self._delegate = delegate

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._delegate)

        def __length_hint__(self):
            raise AssertionError("CSV rows must not be materialized")

    def streaming_reader(*args, **kwargs):
        return StreamingReader(real_reader(*args, **kwargs))

    def fail_read_bytes(self):
        raise AssertionError(f"Path.read_bytes must not be called: {self}")

    monkeypatch.setattr(strategy_recorder_module.csv, "reader",
                        streaming_reader)
    monkeypatch.setattr(Path, "read_bytes", fail_read_bytes)
    second = StrategyEventRecorder(path)
    try:
        assert not second.record(event(
            ts=1000.0,
            intent="OPEN",
            decision_id="execution-511",
        ))
    finally:
        second.close()


def test_close_is_idempotent_and_record_after_close_fails(tmp_path):
    path = tmp_path / "events.csv"
    recorder = StrategyEventRecorder(path)
    recorder.close()
    recorder.close()

    with pytest.raises(ValueError, match="closed"):
        recorder.record(event())

    with path.open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == STRATEGY_EVENT_HEADER

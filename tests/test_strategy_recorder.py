import csv
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from entropy_arb.strategy_recorder import (  # noqa: E402
    STRATEGY_EVENT_HEADER,
    StrategyEvent,
    StrategyEventRecorder,
)


def event(*, ts=60.1, intent="SKIP", reason="REFERENCE_STALE",
          campaign_id="", direction="sell_entropy"):
    return StrategyEvent(
        ts=ts,
        mode="shadow",
        event="decision",
        intent=intent,
        reason=reason,
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


def test_existing_incompatible_header_is_rejected(tmp_path):
    path = tmp_path / "events.csv"
    path.write_text("wrong,header\n", encoding="utf-8")

    with pytest.raises(ValueError, match="header"):
        StrategyEventRecorder(path)


def test_close_is_idempotent_and_record_after_close_fails(tmp_path):
    path = tmp_path / "events.csv"
    recorder = StrategyEventRecorder(path)
    recorder.close()
    recorder.close()

    with pytest.raises(ValueError, match="closed"):
        recorder.record(event())

    with path.open(newline="", encoding="utf-8") as handle:
        assert next(csv.reader(handle)) == STRATEGY_EVENT_HEADER

import csv
import math

import pytest

from entropy_arb.strategy import (
    MarketIdentity,
    ResidualModel,
    warm_start_residual_model,
)


def make_model(**overrides):
    options = {
        "window_minutes": 180,
        "min_samples": 120,
        "lower_quantile": 0.10,
        "upper_quantile": 0.90,
        "regime_window_minutes": 60,
        "recovery_minutes": 15,
    }
    options.update(overrides)
    return ResidualModel(**options)


def seed(m, values, start=0):
    for offset, value in enumerate(values):
        m.observe(
            minute=start + offset,
            residual_bps=value,
            valid=value is not None,
        )


def test_model_replaces_same_minute_and_uses_linear_quantiles():
    m = make_model(
        window_minutes=10,
        min_samples=4,
        regime_window_minutes=2,
        recovery_minutes=1,
    )
    seed(m, [0.0, 10.0, 20.0, 30.0])
    m.observe(minute=3, residual_bps=40.0, valid=True)

    snapshot = m.snapshot(now_minute=3)

    assert snapshot.samples == 4
    assert snapshot.median_bps == pytest.approx(15.0)
    assert snapshot.lower_bps == pytest.approx(3.0)
    assert snapshot.upper_bps == pytest.approx(34.0)
    assert snapshot.q25_bps == pytest.approx(7.5)
    assert snapshot.q75_bps == pytest.approx(25.0)


def test_model_uses_elapsed_minutes_not_last_n_rows():
    m = make_model(
        window_minutes=3,
        min_samples=2,
        regime_window_minutes=2,
        recovery_minutes=1,
    )
    seed(m, [1.0, 2.0], start=1)

    assert m.snapshot(now_minute=2).ready
    assert not m.snapshot(now_minute=5).ready


def test_model_is_not_ready_until_minimum_valid_samples():
    m = make_model()
    seed(m, [0.0] * 119)
    assert m.snapshot(now_minute=118).status == "MODEL_NOT_READY"

    m.observe(minute=119, residual_bps=0.0, valid=True)
    assert m.snapshot(now_minute=119).status == "READY"


def test_five_missing_real_minutes_marks_unstable_then_recovers():
    m = make_model()
    seed(m, [float(i % 5) for i in range(180)])
    assert m.snapshot(now_minute=179).ready

    seed(m, [None] * 5, start=180)
    assert m.snapshot(now_minute=184).status == "REGIME_UNSTABLE"

    seed(m, [2.0] * 14, start=185)
    assert m.snapshot(now_minute=198).status == "REGIME_UNSTABLE"
    m.observe(minute=199, residual_bps=2.0, valid=True)
    assert m.snapshot(now_minute=199).status == "READY"


def test_short_window_median_shift_marks_regime_unstable():
    m = make_model()
    seed(m, [0.0] * 149 + [100.0] * 31)

    assert m.snapshot(now_minute=179).status == "REGIME_UNSTABLE"


def test_short_window_iqr_expansion_marks_regime_unstable():
    m = make_model()
    stable = [float(i % 5) for i in range(120)]
    volatile = [-20.0 if i % 2 else 20.0 for i in range(60)]
    seed(m, stable + volatile)

    assert m.snapshot(now_minute=179).status == "REGIME_UNSTABLE"


@pytest.mark.parametrize(
    ("minute", "residual", "valid"),
    [
        (-1, 1.0, True),
        (1.5, 1.0, True),
        (True, 1.0, True),
        (1, math.nan, True),
        (1, math.inf, True),
        (1, None, True),
    ],
)
def test_model_rejects_invalid_observations(minute, residual, valid):
    with pytest.raises(ValueError):
        make_model().observe(
            minute=minute,
            residual_bps=residual,
            valid=valid,
        )


HISTORY_HEADER = [
    "minute_ts",
    "entropy_symbol",
    "entropy_dex",
    "hedge_symbol",
    "hedge_venue",
    "entropy_reference_age_ms",
    "hedge_reference_age_ms",
    "reference_update_skew_ms",
    "residual_close_bps",
]


def history_row(minute, residual="1", **overrides):
    row = {
        "minute_ts": str(minute * 60),
        "entropy_symbol": "ANTH",
        "entropy_dex": "io",
        "hedge_symbol": "ANTHROPIC",
        "hedge_venue": "lighter-rh",
        "entropy_reference_age_ms": "100",
        "hedge_reference_age_ms": "100",
        "reference_update_skew_ms": "0",
        "residual_close_bps": residual,
    }
    row.update(overrides)
    return row


def write_history(path, rows, header=HISTORY_HEADER):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def test_warm_start_filters_identity_age_skew_future_and_old_rows(tmp_path):
    path = tmp_path / "minutes.csv"
    write_history(path, [
        history_row(100, residual="1"),
        history_row(101, residual="2", entropy_symbol="OTHER"),
        history_row(102, residual="3", entropy_reference_age_ms="16000"),
        history_row(103, residual="4", reference_update_skew_ms="16000"),
        history_row(104, residual="nan"),
        history_row(-100, residual="6"),
        history_row(201, residual="5"),
    ])
    m = make_model(window_minutes=180, min_samples=1)

    loaded = warm_start_residual_model(
        m,
        path=str(path),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=200,
        max_age_sec=15,
        max_skew_sec=15,
    )

    assert loaded.accepted == 1
    assert loaded.rejected_identity == 1
    assert loaded.rejected_reference == 2
    assert loaded.rejected_value == 1
    assert loaded.rejected_time == 2
    assert m.snapshot(now_minute=200).samples == 1


def test_warm_start_rejects_incompatible_header(tmp_path):
    path = tmp_path / "minutes.csv"
    write_history(path, [], header=["minute_ts", "residual_close_bps"])

    with pytest.raises(ValueError, match="minute history header"):
        warm_start_residual_model(
            make_model(),
            path=str(path),
            identity=MarketIdentity(
                "ANTH", "io", "ANTHROPIC", "lighter-rh"),
            now_minute=200,
            max_age_sec=15,
            max_skew_sec=15,
        )


def test_warm_start_missing_file_leaves_model_not_ready(tmp_path):
    m = make_model()

    loaded = warm_start_residual_model(
        m,
        path=str(tmp_path / "missing.csv"),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=200,
        max_age_sec=15,
        max_skew_sec=15,
    )

    assert loaded.accepted == 0
    assert m.snapshot(now_minute=200).status == "MODEL_NOT_READY"


def test_warm_start_counts_blank_identity_as_rejected_row(tmp_path):
    path = tmp_path / "minutes.csv"
    write_history(path, [history_row(100, entropy_symbol="")])

    loaded = warm_start_residual_model(
        make_model(),
        path=str(path),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=100,
        max_age_sec=15,
        max_skew_sec=15,
    )

    assert loaded.rejected_identity == 1

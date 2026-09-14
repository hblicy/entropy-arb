import math

import pytest

from entropy_arb.strategy import ResidualModel


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

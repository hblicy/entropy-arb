import math

import pytest

from entropy_arb.slippage import SlippageModel


def make_model(**overrides):
    options = {
        "bootstrap_bps": 5.0,
        "min_bps": 1.0,
        "safety_bps": 1.0,
        "hard_max_bps": 20.0,
        "min_live_samples": 10,
    }
    options.update(overrides)
    return SlippageModel(**options)


def quote(model, **overrides):
    options = {
        "venue": "entropy",
        "side": "buy",
        "now": 100.0,
        "convergence_bps": 18.0,
        "round_trip_fee_bps": 2.0,
        "min_profit_bps": 2.0,
        "max_edge_fraction": 0.25,
    }
    options.update(overrides)
    return model.quote(**options)


def test_bootstrap_and_edge_share_bound_the_budget():
    result = quote(make_model())

    assert result.statistical_bps == 5.0
    assert result.edge_cap_bps == pytest.approx(3.5)
    assert result.budget_bps == pytest.approx(3.5)
    assert result.source == "bootstrap"


def test_quote_is_unavailable_when_edge_cap_is_below_minimum():
    result = quote(make_model(), convergence_bps=5.0)

    assert result.budget_bps is None
    assert result.reason == "SLIPPAGE_BUDGET_TOO_SMALL"


def test_live_samples_use_recent_hour_when_it_has_twenty_samples():
    model = make_model()
    for index in range(30):
        model.record(
            venue="entropy",
            side="buy",
            now=float(index * 100),
            adverse_bps=float(index % 10),
            decision_budget_bps=20.0,
        )

    result = quote(model, now=3000.0, convergence_bps=100.0)

    assert result.source == "live"
    assert result.sample_count == 30
    assert result.statistical_bps == pytest.approx(10.0)


def test_sparse_recent_hour_extends_to_last_fifty_samples():
    model = make_model()
    for index in range(60):
        model.record(
            venue="entropy",
            side="sell",
            now=float(index * 100),
            adverse_bps=float(index % 10),
            decision_budget_bps=20.0,
        )

    result = quote(
        model,
        venue="entropy",
        side="sell",
        now=10000.0,
        convergence_bps=100.0,
    )

    assert result.sample_count == 50
    assert result.source == "live"


def test_statistical_budget_never_exceeds_hard_max():
    model = make_model()
    for index in range(20):
        model.record(
            venue="hedge",
            side="buy",
            now=float(index),
            adverse_bps=100.0,
            decision_budget_bps=200.0,
        )

    result = quote(
        model,
        venue="hedge",
        now=20.0,
        convergence_bps=1000.0,
    )

    assert result.statistical_bps == 20.0
    assert result.budget_bps == 20.0


def test_three_breaches_halves_size_and_five_pause_entries():
    model = make_model()
    for index in range(3):
        model.record(
            venue="entropy",
            side="buy",
            now=float(index),
            adverse_bps=6.0,
            decision_budget_bps=5.0,
        )
    assert model.entry_size_factor("entropy", now=3.0) == 0.5
    assert not model.entry_paused("entropy", now=3.0)

    for index in range(3, 5):
        model.record(
            venue="entropy",
            side="sell",
            now=float(index),
            adverse_bps=6.0,
            decision_budget_bps=5.0,
        )

    assert model.entry_paused("entropy", now=5.0)
    assert not model.entry_paused("entropy", now=904.0)


def test_single_hard_limit_breach_pauses_immediately():
    model = make_model()

    model.record(
        venue="hedge",
        side="sell",
        now=10.0,
        adverse_bps=20.001,
        decision_budget_bps=20.0,
    )

    assert model.entry_paused("hedge", now=10.0)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_nonfinite_quote_inputs_are_rejected(bad):
    with pytest.raises(ValueError):
        quote(make_model(), convergence_bps=bad)


def test_shadow_path_can_avoid_recording_by_not_calling_record():
    model = make_model()

    assert model.sample_count("entropy", "buy") == 0


def test_protection_exposes_statistical_budget_without_entry_edge_cap():
    model = make_model()

    result = model.protection(venue="entropy", side="buy", now=100.0)

    assert result.budget_bps == 5.0
    assert result.source == "bootstrap"
    assert result.sample_count == 0

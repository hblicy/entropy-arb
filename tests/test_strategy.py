import csv
from dataclasses import replace
import math

import pytest

from entropy_arb.strategy import (
    DynamicResidualStrategy,
    MarketIdentity,
    MarketView,
    ModelSnapshot,
    ResidualModel,
    warm_start_residual_model,
)
from entropy_arb.book import OrderBook
from entropy_arb.campaign import PositionCampaign
from entropy_arb.slippage import SlippageModel


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


def test_same_minute_replacement_does_not_advance_regime_recovery():
    model = make_model()
    seed(model, [float(index % 5) for index in range(180)])
    seed(model, [None] * 5, start=180)
    assert model.snapshot(now_minute=184).status == "REGIME_UNSTABLE"

    for value in range(15):
        model.observe(minute=185, residual_bps=float(value), valid=True)
    assert model.snapshot(now_minute=185).status == "REGIME_UNSTABLE"

    seed(model, [2.0] * 14, start=186)
    assert model.snapshot(now_minute=199).status == "READY"


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


def test_warm_start_last_duplicate_wins_and_excludes_current_minute(tmp_path):
    path = tmp_path / "minutes.csv"
    write_history(path, [
        history_row(198, residual="1"),
        history_row(198, residual="9"),
        history_row(199, residual="3"),
        history_row(200, residual="7"),
    ])
    model = make_model(
        window_minutes=10,
        min_samples=1,
        regime_window_minutes=2,
        recovery_minutes=1,
    )

    loaded = warm_start_residual_model(
        model,
        path=str(path),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=200,
        max_age_sec=15,
        max_skew_sec=15,
    )

    snapshot = model.snapshot(now_minute=200)
    assert loaded.accepted == 2
    assert loaded.rejected_time == 1
    assert snapshot.samples == 2
    assert snapshot.median_bps == pytest.approx(6.0)


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


def test_warm_start_marks_five_minute_tail_gap_unstable(tmp_path):
    path = tmp_path / "minutes.csv"
    write_history(path, [
        history_row(minute, residual=str(float(minute % 5)))
        for minute in range(15, 195)
    ])
    model = make_model()

    loaded = warm_start_residual_model(
        model,
        path=str(path),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=200,
        max_age_sec=15,
        max_skew_sec=15,
    )

    assert loaded.accepted >= model.min_samples
    assert model.snapshot(now_minute=200).status == "REGIME_UNSTABLE"


def test_warm_start_one_minute_tail_gap_stays_ready(tmp_path):
    path = tmp_path / "minutes.csv"
    write_history(path, [
        history_row(minute, residual=str(float(minute % 5)))
        for minute in range(20, 199)
    ])
    model = make_model()

    warm_start_residual_model(
        model,
        path=str(path),
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        now_minute=200,
        max_age_sec=15,
        max_skew_sec=15,
    )

    assert model.snapshot(now_minute=200).status == "READY"


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


def decision_book(bids, asks):
    value = OrderBook()
    value.apply_hl([
        [{"px": str(px), "sz": str(size)} for px, size in bids],
        [{"px": str(px), "sz": str(size)} for px, size in asks],
    ])
    return value


def ready_snapshot(**overrides):
    values = {
        "version": 1,
        "minute": 100,
        "samples": 180,
        "status": "READY",
        "median_bps": 10.0,
        "lower_bps": -20.0,
        "q25_bps": 0.0,
        "q75_bps": 20.0,
        "upper_bps": 40.0,
    }
    values.update(overrides)
    return ModelSnapshot(**values)


def decision_strategy():
    slippage = SlippageModel(
        bootstrap_bps=5.0,
        min_bps=1.0,
        safety_bps=1.0,
        hard_max_bps=20.0,
        min_live_samples=10,
    )
    return DynamicResidualStrategy(
        slippage=slippage,
        reference_max_age_sec=15.0,
        reference_max_skew_sec=15.0,
        exit_band_fraction=0.25,
        min_exit_band_bps=0.5,
        min_expected_profit_bps=2.0,
        soft_hold_sec=3600.0,
        hard_hold_sec=21600.0,
        hard_slippage_bps=20.0,
        max_edge_fraction=0.25,
    )


def decision_market(*, sell_residual=None, buy_residual=None,
                     reference=True, e_age=1.0, h_age=1.0, skew=0.0,
                     books_ready=True, entropy_bid=None, entropy_ask=None,
                     hedge_bid=99.9, hedge_ask=100.0,
                     entry_cap_notional=500.0):
    if sell_residual is not None:
        entropy_bid = hedge_ask * (1.0 + sell_residual / 1e4)
        entropy_ask = entropy_bid + 0.01
    elif buy_residual is not None:
        entropy_ask = hedge_bid * (1.0 + buy_residual / 1e4)
        entropy_bid = entropy_ask - 0.01
    else:
        entropy_bid = 99.99 if entropy_bid is None else entropy_bid
        entropy_ask = 100.0 if entropy_ask is None else entropy_ask
    return MarketView(
        entropy_book=decision_book(
            bids=[(entropy_bid, 20)], asks=[(entropy_ask, 20)]),
        hedge_book=decision_book(
            bids=[(hedge_bid, 20)], asks=[(hedge_ask, 20)]),
        entropy_oracle_px=100.0 if reference else None,
        hedge_index_px=100.0 if reference else None,
        entropy_reference_age_sec=e_age if reference else None,
        hedge_reference_age_sec=h_age if reference else None,
        reference_skew_sec=skew if reference else None,
        books_ready=books_ready,
        entropy_fee_bps=0.9,
        hedge_fee_bps=0.0,
        take_fraction=0.5,
        entry_cap_notional=entry_cap_notional,
        min_base=0.01,
        min_notional=10.0,
        size_step=0.01,
    )


def active_campaign(**overrides):
    values = {
        "campaign_id": "active",
        "mode": "shadow",
        "identity": MarketIdentity(
            "ANTH", "io", "ANTHROPIC", "lighter-rh"),
        "direction": "buy_entropy",
        "opened_at": 0.0,
        "qty": 1.0,
        "entropy_avg_px": 100.0,
        "hedge_avg_px": 101.0,
        "frozen_model": ready_snapshot(),
        "entry_boundary_bps": -20.0,
        "exit_target_bps": 2.5,
        "fees_usd": 0.09,
        "realized_pnl_usd": -0.09,
    }
    values.update(overrides)
    return PositionCampaign(**values)


def decide(market, *, campaign=None, snapshot=None, now=100.0):
    return decision_strategy().decide(
        market=market,
        model=ready_snapshot() if snapshot is None else snapshot,
        campaign=campaign,
        now_wall=now,
        now_mono=now,
    )


def test_ready_model_opens_sell_entropy_at_upper_quantile():
    result = decide(decision_market(sell_residual=45.0))

    assert result.intent == "OPEN"
    assert result.direction == "sell_entropy"
    assert result.entry_boundary_bps == 40.0
    assert result.exit_target_bps == pytest.approx(17.5)
    assert result.plan.projected_net_bps >= 2.0


def test_ready_model_opens_buy_entropy_at_lower_quantile():
    result = decide(decision_market(buy_residual=-25.0))

    assert result.intent == "OPEN"
    assert result.direction == "buy_entropy"
    assert result.entry_boundary_bps == -20.0
    assert result.exit_target_bps == pytest.approx(2.5)


def test_entry_decision_records_final_marginal_convergence():
    market = decision_market(
        entropy_bid=100.5,
        entropy_ask=100.51,
        hedge_bid=99.9,
        hedge_ask=100.0,
        entry_cap_notional=1000.0,
    )
    market = replace(
        market,
        entropy_book=decision_book(
            bids=[(100.5, 1.0), (100.45, 10.0)],
            asks=[(100.51, 20.0)],
        ),
    )

    result = decide(market)

    assert result.intent == "OPEN"
    assert result.plan.qty == pytest.approx(5.5)
    assert result.plan.convergence_bps == pytest.approx(27.5)
    assert result.top_convergence_bps == pytest.approx(32.5)
    assert result.convergence_bps == pytest.approx(
        result.plan.convergence_bps)
    assert result.top_convergence_bps != result.convergence_bps


@pytest.mark.parametrize(
    ("market", "snapshot", "reason"),
    [
        (decision_market(sell_residual=45.0),
         ready_snapshot(status="MODEL_NOT_READY", samples=119),
         "MODEL_NOT_READY"),
        (decision_market(sell_residual=45.0),
         ready_snapshot(status="REGIME_UNSTABLE"),
         "REGIME_UNSTABLE"),
        (decision_market(sell_residual=45.0, reference=False),
         ready_snapshot(), "REFERENCE_INCOMPLETE"),
        (decision_market(sell_residual=45.0, e_age=15.001),
         ready_snapshot(), "REFERENCE_STALE"),
        (decision_market(sell_residual=45.0, skew=15.001),
         ready_snapshot(), "REFERENCE_SKEW"),
        (decision_market(sell_residual=45.0, books_ready=False),
         ready_snapshot(), "BOOK_NOT_READY"),
    ],
)
def test_new_risk_fails_closed(market, snapshot, reason):
    assert decide(market, snapshot=snapshot).reason == reason


def test_active_campaign_adds_only_at_frozen_boundary():
    campaign = active_campaign()

    missed = decide(
        decision_market(buy_residual=-19.0), campaign=campaign)
    reached = decide(
        decision_market(buy_residual=-25.0), campaign=campaign)

    assert missed.intent == "SKIP"
    assert missed.reason == "ENTRY_NOT_REACHED"
    assert reached.intent == "ADD"
    assert reached.direction == "buy_entropy"


def test_new_risk_stops_cleanly_when_position_cap_is_exhausted():
    result = decide(decision_market(
        sell_residual=45.0, entry_cap_notional=0.0))

    assert result.intent == "SKIP"
    assert result.reason == "POSITION_CAP_REACHED"


def test_active_campaign_uses_opposite_signal_only_to_close():
    result = decide(
        decision_market(sell_residual=45.0),
        campaign=active_campaign(direction="buy_entropy"),
    )

    assert result.intent == "CLOSE"
    assert result.direction == "sell_entropy"
    assert result.plan.qty <= 1.0


def test_normal_close_has_priority_over_new_entries():
    campaign = active_campaign(
        direction="sell_entropy",
        entropy_avg_px=101.0,
        hedge_avg_px=100.0,
        entry_boundary_bps=40.0,
        exit_target_bps=17.5,
    )

    result = decide(
        decision_market(entropy_bid=99.99, entropy_ask=100.0,
                        hedge_bid=100.0, hedge_ask=100.01),
        campaign=campaign,
    )

    assert result.intent == "CLOSE"
    assert result.direction == "buy_entropy"


def test_soft_exit_closes_at_nonnegative_estimated_campaign_pnl():
    campaign = active_campaign(
        direction="sell_entropy",
        entropy_avg_px=102.0,
        hedge_avg_px=100.0,
        entry_boundary_bps=40.0,
        exit_target_bps=-50.0,
    )
    market = decision_market(
        entropy_bid=99.99,
        entropy_ask=100.0,
        hedge_bid=100.0,
        hedge_ask=100.01,
    )

    before_soft = decide(market, campaign=campaign, now=3599.0)
    at_soft = decide(market, campaign=campaign, now=3600.0)

    assert before_soft.intent == "SKIP"
    assert at_soft.intent == "CLOSE"
    assert at_soft.reason == "SOFT_EXIT_NONNEGATIVE"


def test_hard_exit_uses_fresh_books_even_without_reference():
    result = decide(
        decision_market(reference=False),
        campaign=active_campaign(),
        now=21600.0,
    )

    assert result.intent == "FORCED_CLOSE"
    assert result.plan.qty == 1.0


def test_reference_failure_blocks_add_but_not_hard_exit():
    result = decide(
        decision_market(buy_residual=-25.0, reference=False),
        campaign=active_campaign(),
        now=100.0,
    )

    assert result.intent == "SKIP"
    assert result.reason == "REFERENCE_INCOMPLETE"

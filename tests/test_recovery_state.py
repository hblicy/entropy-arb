import json
from dataclasses import replace

import pytest

from entropy_arb.campaign import PositionCampaign
from entropy_arb.recovery_state import (
    PendingAuditContext,
    PendingExecutionState,
    PendingExecutionStateError,
    PendingExecutionStore,
    PendingLegState,
    pending_execution_path,
)
from entropy_arb.strategy import MarketIdentity, ModelSnapshot


def model_snapshot():
    return ModelSnapshot(
        version=1, minute=100, samples=50, status="READY",
        median_bps=0.0, lower_bps=-5.0, q25_bps=-2.0,
        q75_bps=2.0, upper_bps=5.0)


def campaign_state():
    return PositionCampaign(
        campaign_id="campaign-1", mode="live",
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        direction="sell_entropy", opened_at=1000.0, qty=1.0,
        entropy_avg_px=100.1, hedge_avg_px=100.0,
        frozen_model=model_snapshot(), entry_boundary_bps=5.0,
        exit_target_bps=0.0, fees_usd=0.02,
        realized_pnl_usd=-0.02)


def audit_context():
    return PendingAuditContext(
        reason="exit target reached",
        signed_residual_bps=-1.5,
        reference_basis_bps=0.25,
        convergence_bps=1.75,
        round_trip_fee_bps=0.9,
        buy_slippage_budget_bps=1.0,
        sell_slippage_budget_bps=1.2,
        projected_net_bps=0.85,
        projected_net_usd=0.085,
        estimated_campaign_pnl_usd=-0.01,
        entropy_reference_age_ms=15.0,
        hedge_reference_age_ms=20.0,
        reference_update_skew_ms=5.0,
        net_funding_bps_per_hour=-0.03,
        planned_notional_usd=100.0,
    )


def pending_state():
    return PendingExecutionState(
        execution_id="exec-1",
        identity=MarketIdentity("ANTH", "io", "ANTHROPIC", "lighter-rh"),
        intent="CLOSE",
        direction="buy_entropy",
        campaign_id="campaign-1",
        qty=1.0,
        decided_at=1010.0,
        frozen_model=model_snapshot(),
        entry_boundary_bps=5.0,
        exit_target_bps=0.0,
        entropy_expected_px=100.0,
        hedge_expected_px=100.1,
        entropy_fee_bps=0.3,
        hedge_fee_bps=0.6,
        campaign_before=campaign_state(),
        buy=PendingLegState(
            venue_key="entropy", is_buy=True, order_ref="buy-1",
            status="timeout", filled_base=0.2, avg_px=100.0,
            applied_fill=0.2, unresolved=True),
        sell=PendingLegState(
            venue_key="hedge", is_buy=False, order_ref="sell-1",
            status="filled", filled_base=1.0, avg_px=100.1,
            applied_fill=1.0, unresolved=False),
        audit=audit_context(),
        settled_at=None,
        audit_ok=True,
        campaign_applied=False,
    )


def test_pending_store_round_trips_without_secrets(tmp_path):
    path = tmp_path / "campaign.pending.json"
    store = PendingExecutionStore(path)
    expected = pending_state()

    store.save(expected)

    assert store.load() == expected
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == 3
    payload = path.read_text(encoding="utf-8").lower()
    assert "private_key" not in payload
    assert "api_private_key" not in payload
    assert "token" not in payload


def test_pending_open_requires_durable_campaign_identity():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="campaign_id"):
        PendingExecutionState(
            **{
                **state.__dict__,
                "intent": "OPEN",
                "campaign_id": None,
                "campaign_before": None,
            }
        )


def test_pending_store_rejects_v2_without_modifying_original_file(tmp_path):
    path = tmp_path / "campaign.pending.json"
    original = json.dumps({
        "schema_version": 2,
        "pending_execution": None,
    }, indent=2).encode()
    path.write_bytes(original)

    with pytest.raises(
            PendingExecutionStateError,
            match=r"campaign\.pending\.json.*2.*manual verification required"):
        PendingExecutionStore(path).load()

    assert path.read_bytes() == original


def test_pending_store_rejects_unknown_schema_version_with_path(tmp_path):
    path = tmp_path / "campaign.pending.json"
    path.write_text(json.dumps({
        "schema_version": 999,
        "pending_execution": None,
    }), encoding="utf-8")

    with pytest.raises(
            PendingExecutionStateError,
            match=r"campaign\.pending\.json.*999.*manual verification required"):
        PendingExecutionStore(path).load()


def test_pending_store_rejects_applied_fill_above_observed_fill(tmp_path):
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="applied_fill"):
        PendingLegState(
            venue_key=state.buy.venue_key,
            is_buy=True,
            order_ref=state.buy.order_ref,
            status=state.buy.status,
            filled_base=0.1,
            avg_px=state.buy.avg_px,
            applied_fill=0.2,
            unresolved=True,
        )


def test_pending_store_can_atomically_clear(tmp_path):
    path = tmp_path / "campaign.pending.json"
    store = PendingExecutionStore(path)
    store.save(pending_state())

    store.save(None)

    assert store.load() is None
    assert not list(tmp_path.glob("*.tmp"))


def test_pending_path_is_derived_without_colliding_with_campaign_file(tmp_path):
    campaign = tmp_path / "campaign-state.json"

    pending = pending_execution_path(campaign)

    assert pending == tmp_path / "campaign-state.pending.json"
    assert pending != campaign


def test_pending_rejects_unknown_venue():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="venue"):
        replace(state, buy=replace(state.buy, venue_key="unknown"))


def test_pending_terminal_legs_require_settled_at():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="settled_at"):
        replace(
            state,
            buy=replace(state.buy, status="filled", filled_base=1.0,
                        avg_px=100.0, applied_fill=1.0,
                        unresolved=False),
            settled_at=None,
        )


def test_pending_unresolved_leg_rejects_settled_at():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="settled_at"):
        replace(state, settled_at=state.decided_at + 1.0)


def test_pending_rejects_fill_above_planned_qty():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="qty"):
        replace(
            state,
            buy=replace(state.buy, filled_base=state.qty + 2e-12,
                        avg_px=100.0),
        )


def test_pending_rejects_leg_venues_inconsistent_with_direction():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="direction"):
        replace(
            state,
            direction="sell_entropy",
            buy=replace(state.buy, venue_key="entropy"),
            sell=replace(state.sell, venue_key="hedge"),
        )


def test_pending_close_direction_must_reverse_campaign():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="opposite"):
        replace(
            state,
            direction=state.campaign_before.direction,
            buy=replace(state.buy, venue_key="hedge"),
            sell=replace(state.sell, venue_key="entropy"),
        )


def test_pending_terminal_state_accepts_settled_at_after_decision():
    state = pending_state()

    terminal = replace(
        state,
        buy=replace(state.buy, status="filled", filled_base=1.0,
                    avg_px=100.0, applied_fill=1.0, unresolved=False),
        settled_at=state.decided_at + 1.0,
    )

    assert terminal.settled_at == state.decided_at + 1.0


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_pending_loader_requires_exact_audit_fields(tmp_path, mutation):
    path = tmp_path / "campaign.pending.json"
    store = PendingExecutionStore(path)
    store.save(pending_state())
    payload = json.loads(path.read_text(encoding="utf-8"))
    audit = payload["pending_execution"]["audit"]
    if mutation == "missing":
        audit.pop("reason")
    else:
        audit["unexpected"] = None
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PendingExecutionStateError, match="audit fields"):
        store.load()


@pytest.mark.parametrize("field", [
    "signed_residual_bps",
    "reference_basis_bps",
    "projected_net_bps",
    "projected_net_usd",
    "estimated_campaign_pnl_usd",
    "net_funding_bps_per_hour",
])
def test_pending_audit_rejects_nonfinite_signed_values(field):
    with pytest.raises(PendingExecutionStateError, match=field):
        replace(audit_context(), **{field: float("inf")})


@pytest.mark.parametrize("field", [
    "convergence_bps",
    "round_trip_fee_bps",
    "buy_slippage_budget_bps",
    "sell_slippage_budget_bps",
    "entropy_reference_age_ms",
    "hedge_reference_age_ms",
    "reference_update_skew_ms",
])
def test_pending_audit_rejects_negative_nonnegative_values(field):
    with pytest.raises(PendingExecutionStateError, match=field):
        replace(audit_context(), **{field: -0.01})


@pytest.mark.parametrize("changes", [
    {"reason": " "},
    {"planned_notional_usd": 0.0},
    {"planned_notional_usd": float("nan")},
])
def test_pending_audit_rejects_invalid_required_values(changes):
    with pytest.raises(PendingExecutionStateError):
        replace(audit_context(), **changes)


def test_pending_add_direction_must_match_campaign():
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="ADD direction"):
        replace(state, intent="ADD")


@pytest.mark.parametrize("settled_at", [-1.0, float("inf"), 1009.0])
def test_pending_rejects_invalid_settled_at(settled_at):
    state = pending_state()

    with pytest.raises(PendingExecutionStateError, match="settled_at"):
        replace(
            state,
            buy=replace(state.buy, status="filled", filled_base=1.0,
                        avg_px=100.0, applied_fill=1.0,
                        unresolved=False),
            settled_at=settled_at,
        )


@pytest.mark.parametrize(
    "field", ["planned_notional_usd", "signed_residual_bps"])
def test_pending_audit_rejects_integer_too_large_for_float(field):
    with pytest.raises(PendingExecutionStateError, match=field):
        replace(audit_context(), **{field: 10 ** 400})


def test_pending_loader_wraps_integer_too_large_for_float(tmp_path):
    path = tmp_path / "campaign.pending.json"
    store = PendingExecutionStore(path)
    store.save(pending_state())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["pending_execution"]["audit"][
        "signed_residual_bps"] = 10 ** 400
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(
            PendingExecutionStateError, match="signed_residual_bps"):
        store.load()


def test_pending_execution_requires_explicit_audit_context():
    values = dict(pending_state().__dict__)
    values.pop("audit")

    with pytest.raises(TypeError, match="audit"):
        PendingExecutionState(**values)


@pytest.mark.parametrize("intent", ["CLOSE", "FORCED_CLOSE"])
@pytest.mark.parametrize("model", [
    ModelSnapshot(
        version=2, minute=101, samples=50, status="REGIME_UNSTABLE",
        median_bps=0.0, lower_bps=-5.0, q25_bps=-2.0,
        q75_bps=2.0, upper_bps=5.0),
    ModelSnapshot(
        version=0, minute=101, samples=0, status="MODEL_NOT_READY",
        median_bps=None, lower_bps=None, q25_bps=None,
        q75_bps=None, upper_bps=None),
])
def test_pending_close_accepts_valid_nonready_decision_model(intent, model):
    state = replace(pending_state(), intent=intent, frozen_model=model)

    assert state.intent == intent
    assert state.frozen_model == model


@pytest.mark.parametrize(
    "status", ["READY", "REGIME_UNSTABLE", "MODEL_NOT_READY"])
def test_pending_close_rejects_sampled_model_with_zero_version(status):
    model = ModelSnapshot(
        version=0, minute=101, samples=1, status=status,
        median_bps=0.0, lower_bps=-5.0, q25_bps=-2.0,
        q75_bps=2.0, upper_bps=5.0)

    with pytest.raises(
            PendingExecutionStateError,
            match="frozen_model.version must be a valid integer"):
        replace(pending_state(), frozen_model=model)


@pytest.mark.parametrize("intent", ["OPEN", "ADD"])
def test_pending_entry_still_rejects_nonready_decision_model(intent):
    state = pending_state()
    changes = {
        "intent": intent,
        "frozen_model": ModelSnapshot(
            version=0, minute=101, samples=0, status="MODEL_NOT_READY",
            median_bps=None, lower_bps=None, q25_bps=None,
            q75_bps=None, upper_bps=None),
    }
    if intent == "OPEN":
        changes.update(campaign_id="new-campaign", campaign_before=None)
    else:
        changes.update(
            direction="sell_entropy",
            buy=replace(state.buy, venue_key="hedge"),
            sell=replace(state.sell, venue_key="entropy"),
        )

    with pytest.raises(PendingExecutionStateError, match="ready"):
        replace(state, **changes)

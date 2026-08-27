import os
import tempfile
from types import SimpleNamespace

from entropy_arb.config import load_config
from entropy_arb.models import OrderResult
from entropy_arb.venue_hl import HLVenue, NonceAllocator
from entropy_arb.venue_lighter import LighterVenue
from entropy_arb.venues.base import VenueAdapter


NO_ENV = os.path.join(tempfile.gettempdir(), "entropy-arb-no-such.env")
MINIMAL = """
thresholds:
  midline_bps: 0
  upper_bps: 4
  lower_bps: 4
"""


def make_cfg(hedge):
    f = tempfile.NamedTemporaryFile(
        "w", suffix=".yaml", delete=False, encoding="utf-8")
    f.write(MINIMAL)
    f.close()
    return load_config(
        f.name, NO_ENV, symbol="SNDK", hedge_venue=hedge)


def test_existing_venues_satisfy_runtime_protocol():
    hl_cfg = make_cfg("tradexyz")
    lighter_cfg = make_cfg("lighter")
    hl = HLVenue(hl_cfg.entropy, "https://api", "wss://ws", object(), 5.0)
    lighter = LighterVenue(lighter_cfg.hedge, object(), 5.0)
    assert isinstance(hl, VenueAdapter)
    assert isinstance(lighter, VenueAdapter)


def test_hl_parse_returns_typed_fill():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"filled": {"totalSz": "0.5", "avgPx": "100.25"}}
    ]}}}
    result = HLVenue._parse(body)
    assert result == OrderResult(
        status="filled", filled_base=0.5, avg_px=100.25)


def test_hl_parse_marks_resting_ioc_as_unknown():
    body = {"status": "ok", "response": {"data": {"statuses": [
        {"resting": {"oid": 1}}
    ]}}}
    result = HLVenue._parse(body)
    assert result.status == "resting?"
    assert result.unresolved is True


def test_hl_peer_setup_shares_nonce_and_deduplicates_equity():
    cfg = make_cfg("tradexyz")
    anchor = HLVenue(cfg.entropy, "https://api", "wss://ws", object(), 5.0)
    hedge = HLVenue(cfg.hedge, "https://api", "wss://ws", object(), 5.0)
    anchor.account = SimpleNamespace(
        wallet=SimpleNamespace(address="0xsigner"),
        query_address="0xaccount",
        nonces=NonceAllocator())
    hedge.account = SimpleNamespace(
        wallet=SimpleNamespace(address="0xsigner"),
        query_address="0xaccount",
        nonces=NonceAllocator())

    anchor.configure_peer(hedge)

    assert hedge.account.nonces is anchor.account.nonces
    assert hedge.include_core_equity is False

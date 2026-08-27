import os
import tempfile
from dataclasses import replace

import pytest

from entropy_arb.config import load_config
from entropy_arb.venue_hl import HLVenue
from entropy_arb.venue_lighter import LighterVenue
from entropy_arb.venues.registry import VenueRuntime, create_venue


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


def runtime():
    return VenueRuntime(
        session=object(),
        hl_api_url="https://api",
        hl_ws_url="wss://ws",
        settle_timeout_sec=5.0,
    )


def test_registry_creates_hyperliquid_adapter():
    cfg = make_cfg("tradexyz")
    assert isinstance(create_venue(cfg.entropy, runtime()), HLVenue)


def test_registry_creates_lighter_adapter():
    cfg = make_cfg("lighter")
    assert isinstance(create_venue(cfg.hedge, runtime()), LighterVenue)


def test_registry_rejects_unknown_kind():
    cfg = make_cfg("lighter")
    unknown = replace(cfg.hedge, kind="unknown")
    with pytest.raises(ValueError, match="unsupported venue kind 'unknown'"):
        create_venue(unknown, runtime())

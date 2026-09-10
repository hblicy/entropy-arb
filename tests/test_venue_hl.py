from entropy_arb.config import VenueConf
from entropy_arb.reference import ReferenceState
from entropy_arb.venue_hl import HLVenue


def test_hl_venue_owns_reference_state():
    conf = VenueConf(
        key="entropy", kind="hl", label="ENTROPY", symbol="ANTH",
        fee_bps=0.9, cap_usd=1000.0, orders_per_min=120, hl_dex="io")

    venue = HLVenue(conf, "https://api", "wss://ws", object(), 5.0)

    assert isinstance(venue.reference, ReferenceState)

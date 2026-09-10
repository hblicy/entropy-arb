from entropy_arb.config import LighterProfile, VenueConf
from entropy_arb.reference import ReferenceState
from entropy_arb.venue_lighter import LighterVenue


def test_lighter_venue_owns_reference_state():
    profile = LighterProfile("robinhood", "https://api", "wss://ws", 1)
    conf = VenueConf(
        key="hedge", kind="lighter", label="RH", symbol="ANTHROPIC",
        fee_bps=0.0, cap_usd=1000.0, orders_per_min=30,
        lighter_profile=profile)

    venue = LighterVenue(conf, object(), 5.0)

    assert isinstance(venue.reference, ReferenceState)

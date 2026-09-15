from entropy_arb.runtime_paths import market_scoped_path, strategy_paths
from entropy_arb.strategy import MarketIdentity


def identity(*, entropy_symbol="ANTH", entropy_dex="io",
             hedge_symbol="ANTHROPIC", hedge_venue="lighter-rh"):
    return MarketIdentity(
        entropy_symbol=entropy_symbol,
        entropy_dex=entropy_dex,
        hedge_symbol=hedge_symbol,
        hedge_venue=hedge_venue,
    )


def test_strategy_paths_are_market_and_mode_scoped(tmp_path):
    market = identity()

    live = strategy_paths(
        tmp_path / "campaign.json",
        tmp_path / "events.csv",
        market,
        shadow=False,
    )
    shadow = strategy_paths(
        tmp_path / "campaign.json",
        tmp_path / "events.csv",
        market,
        shadow=True,
    )

    assert live.campaign != live.pending
    assert live.campaign != shadow.campaign
    assert live.events != shadow.events
    assert live.pending is not None
    assert shadow.pending is None
    assert live.campaign.name.endswith(".json")
    assert live.pending.name.endswith(".pending.json")
    assert shadow.campaign.name.endswith(".shadow.json")
    assert shadow.events.name.endswith(".shadow.csv")
    assert "ANTH" in live.campaign.name
    assert "ANTHROPIC" in live.campaign.name


def test_strategy_paths_preserve_market_tag_for_extensionless_files(tmp_path):
    market = identity()
    campaign = tmp_path / "state"
    events = tmp_path / "events"
    scoped_campaign = market_scoped_path(campaign, market)
    scoped_events = market_scoped_path(events, market)
    market_tag = scoped_campaign.name.removeprefix("state.")

    live = strategy_paths(campaign, events, market, shadow=False)
    shadow = strategy_paths(campaign, events, market, shadow=True)

    assert live.campaign.name == f"state.{market_tag}"
    assert live.pending.name == f"state.{market_tag}.pending.json"
    assert shadow.campaign.name == f"state.{market_tag}.shadow"
    assert live.events.name == f"events.{market_tag}"
    assert shadow.events.name == f"events.{market_tag}.shadow"
    assert len({live.campaign, live.pending, shadow.campaign}) == 3
    assert live.events != shadow.events


def test_market_scoped_path_is_deterministic_and_identity_specific(tmp_path):
    configured = tmp_path / "campaign.json"
    first = market_scoped_path(configured, identity())
    same = market_scoped_path(configured, identity())
    other = market_scoped_path(
        configured,
        identity(hedge_symbol="SNDK"),
    )

    assert first == same
    assert first != other


def test_market_scoped_path_sanitizes_readable_name_without_hash_collision(
        tmp_path):
    configured = tmp_path / "campaign.json"

    slash = market_scoped_path(
        configured,
        identity(entropy_symbol="ANTH/USDC"),
    )
    colon = market_scoped_path(
        configured,
        identity(entropy_symbol="ANTH:USDC"),
    )

    assert "/" not in slash.name
    assert ":" not in colon.name
    assert slash != colon


def test_strategy_paths_report_legacy_paths(tmp_path):
    campaign = tmp_path / "campaign.json"
    events = tmp_path / "events.csv"

    live = strategy_paths(campaign, events, identity(), shadow=False)
    shadow = strategy_paths(campaign, events, identity(), shadow=True)

    assert live.legacy_campaign == campaign
    assert live.legacy_pending == tmp_path / "campaign.pending.json"
    assert shadow.legacy_campaign == tmp_path / "campaign.shadow.json"
    assert shadow.legacy_pending is None

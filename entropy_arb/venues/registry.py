"""Explicit venue factory registry; configuration cannot import code."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

import aiohttp

from ..config import VenueConf
from ..venue_hl import HLVenue
from ..venue_lighter import LighterVenue
from .base import VenueAdapter


@dataclass(frozen=True)
class VenueRuntime:
    session: aiohttp.ClientSession
    hl_api_url: str
    hl_ws_url: str
    settle_timeout_sec: float


VenueFactory = Callable[[VenueConf, VenueRuntime], VenueAdapter]


def _create_hl(conf: VenueConf, runtime: VenueRuntime) -> VenueAdapter:
    return HLVenue(
        conf, runtime.hl_api_url, runtime.hl_ws_url,
        runtime.session, runtime.settle_timeout_sec)


def _create_lighter(conf: VenueConf, runtime: VenueRuntime) -> VenueAdapter:
    return LighterVenue(
        conf, runtime.session, runtime.settle_timeout_sec)


ADAPTER_FACTORIES: Dict[str, VenueFactory] = {
    "hl": _create_hl,
    "lighter": _create_lighter,
}


def create_venue(conf: VenueConf, runtime: VenueRuntime) -> VenueAdapter:
    try:
        factory = ADAPTER_FACTORIES[conf.kind]
    except KeyError as exc:
        raise ValueError(
            f"unsupported venue kind {conf.kind!r}") from exc
    venue = factory(conf, runtime)
    if not isinstance(venue, VenueAdapter):
        raise TypeError(
            f"adapter {type(venue).__name__} does not satisfy VenueAdapter")
    return venue

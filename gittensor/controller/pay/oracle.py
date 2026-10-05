# The MIT License (MIT)
# Copyright © 2025 Entrius

"""Prices for turning USD pay targets into alpha (vault ``23`` §7a, §7b "price-oracle failure must fail safe").

A ``PriceOracle`` answers two questions: USD per TAO and TAO per alpha. ``StaticOracle`` answers from config;
``CoinGeckoChainOracle`` (the default) asks CoinGecko for TAO/USD, as phase 0's serving pricing did, and the chain
for alpha/TAO (the subnet pool price, read-only); ``MetagraphedOracle`` reads metagraphed's REST API. The ledger never talks to either directly: it asks a
``FailSafeOracle`` for a ``Quote``, which

* holds the last good price when a read fails, is not a positive finite number, or the source says it is stale;
* refuses a read more than ``max_move`` x away from the last good price, until ``confirm_reads`` reads in a row agree
  with each other (a real move is accepted a few reads late; a single wild read never is);
* before any good read, answers with the static fallback, priced so the pool undersizes rather than overpays;

so the price is never zero and never wild, and ``held`` says when it is not fresh.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from gittensor.controller.checks import config as cfg


class OracleError(Exception):
    """No usable price from this source right now."""


class PriceOracle(Protocol):
    def tao_usd(self) -> float: ...

    def alpha_tao(self) -> float: ...


@dataclass(frozen=True)
class Quote:
    tao_usd: float
    alpha_tao: float
    at: float  # when the older of the two prices was last read good (or the quote time, for the static fallback)
    source: str
    held: bool = False
    notes: tuple[str, ...] = ()

    @property
    def alpha_usd(self) -> float:
        return self.tao_usd * self.alpha_tao

    def as_dict(self) -> dict:
        return {
            'tao_usd': self.tao_usd,
            'alpha_tao': self.alpha_tao,
            'alpha_usd': self.alpha_usd,
            'at': self.at,
            'source': self.source,
            'held': self.held,
            'notes': list(self.notes),
        }


@dataclass(frozen=True)
class StaticOracle:
    tao_usd_value: float = cfg.STATIC_TAO_USD
    alpha_tao_value: float = cfg.STATIC_ALPHA_TAO

    def tao_usd(self) -> float:
        return self.tao_usd_value

    def alpha_tao(self) -> float:
        return self.alpha_tao_value


class MetagraphedOracle:
    """metagraphed's REST API. ``/api/v1/network/tao-usd`` (a composed on-chain wTAO median; ``latest.usd_per_tao`` is
    null when its pool quorum is not met, and ``stale`` marks an old point) and the subnet's economics
    (``economics.alpha_price_tao``). The TAO/USD path is the one metagraphed documents; the economics path mirrors its
    ``get_subnet_economics`` tool and is unverified against the live host (9/15)."""

    TAO_USD_PATH = '/api/v1/network/tao-usd'
    ECONOMICS_PATH = '/api/v1/subnets/{netuid}/economics'

    def __init__(
        self,
        base_url: str,
        netuid: int = cfg.NETUID,
        get: Callable[..., Any] | None = None,
        timeout_s: float = 10.0,
    ):
        if not base_url:
            raise ValueError('MetagraphedOracle needs a base URL')
        self.base_url, self.netuid, self.timeout_s = base_url.rstrip('/'), netuid, timeout_s
        if get is None:
            import requests

            get = requests.get
        self._get = get

    def _json(self, path: str) -> dict:
        try:
            response = self._get(self.base_url + path, timeout=self.timeout_s)
            response.raise_for_status()
            doc = response.json()
        except Exception as e:
            raise OracleError(f'{path}: {type(e).__name__}: {e}'[:300]) from e
        if not isinstance(doc, dict):
            raise OracleError(f'{path}: not a JSON object')
        return doc

    def tao_usd(self) -> float:
        doc = self._json(self.TAO_USD_PATH)
        latest = doc.get('latest') or {}
        if doc.get('stale'):
            raise OracleError(f'tao-usd is stale ({doc.get("age_ms")} ms old)')
        if latest.get('usd_per_tao') is None:
            raise OracleError(f'tao-usd not priceable ({latest.get("price_basis")})')
        return float(latest['usd_per_tao'])

    def alpha_tao(self) -> float:
        doc = self._json(self.ECONOMICS_PATH.format(netuid=self.netuid))
        economics = doc.get('economics') or {}
        if economics.get('alpha_price_tao') is None:
            raise OracleError(f'subnet {self.netuid} economics carry no alpha_price_tao')
        return float(economics['alpha_price_tao'])


class CoinGeckoChainOracle:
    """TAO/USD from CoinGecko's free ``simple/price`` endpoint (the same URL phase 0 used) and alpha/TAO from the
    subnet's own pool price on chain. Both injectable for tests: ``get`` (a ``requests.get``-alike) and
    ``price_reader`` (a callable returning TAO per alpha as a float)."""

    def __init__(
        self,
        endpoint: str,
        netuid: int = cfg.NETUID,
        url: str = cfg.COINGECKO_TAO_USD_URL,
        get: Callable[..., Any] | None = None,
        price_reader: Callable[[], float] | None = None,
        timeout_s: float = 10.0,
    ):
        self.endpoint, self.netuid, self.url, self.timeout_s = endpoint, netuid, url, timeout_s
        if get is None:
            import requests

            get = requests.get
        self._get = get
        self._price_reader = price_reader
        self._subtensor = None

    def tao_usd(self) -> float:
        try:
            response = self._get(self.url, timeout=self.timeout_s)
            response.raise_for_status()
            doc = response.json()
        except Exception as e:
            raise OracleError(f'coingecko: {type(e).__name__}: {e}'[:300]) from e
        try:
            return float(doc['bittensor']['usd'])
        except (KeyError, TypeError, ValueError) as e:
            raise OracleError(f'coingecko: unexpected body {str(doc)[:120]!r}') from e

    def alpha_tao(self) -> float:
        if self._price_reader is not None:
            return float(self._price_reader())
        try:
            import bittensor as bt

            if self._subtensor is None:
                self._subtensor = bt.Subtensor(network=self.endpoint)
            info = self._subtensor.subnet(self.netuid)
            price: Any = getattr(info, 'price', None)
            value = float(getattr(price, 'tao', price))
        except Exception as e:
            self._subtensor = None  # reconnect next time
            raise OracleError(
                f'chain pool price ({self.endpoint}, netuid {self.netuid}): {type(e).__name__}: {e}'[:300]
            ) from e
        return value


@dataclass
class _Guard:
    """One price's last good value and the refused reads since."""

    max_move: float
    confirm_reads: int
    last: float | None = None
    at: float | None = None
    pending: list[float] = field(default_factory=list)

    def offer(self, value: float, now: float) -> str:
        """Take a read. Returns '' when it became the price, else why the last good price is held."""
        if not math.isfinite(value) or value <= 0:
            return f'unusable read {value!r}'
        if self.last is None or self.last / self.max_move <= value <= self.last * self.max_move:
            self.last, self.at, self.pending = value, now, []
            return ''
        self.pending.append(value)
        recent = self.pending[-self.confirm_reads :]
        if len(recent) >= self.confirm_reads and max(recent) / min(recent) <= self.max_move:
            self.last, self.at, self.pending = value, now, []
            return ''
        return f'refused a move to {value:g} from {self.last:g} ({len(self.pending)}/{self.confirm_reads} reads)'


class FailSafeOracle:
    def __init__(
        self,
        inner: PriceOracle,
        fallback: PriceOracle | None = None,
        refresh_s: float = cfg.ORACLE_REFRESH_S,
        max_move: float = cfg.ORACLE_MAX_MOVE,
        confirm_reads: int = cfg.ORACLE_CONFIRM_READS,
        clock: Callable[[], float] = time.time,
    ):
        self.inner, self.fallback = inner, fallback or StaticOracle()
        self.refresh_s, self.clock = refresh_s, clock
        self._tao = _Guard(max_move, confirm_reads)
        self._alpha = _Guard(max_move, confirm_reads)
        self._cached: Quote | None = None
        self._cached_read_at = 0.0

    def quote(self, force: bool = False) -> Quote:
        now = self.clock()
        if not force and self._cached is not None and now - self._cached_read_at < self.refresh_s:
            return self._cached
        notes: list[str] = []
        prices = {}
        for name, guard, read, fallback in (
            ('tao_usd', self._tao, self.inner.tao_usd, self.fallback.tao_usd),
            ('alpha_tao', self._alpha, self.inner.alpha_tao, self.fallback.alpha_tao),
        ):
            try:
                why = guard.offer(float(read()), now)
            except (OracleError, TypeError, ValueError) as e:
                why = f'{type(e).__name__}: {e}'[:300]
            if why:
                notes.append(f'{name}: {why}' + ('' if guard.last is not None else ': static fallback'))
            prices[name] = guard.last if guard.last is not None else float(fallback())
        ats = [g.at for g in (self._tao, self._alpha) if g.at is not None]
        at = min(ats) if len(ats) == 2 else now
        source = type(self.inner).__name__ if len(ats) == 2 else f'{type(self.inner).__name__}+static'
        self._cached = Quote(prices['tao_usd'], prices['alpha_tao'], at, source, bool(notes), tuple(notes))
        self._cached_read_at = now
        return self._cached

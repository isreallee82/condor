"""Compute the FlowEdge regime signal and a ready-to-submit DCA ladder."""

import asyncio
import json
import logging
import math
import time
from typing import Any

import aiohttp
import numpy as np
import pandas as pd
from config_manager import get_client, get_config_manager
from pydantic import BaseModel, ConfigDict, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"
ADX_LENGTH = 14

# Cap on every advisory market-data probe. The shared client's own ClientTimeout
# is 60s total, so two uncapped probes against an unreachable venue cost a 60s
# tick 120s+ of dead waiting — the agent then ticks at a third of its configured
# rate. Ten seconds is far more than a healthy venue needs and bounds the loss.
PROBE_TIMEOUT_SEC = 10.0

# The order-book probe gets ONE self-heal attempt per tick, and only on a
# timeout. A pair that was never SUBSCRIBED makes get_order_book hang to the
# full cap with no error at all — diagnostics on the real failure showed
# tracker_ready false, websocket_status not_connected, trading_pairs [] and all
# eight listener tasks absent — and that, not an outage, is what blocked this
# agent for its entire life. add_trading_pair is idempotent and market-data
# only: it starts a feed, it places nothing. (restart_order_book_tracker is no
# use in that state — with nothing subscribed it answers "No trading pairs to
# restart".) SUBSCRIBE_READY_SEC is the wait the API is asked to do server-side
# for the tracker to come up; SUBSCRIBE_TIMEOUT_SEC is our own cap around the
# call and is deliberately LONGER, so the server gets to answer instead of
# being cut off mid-flight — a cancelled subscribe would tell us nothing about
# whether it worked. A tick that times out and cannot heal therefore costs
# 10 + 12 + 10 on the book and 10 on funding — bounded, and still well under the
# 120s+ that two UNCAPPED probes cost before any of this existed.
SUBSCRIBE_READY_SEC = 8
SUBSCRIBE_TIMEOUT_SEC = 12.0


class Config(BaseModel):
    """FlowEdge signal — dual-timeframe ADX regime, volatility-normalised score, sized DCA ladder."""

    # Reject unknown keys instead of silently dropping them. The caller composes
    # this config by hand from the session config, and pydantic's default
    # extra='ignore' quietly discarded leverage / risk_limits / server_name —
    # so the ladder went out at the venue's account leverage with nobody warned.
    model_config = ConfigDict(extra="forbid")

    connector_name: str = Field(default="derive_perpetual", description="Exchange connector")
    trading_pair: str = Field(default="XRP-USDC", description="Trading pair")
    candle_connector: str = Field(
        default="",
        description="Connector used to fetch candles; falls back to connector_name when empty. "
                    "Use this when the execution connector does not serve candle data (e.g. derive_perpetual).",
    )
    candle_trading_pair: str = Field(
        default="",
        description="Trading pair used for candle data; falls back to trading_pair when empty. "
                    "Use when the candle connector quotes a different asset (e.g. XRP-USDT on binance_perpetual).",
    )
    fast_interval: str = Field(default="3m", description="Signal timeframe")
    slow_interval: str = Field(default="15m", description="Regime timeframe")
    fast_max_records: int = Field(
        default=150,
        description="Fast candles to load — must match the controller's fast_max_records",
    )
    slow_max_records: int = Field(
        default=100,
        description="Slow candles to load — must match the controller's slow_max_records",
    )
    # Sizing is not this routine's to invent — it belongs to the session budget
    # (AgentConfig.total_amount_quote) and has to be passed in. The old default
    # of 100.0 sized every ladder to 100 against a 70 budget, inside a block the
    # routine itself labels "use these values verbatim", and also asked the
    # exit-liquidity probe about a notional nobody was going to trade. 0 means
    # "not supplied" and _build_ladder refuses to build. It is a zero default
    # rather than a required field only because routines/base.py's
    # get_default_config() constructs Config() with no arguments to render the
    # settings menu, and a required field would raise there.
    total_amount_quote: float = Field(
        default=0.0,
        description="Quote capital for one entry — pass the session's total_amount_quote; "
                    "no ladder is emitted while this is 0",
    )
    leverage: int = Field(
        default=1,
        description="Leverage the dca_executor is created with — pass the session's "
                    "configured leverage; it is emitted verbatim in the ladder",
    )

    signal_threshold: float = Field(default=0.30, description="Score needed to fire an entry")
    adx_trending_threshold: float = Field(default=22.0, description="ADX above this = trending")
    adx_extreme_threshold: float = Field(default=50.0, description="ADX above this = halt")
    allow_fast_regime_entry: bool = Field(
        default=True,
        description="Let the fast frame alone confirm a trend (mirrors the controller)",
    )

    cfi_weight: float = Field(default=0.35, description="Weight on candle flow imbalance")
    vwap_weight: float = Field(default=0.25, description="Weight on VWAP extension")
    trend_weight: float = Field(default=0.25, description="Weight on EMA/ATR trend")
    di_weight: float = Field(default=0.15, description="Weight on slow-frame DI bias")

    vwap_window: int = Field(default=24, description="Rolling VWAP window in bars")
    trend_ema_length: int = Field(default=21, description="EMA length for the trend term")
    rsi_length: int = Field(default=14, description="RSI length")
    rsi_overbought: float = Field(default=70.0, description="RSI overbought band")
    rsi_oversold: float = Field(default=30.0, description="RSI oversold band")

    natr_baseline_pct: float = Field(default=0.25, description="NATR%% that maps to a 1.0x multiplier")
    vol_multiplier_min: float = Field(default=0.6, description="Lower clamp on the volatility multiplier")
    vol_multiplier_max: float = Field(default=2.5, description="Upper clamp on the volatility multiplier")

    stop_loss: float = Field(default=0.010, description="Stop loss at baseline volatility")
    take_profit: float = Field(default=0.003, description="Take profit at baseline volatility")
    min_take_profit: float = Field(default=0.0015, description="Take profit floor after round-trip fees")
    time_limit: int = Field(default=900, description="Executor time limit in seconds")
    dca_spreads: list[float] = Field(
        default=[0.0015, 0.0035, 0.0065],
        description="Ladder rung distances — must match the controller's dca_spreads",
    )
    dca_amounts_pct: list[float] = Field(
        default=[0.5, 0.3, 0.2],
        description="Ladder weighting — must match the controller's dca_amounts_pct",
    )
    exit_depth_band_pct: float = Field(
        default=0.5,
        description="Price band, in %, the exit must find liquidity inside",
    )

    funding_threshold: float = Field(default=0.0005, description="Funding rate that counts as crowded")
    funding_bias_strength: float = Field(default=0.15, description="Score tilt applied to crowded funding")


# ---------------------------------------------------------------------------
# Indicators — Wilder's smoothing, matching pandas_ta semantics.
# Implemented here so the routine has no TA dependency beyond pandas/numpy.
# ---------------------------------------------------------------------------

def _rma(series: pd.Series, length: int) -> pd.Series:
    """Wilder's moving average (pandas_ta's `rma`)."""
    return series.ewm(alpha=1.0 / length, adjust=False).mean()


def _true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    return pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)


def _atr(df: pd.DataFrame, length: int = ADX_LENGTH) -> pd.Series:
    return _rma(_true_range(df), length)


def _natr_pct(df: pd.DataFrame, length: int = ADX_LENGTH) -> pd.Series:
    """Normalised ATR in percent, the same units pandas_ta reports."""
    return 100.0 * _atr(df, length) / df["close"].replace(0, np.nan)


def _rsi(close: pd.Series, length: int = 14) -> pd.Series:
    delta = close.diff()
    gain = _rma(delta.clip(lower=0.0), length)
    loss = _rma((-delta).clip(lower=0.0), length)
    rs = gain / loss.replace(0, np.nan)
    return (100.0 - 100.0 / (1.0 + rs)).fillna(50.0)


def _adx_di(df: pd.DataFrame, length: int = ADX_LENGTH) -> tuple[float, float]:
    """Return (ADX, DI bias in [-1, 1]) from the last bar, or (nan, 0.0)."""
    if len(df) < length * 2:
        return float("nan"), 0.0

    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)

    atr = _atr(df, length).replace(0, np.nan)
    plus_di = 100.0 * _rma(pd.Series(plus_dm, index=df.index), length) / atr
    minus_di = 100.0 * _rma(pd.Series(minus_dm, index=df.index), length) / atr

    di_sum = (plus_di + minus_di).replace(0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / di_sum
    adx = _rma(dx.fillna(0.0), length)

    last_adx = float(adx.iloc[-1])
    p, m = float(plus_di.iloc[-1]), float(minus_di.iloc[-1])
    di_bias = (p - m) / (p + m) if (p + m) > 0 and not math.isnan(p + m) else 0.0
    return last_adx, float(np.clip(di_bias, -1.0, 1.0))


def _classify(adx: float, trending: float, extreme: float) -> str:
    # NaN means "could not be computed" — a short, empty or wrong-shaped frame —
    # which is not the same claim as "computed, and the market is calm".
    # Returning RANGING here disabled the strategy's only risk-off filter exactly
    # when its input was missing, so unknown gets its own label and the entry
    # gate treats it as a halt.
    if math.isnan(adx):
        return "UNKNOWN"
    if adx >= extreme:
        return "EXTREME"
    if adx >= trending:
        return "TRENDING"
    return "RANGING"


# ---------------------------------------------------------------------------
# Data access
# ---------------------------------------------------------------------------

def _to_frame(result: Any) -> pd.DataFrame:
    """Normalise the several shapes the candles endpoint can return."""
    if isinstance(result, list):
        records = result
    elif isinstance(result, dict):
        records = result.get("data", result.get("candles", []))
    else:
        records = []
    if not records:
        return pd.DataFrame()
    df = pd.DataFrame(records)
    for col in ("open", "high", "low", "close", "volume"):
        if col not in df.columns:
            return pd.DataFrame()
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def _normalize_levels(raw: Any) -> list[list[float]]:
    """Coerce order-book levels to [[price, amount], ...].

    The API returns levels as 2-tuples on most connectors, as longer tuples on
    some, and as {"price": .., "amount": ..} dicts on others; the sibling
    market_making_expert/market_analyzer routine normalises the same shapes.
    Without this, a shape change raises out of the depth probe and gets reported
    with the same words as a network outage — a bug in us wearing an outage's
    clothes. Two failures raise so _probe_error can label them MALFORMED_BOOK: a
    non-numeric price inside a well-shaped level, and a level list in which NOT
    ONE entry is a shape we recognise (a flat [price, price, ...] list, say).
    Returning [] for the second used to render as EMPTY_BOOK — our own parsing
    gap wearing a dead venue's clothes, the same confusion in the other
    direction. A merely MIXED list still drops the entries it cannot read.
    """
    levels: list[list[float]] = []
    seen = 0
    unreadable = 0
    first: Any = None
    for entry in raw or []:
        seen += 1
        if seen == 1:
            first = entry
        if isinstance(entry, dict):
            price = entry.get("price", entry.get("p"))
            amount = entry.get("amount", entry.get("quantity", entry.get("size", entry.get("s"))))
        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
            price, amount = entry[0], entry[1]
        else:
            unreadable += 1
            continue
        if price is None or amount is None:
            unreadable += 1
            continue
        price, amount = float(price), float(amount)
        if amount > 0:
            levels.append([price, amount])
    if seen and unreadable == seen:
        raise TypeError(
            f"order-book levels in an unrecognised shape: {seen} entries, first is "
            f"{type(first).__name__} {first!r:.60}"
        )
    return levels


# ---------------------------------------------------------------------------
# Probe error taxonomy
#
# Every market-data probe below is advisory and must never raise into the tick,
# but "it failed" is not enough: the agent reads this text and has to decide
# whether it is looking at a thin book, a wrong pair, or a dead backend. So
# failures are classified by exception TYPE and by the HTTP status the API
# returned — NEVER by str(e). The failure mode actually occurring in production
# is the client's 60s total timeout, which raises a bare TimeoutError whose
# str() is the EMPTY STRING: a note built from str(e) alone rendered as literally
# empty parentheses, and the agent, handed no evidence, invented a root cause and
# ran off-config against another venue. type(e).__name__ is therefore always
# included, so an empty-stringifying exception can never render as "()" again.
# ---------------------------------------------------------------------------

def _probe_error(e: BaseException, cap: float = PROBE_TIMEOUT_SEC) -> tuple[str, str]:
    """Classify a probe failure as (code, human-readable detail).

    `cap` is how long this particular call was allowed to run, and it is only
    ever used to describe a timeout truthfully. Callers that do NOT wrap their
    request in asyncio.wait_for must pass the time actually spent — the default
    would otherwise report "no response within 10s" after a 60s client timeout,
    a false statement about the evidence inside the one function whose job is to
    make the evidence trustworthy.

    No code here ever means "OK". Every one of them means the observation could
    not be made, and for the exit-liquidity check an observation that could not
    be made is the same as "not safe to enter": a timeout says nothing about the
    book, and on this venue 53 of ~56 MARKET closes were refused. The executor's
    own take_profit / stop_loss / time_limit barriers are no fallback — they
    close with MARKET orders too, the exact order type that was refused.
    """
    name = type(e).__name__
    # ConnectionTimeoutError SUBCLASSES TimeoutError, so it has to be tested
    # first — otherwise "cannot reach the API host" is reported as "the venue
    # was slow", which points the agent at the wrong half of the stack.
    if isinstance(e, aiohttp.ConnectionTimeoutError):
        return "BACKEND_UNREACHABLE", f"{name}: {e}"
    if isinstance(e, (aiohttp.ClientConnectorError, aiohttp.ClientOSError,
                      aiohttp.ServerDisconnectedError)):
        return "BACKEND_UNREACHABLE", f"{name}: {e}"
    if isinstance(e, aiohttp.ClientResponseError):
        # The API answered, so the backend is alive; the status and its message
        # say whether the exchange behind it is, or whether we asked wrongly.
        message = e.message or ""
        detail = f"{name}: HTTP {e.status}: {message or '<no message>'}"
        if "Cannot connect to host" in message:
            return "VENUE_UNREACHABLE", detail
        if e.status in (400, 404, 422):
            return "BAD_REQUEST", detail
        return "API_ERROR", detail
    if isinstance(e, TimeoutError):
        # asyncio.TimeoutError is the builtin TimeoutError; str() is "" — which
        # is why the CODE above is decided by type alone and never by str(e).
        # aiohttp's read timeouts (SocketTimeoutError / ServerTimeoutError) land
        # here too and DO carry a message ("Timeout on reading data from
        # socket"); appending it is display only and throws no evidence away.
        text = str(e)
        return "TRANSPORT_TIMEOUT", (
            f"{name}: no response within {cap:g}s — the request was accepted and never answered"
            + (f" ({text})" if text else "")
        )
    if isinstance(e, (TypeError, ValueError, AttributeError, KeyError, IndexError)):
        # A defect in OUR parsing, not an outage. Kept distinct so a payload
        # shape change is never diagnosed as a dead venue.
        return "MALFORMED_BOOK", f"{name}: {e}"
    # str() only as a display fallback here — the code is already decided, and
    # the type name in front of it keeps the note non-empty either way.
    return "UNKNOWN", f"{name}: {str(e) or '<no message>'}"


def _server_label(client) -> str:
    """Best-effort name of the API server behind `client`.

    The connectivity line has to name the server, or "DEGRADED" reads as a venue
    problem rather than an infrastructure one. The client only knows its base
    URL, so ask the ConfigManager which configured server it is and fall back to
    the URL. A label is never worth breaking a tick over.
    """
    try:
        # ConfigManager._clients is private and its VALUE shape is not ours to
        # depend on (it is a (client, verified_at) tuple today). Unpack
        # defensively so a shape change costs a label, not a tick.
        for name, entry in getattr(get_config_manager(), "_clients", {}).items():
            cached = entry[0] if isinstance(entry, (tuple, list)) and entry else entry
            if cached is client:
                return name
    except Exception:  # noqa: BLE001 — cosmetic only
        pass
    return str(getattr(client, "base_url", "") or "unknown")


async def _fetch_funding(client, connector_name: str, trading_pair: str,
                         cap: float = PROBE_TIMEOUT_SEC) -> tuple[float | None, str, str]:
    """Live funding rate as (rate, code, detail).

    `code` is "OK" when the venue answered with a rate, "NO_FUNDING" when it
    answered but reports none (spot, or a perp that simply does not publish one),
    and a `_probe_error` code when the probe itself failed.

    Those three used to collapse into a bare None, printed as `funding_rate:
    None` — indistinguishable from a venue that legitimately has no funding, and
    logged at debug level so it was invisible in normal operation. That threw
    away one of the two independent live observations that the execution venue
    is unreachable, which is exactly the evidence the agent needed.
    """
    try:
        info = await asyncio.wait_for(
            client.market_data.get_funding_info(connector_name, trading_pair),
            timeout=cap,
        )
    except Exception as e:  # noqa: BLE001 — a missing funding feed is not fatal
        code, detail = _probe_error(e, cap)
        logger.warning("funding probe failed on %s %s: %s: %s",
                       connector_name, trading_pair, code, detail)
        return None, code, detail
    if not isinstance(info, dict):
        return None, "NO_FUNDING", "venue returned no funding payload"
    for key in ("rate", "funding_rate", "fundingRate", "next_funding_rate"):
        if info.get(key) is not None:
            try:
                return float(info[key]), "OK", ""
            except (TypeError, ValueError):
                return None, "NO_FUNDING", f"unparseable funding value {info[key]!r}"
    return None, "NO_FUNDING", "venue does not report a funding rate"


# ---------------------------------------------------------------------------
# Signal
# ---------------------------------------------------------------------------

def _closed_bars(df: pd.DataFrame) -> pd.DataFrame:
    """Drop the bar currently being built.

    The candles endpoint returns the feed's rolling buffer, and its last row is
    still forming. Computing features on it repaints the signal: early in a bar
    the close is pinned to the high or the low, so CFI — (close-open)/(high-low)
    — is mechanically +/-1, and the in-progress bar's tiny true range deflates
    NATR, inflating every term that divides by it. The score can cross the entry
    threshold on a reading that no longer exists seconds later.

    The Hummingbot controller does exactly this; the two must agree.
    """
    if df is None or df.empty:
        return df
    return df.iloc[:-1]


def _compute_signal(config: Config, fast: pd.DataFrame, slow: pd.DataFrame,
                    funding_rate: float | None) -> dict:
    """
    Blend four normalised features into a score in [-1, 1].

    Every term is mapped into the same unit before it is weighted: the two
    price-space features are divided by live NATR and squashed with tanh, so
    the score means the same thing on a calm major and a volatile alt.
    """
    # Closed bars only — see _closed_bars.
    fast = _closed_bars(fast)
    slow = _closed_bars(slow)

    natr_series = _natr_pct(fast, ADX_LENGTH)
    natr_pct = float(natr_series.iloc[-1])
    if math.isnan(natr_pct) or natr_pct <= 0:
        natr_pct = config.natr_baseline_pct
    natr_frac = natr_pct / 100.0

    # Candle flow imbalance — where the close sits inside the bar's range
    candle_range = (fast["high"] - fast["low"]).replace(0, np.nan)
    cfi = ((fast["close"] - fast["open"]) / candle_range).fillna(0.0).clip(-1.0, 1.0)
    cfi_smooth = float(cfi.rolling(5, min_periods=1).mean().iloc[-1])

    # VWAP extension — rolling window, so the lookback is identical on every bar
    window = max(int(config.vwap_window), 2)
    typical = (fast["high"] + fast["low"] + fast["close"]) / 3.0
    vol = fast["volume"].fillna(0.0)
    pv = (typical * vol).rolling(window, min_periods=1).sum()
    vv = vol.rolling(window, min_periods=1).sum().replace(0, np.nan)
    vwap = (pv / vv).fillna(typical.rolling(window, min_periods=1).mean())
    vwap_dev = float(((fast["close"] - vwap) / vwap.replace(0, np.nan)).fillna(0.0).iloc[-1])
    vwap_signal = float(np.tanh((vwap_dev / natr_frac) / 2.0))

    # Trend — distance from an EMA measured in ATR units
    ema = fast["close"].ewm(span=config.trend_ema_length, adjust=False).mean()
    atr = _atr(fast, ADX_LENGTH).replace(0, np.nan)
    trend_raw = float(((fast["close"] - ema) / atr).fillna(0.0).iloc[-1])
    trend_signal = float(np.tanh(trend_raw / 2.0))

    rsi = float(_rsi(fast["close"], config.rsi_length).iloc[-1])

    fast_adx, _ = _adx_di(fast)
    slow_adx, slow_di = _adx_di(slow) if not slow.empty else (float("nan"), 0.0)

    raw = (
        config.cfi_weight * cfi_smooth
        + config.vwap_weight * vwap_signal
        + config.trend_weight * trend_signal
    )

    # RSI dampener — proportional, not a binary veto
    damp = 1.0
    if raw > 0 and rsi > config.rsi_overbought:
        damp = max(1.0 - (rsi - config.rsi_overbought) / max(100.0 - config.rsi_overbought, 1e-9), 0.0)
    elif raw < 0 and rsi < config.rsi_oversold:
        damp = max(1.0 - (config.rsi_oversold - rsi) / max(config.rsi_oversold, 1e-9), 0.0)
    damped = raw * damp

    di_term = config.di_weight * slow_di

    # Continuous crowding tilt: builds smoothly and saturates toward the
    # configured strength instead of snapping as the rate crosses the threshold.
    funding_bias = 0.0
    if funding_rate is not None:
        thr = max(abs(config.funding_threshold), 1e-9)
        funding_bias = -config.funding_bias_strength * math.tanh(funding_rate / thr)

    score = float(np.clip(damped + di_term + funding_bias, -1.0, 1.0))

    slow_regime = _classify(slow_adx, config.adx_trending_threshold, config.adx_extreme_threshold)
    fast_regime = _classify(fast_adx, config.adx_trending_threshold, config.adx_extreme_threshold)
    # The slow frame is the risk-off filter — EXTREME there halts everything.
    # UNKNOWN halts too: an ADX we could not compute is not evidence of a calm
    # higher timeframe, and treating it as RANGING let the fast frame alone open
    # the gate with the filter blind (see _classify).
    # EXTREME on the fast frame is a short-term impulse; it just fails to confirm.
    if slow_regime in ("EXTREME", "UNKNOWN"):
        entry_gate = "halt"
    elif (slow_regime == "TRENDING" and fast_regime == "TRENDING"
            and config.allow_fast_regime_entry):
        entry_gate = "both"
    elif slow_regime == "TRENDING":
        entry_gate = "slow"
    elif fast_regime == "TRENDING" and config.allow_fast_regime_entry:
        entry_gate = "fast"
    else:
        entry_gate = "none"

    if entry_gate in ("none", "halt"):
        direction = "HOLD"
        reason = f"regime gate {entry_gate} (slow={slow_regime}, fast={fast_regime})"
    elif score >= config.signal_threshold:
        direction = "LONG"
        reason = f"score {score:+.3f} >= threshold {config.signal_threshold:.2f}"
    elif score <= -config.signal_threshold:
        direction = "SHORT"
        reason = f"score {score:+.3f} <= -threshold {config.signal_threshold:.2f}"
    else:
        direction = "HOLD"
        reason = f"|score| {abs(score):.3f} below threshold {config.signal_threshold:.2f}"

    multiplier = float(np.clip(
        natr_pct / max(config.natr_baseline_pct, 1e-6),
        config.vol_multiplier_min,
        config.vol_multiplier_max,
    ))

    return {
        # The last CLOSED bar of the *candle* feed, which may be a different
        # connector and a different quote asset from the execution venue. It is
        # an indicator input, never a price to quote against — run() attaches the
        # execution venue's own `exec_price` for that. The two are named apart so
        # they can never be silently swapped again.
        "candle_close": float(fast["close"].iloc[-1]),
        "score": round(score, 4),
        "direction": direction,
        "reason": reason,
        "entry_gate": entry_gate,
        "slow_regime": slow_regime,
        "fast_regime": fast_regime,
        "slow_adx": None if math.isnan(slow_adx) else round(slow_adx, 2),
        "fast_adx": None if math.isnan(fast_adx) else round(fast_adx, 2),
        "components": {
            "cfi": round(cfi_smooth, 4),
            "vwap": round(vwap_signal, 4),
            "trend": round(trend_signal, 4),
            "di": round(slow_di, 4),
            "funding": round(funding_bias, 4),
        },
        "natr_pct": round(natr_pct, 4),
        "vol_multiplier": round(multiplier, 3),
        "rsi": round(rsi, 2),
        "funding_rate": funding_rate,
    }


def _build_ladder(config: Config, signal: dict) -> tuple[dict | None, str]:
    """Turn the signal into the exact numbers a dca_executor needs.

    Returns (ladder, refusal). `refusal` explains in one clause why no ladder was
    built, so the caller can print the reason instead of silently omitting the
    block and leaving the agent to guess whether the market was quiet.
    """
    if signal["direction"] == "HOLD":
        return None, "the decision is HOLD"

    # The rungs are limit prices on the EXECUTION venue, so they have to be
    # anchored to that venue's own price. The candle close belongs to the proxy
    # feed and carries cross-venue basis, USDT-vs-USDC quote basis, and up to a
    # full bar of staleness — regularly more than the near rung's own offset,
    # which posts a MAKER buy above the market: rejected post-only, or filled as
    # a taker on a strategy whose take-profit floor is sized for maker fees.
    price = signal.get("exec_price")
    if not price or price <= 0:
        return None, ("no execution-venue price is available — the candle feed is a proxy "
                      "and must not be used to price limit orders")

    if config.total_amount_quote <= 0:
        return None, ("no budget supplied — pass the session's total_amount_quote in the "
                      "routine config; the routine will not invent a size")

    mult = signal["vol_multiplier"]
    side = 1 if signal["direction"] == "LONG" else 2
    spreads = list(config.dca_spreads)
    weights = list(config.dca_amounts_pct)

    prices = [
        round(price * (1 - s * mult), 8) if side == 1 else round(price * (1 + s * mult), 8)
        for s in spreads
    ]
    amounts = [round(config.total_amount_quote * w, 4) for w in weights]

    take_profit = max(config.take_profit * mult, config.min_take_profit)
    return {
        "side": side,
        "prices": prices,
        "amounts_quote": amounts,
        # Emitted explicitly: AGENT.md lists leverage as a required dca_executor
        # field, and this block is meant to be pasted verbatim. Left out, the
        # executor inherits whatever leverage the account happens to carry.
        "leverage": config.leverage,
        "stop_loss": round(config.stop_loss * mult, 6),
        "take_profit": round(take_profit, 6),
        "time_limit": config.time_limit,
        "mode": "MAKER",
    }, ""


async def _subscribe_trading_pair(client, connector_name: str, trading_pair: str,
                                  cap: float = SUBSCRIBE_TIMEOUT_SEC) -> tuple[bool, str]:
    """Ask the API to subscribe `trading_pair` on `connector_name`. Never raises.

    Returns (accepted, detail). True means the API did not report a failure —
    that is an ACCEPT, not a verified live feed (see the body check below).
    This is a market-data call — it starts an order-book feed and places no
    order, moves no funds and changes no account state — and it is idempotent:
    calling it for a pair already subscribed is a no-op. It is the one remedy
    for the hang described in the SUBSCRIBE_READY_SEC comment above, and like
    every other probe here it is advisory: a failure to subscribe is reported,
    never raised into the tick.
    """
    try:
        result = await asyncio.wait_for(
            client.market_data.add_trading_pair(
                connector_name, trading_pair, timeout=SUBSCRIBE_READY_SEC,
            ),
            timeout=cap,
        )
    except Exception as e:  # noqa: BLE001 — a failed remedy must not break the tick
        code, detail = _probe_error(e, cap)
        logger.warning("add_trading_pair failed on %s %s: %s: %s",
                       connector_name, trading_pair, code, detail)
        return False, f"{code}: {detail}"
    # The endpoint reports its own outcome in the body; a 200 is not by itself a
    # subscription. Only an explicit failure is read as a failure — every other
    # body shape is reported as ACCEPTED, which is exactly what it is and no
    # more. The payload shape is not ours to depend on, so callers must say "the
    # API accepted the call" and never "the pair is subscribed": what actually
    # confirms a live feed is get_order_book_diagnostics (tracker_ready /
    # websocket_status / trading_pairs), or a retried probe that reads a book.
    if isinstance(result, dict):
        status = str(result.get("status", result.get("state", ""))).lower()
        message = str(result.get("message", result.get("detail", "")) or "")
        if status in ("error", "failed", "failure") or result.get("success") is False:
            logger.warning("add_trading_pair refused on %s %s: %s",
                           connector_name, trading_pair, message or result)
            return False, f"the API refused the subscription: {message or result}"
        return True, message or (status or "accepted")
    return True, "accepted"


async def _read_exit_book(client, connector_name: str, trading_pair: str,
                          notional: float, band_pct: float,
                          cap: float = PROBE_TIMEOUT_SEC) -> dict:
    """One attempt at reading the exit book. Callers go through _exit_liquidity.

    Can the book absorb the exit we are about to owe?

    The entry is a resting maker ladder and always fills politely. The exit is a
    market order, and a venue will reject it outright when there is no liquidity
    inside its price band. On derive_perpetual XRP-USDC that killed 53 of ~56
    closes: 21 entry fills against 3 close fills, positions opened that could not
    be exited. Sizing has to be judged against the exit, not the entry.

    `state` is one of:
      OK          — the book was read and can absorb `notional`
      THIN        — the book was read and cannot
      UNVERIFIED  — nothing was verified; `code` says why. Either the book could
                    not be read (a `_probe_error` code), or it was read and there
                    was no `notional` to judge it against (NOT_SIZED).
    UNVERIFIED is never OK. The failure that defaults to "ok" is the one that
    opens a position nobody can close. `mid` is kept because it is the only live
    price this routine ever sees from the execution venue, and it is what the
    ladder anchors to.
    """
    out = {"state": "UNVERIFIED", "code": "NOT_RUN", "ok": False, "checked": False,
           "bid_quote": None, "ask_quote": None, "mid": None, "note": "", "detail": ""}
    try:
        ob = await asyncio.wait_for(
            client.market_data.get_order_book(connector_name, trading_pair, depth=50),
            timeout=cap,
        )
        # Some endpoints wrap the payload as {"data": {...}} — _to_frame already
        # normalises that same envelope for candles. Unwrap it, then insist on a
        # mapping that actually carries bids/asks: an unrecognised envelope fell
        # through `.get("bids", [])` -> [] -> EMPTY_BOOK, i.e. a gap in our own
        # parsing reported as a venue with no book.
        if isinstance(ob, dict) and "bids" not in ob and "asks" not in ob:
            inner = ob.get("data")
            if isinstance(inner, dict):
                ob = inner
        if not isinstance(ob, dict) or ("bids" not in ob and "asks" not in ob):
            raise TypeError(
                f"order-book payload carries no bids/asks: {type(ob).__name__}"
                + (f" keys={sorted(ob)[:8]}" if isinstance(ob, dict) else "")
            )
        bids = _normalize_levels(ob.get("bids", []))
        asks = _normalize_levels(ob.get("asks", []))
        if not bids or not asks:
            out.update(code="EMPTY_BOOK",
                       detail="the venue returned no bids or no asks",
                       note="EMPTY_BOOK: the venue returned no bids or no asks")
            return out
        # The API does not promise sorted levels, and the mid is only the mid if
        # these really are the best bid and ask.
        bids.sort(key=lambda level: level[0], reverse=True)
        asks.sort(key=lambda level: level[0])
        mid = (bids[0][0] + asks[0][0]) / 2.0
        band = mid * band_pct / 100.0
        bid_q = sum(p * a for p, a in bids if p >= mid - band)
        ask_q = sum(p * a for p, a in asks if p <= mid + band)
        if notional <= 0:
            # No exit size was supplied (Config.total_amount_quote defaults to 0
            # exactly so an un-forwarded budget is visible), so there is nothing
            # to judge the book against. `min(bid_q, ask_q) >= 0` is trivially
            # true, which rendered a one-lot book as "VERIFIED OK" — a fail-open
            # on the single guard this probe exists to be, and the strongest
            # possible green light handed out on zero evidence. The measurement
            # is real and is kept (mid anchors the ladder); the VERDICT is not.
            note = ("NOT_SIZED: the book was read, but total_amount_quote is 0 so there is no "
                    "exit size to test it against — nothing about depth is verified")
            out.update(checked=True, ok=False, mid=mid, bid_quote=bid_q, ask_quote=ask_q,
                       state="UNVERIFIED", code="NOT_SIZED", detail=note, note=note)
            return out
        ok = min(bid_q, ask_q) >= notional
        out.update(checked=True, ok=ok, mid=mid, bid_quote=bid_q, ask_quote=ask_q,
                   state="OK" if ok else "THIN", code="OK" if ok else "THIN")
        if not ok:
            out["note"] = (f"only {min(bid_q, ask_q):,.0f} quote within "
                           f"{band_pct}% of mid, need {notional:,.0f} to exit")
    except Exception as e:  # noqa: BLE001 — advisory only, never break the tick
        code, detail = _probe_error(e, cap)
        # MALFORMED_BOOK is a defect in our own parsing rather than an outage,
        # so it is logged loudly instead of blending into the warning stream.
        log = logger.error if code == "MALFORMED_BOOK" else logger.warning
        log("order book probe failed on %s %s: %s: %s",
            connector_name, trading_pair, code, detail)
        out.update(state="UNVERIFIED", code=code, ok=False, checked=False,
                   detail=detail, note=f"{code}: {detail}")
    return out


async def _exit_liquidity(client, connector_name: str, trading_pair: str,
                          notional: float, band_pct: float,
                          cap: float = PROBE_TIMEOUT_SEC) -> dict:
    """_read_exit_book, plus ONE self-heal attempt when the probe times out.

    The only failure this repairs is the one that actually happened: the pair
    was never subscribed, so the order-book tracker had no feed and the request
    hung to the cap with no error. The remedy is a single idempotent,
    market-data-only add_trading_pair followed by one retry of the probe.

    It is attempted ONLY for TRANSPORT_TIMEOUT, and only for the order book.
    Every other code is a different fault and subscribing cannot touch it: a
    BAD_REQUEST is the API refusing this connector or pair on this endpoint, a
    MALFORMED_BOOK is a defect in our own parser, an EMPTY_BOOK was answered,
    and BACKEND_UNREACHABLE means the call never landed. Once per tick, never
    in a loop: if subscribing did not fix it the first time it will not fix it
    the second, and a retry loop would spend the whole tick budget.

    The outcome is recorded under "heal" and printed on the connectivity block,
    because a probe that silently repairs itself is a probe whose evidence the
    agent cannot read: the pair being unsubscribed is itself the diagnosis, and
    the subscription is RUNTIME state that does not survive an API restart.

    "recovered" means one thing only: the retry actually READ a book. A retry
    that fails some OTHER way is reported as the different fault it is — never
    as a repair, and never with a cause the retry did not establish.
    """
    heal = {"attempted": False, "subscribed": False, "recovered": False, "note": ""}
    out = await _read_exit_book(client, connector_name, trading_pair,
                                notional, band_pct, cap)
    if out.get("code") == "TRANSPORT_TIMEOUT":
        heal["attempted"] = True
        call = f"add_trading_pair({connector_name}, {trading_pair})"
        subscribed, detail = await _subscribe_trading_pair(client, connector_name,
                                                           trading_pair)
        heal["subscribed"] = subscribed
        if not subscribed:
            heal["note"] = (
                f"{call} was ATTEMPTED after the timeout and FAILED ({detail}), so the pair "
                f"could not be subscribed and the hang is still unexplained — treat the "
                f"timeout as unresolved."
            )
        else:
            retry = await _read_exit_book(client, connector_name, trading_pair,
                                          notional, band_pct, cap)
            if retry.get("code") == "TRANSPORT_TIMEOUT":
                # "ACCEPTED", not "SUCCEEDED", and "less likely", not "ruled
                # out": _subscribe_trading_pair reports success for any body it
                # does not recognise as a refusal, so it cannot certify that the
                # feed came up. The conclusion below names the endpoint that can.
                heal["note"] = (
                    f"{call} was ACCEPTED ({detail}) but the retried probe timed out AGAIN — "
                    f"that makes an unsubscribed tracker the LESS likely explanation, though an "
                    f"accepted call is not proof the feed came up. The timeout is unresolved."
                )
            elif retry.get("checked"):
                # Recovery is gated on the retry having actually READ and parsed
                # a book — `checked` is set only after bids and asks came off a
                # real payload (OK / THIN / NOT_SIZED). Branching instead on
                # "the code is no longer TRANSPORT_TIMEOUT" declared
                # BACKEND_UNREACHABLE, VENUE_UNREACHABLE, BAD_REQUEST,
                # MALFORMED_BOOK and EMPTY_BOOK all "recovered", and printed
                # "not unreachable" three lines under a probe line reading
                # BACKEND_UNREACHABLE: a confident wrong cause, stated in-band.
                heal["recovered"] = True
                heal["note"] = (
                    f"the first probe timed out; {call} was ACCEPTED ({detail}) and the retry "
                    f"then READ the book ({retry.get('code')}) — so the hang was an UNSUBSCRIBED "
                    f"pair with no feed, not an unreachable venue. The subscription is runtime "
                    f"state and is lost on an API container restart, so expect this again "
                    f"after one."
                )
                out = retry
            else:
                # The subscribe was accepted and the retry still did not read a
                # book: it failed a DIFFERENT way. That establishes nothing about
                # what caused the original timeout, so nothing here claims it —
                # the new code is named and the agent is sent to that code's own
                # conclusion, which is written for that fault.
                heal["note"] = (
                    f"{call} was ACCEPTED ({detail}) but the retry did NOT read a book either — "
                    f"it came back {retry.get('code')}, a DIFFERENT fault from the timeout. "
                    f"Subscribing did not restore the probe, and the cause of the timeout is "
                    f"NOT established by this. What stands now is {retry.get('code')} — act on "
                    f"its conclusion below, not on the timeout."
                )
                out = retry
        logger.warning("order book self-heal on %s %s: %s",
                       connector_name, trading_pair, heal["note"])
    out["heal"] = heal
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

# How each probe fault code is explained to the agent. The conclusion under the
# connectivity block used to be ONE unconditional paragraph — "INFRASTRUCTURE
# fault ... it is NOT thin liquidity and NOT a venue/pair mismatch ... both are
# valid connector names ... do NOT change connector_name" — printed for every
# non-OK code. That is the same confident wrong root cause the classifier above
# exists to remove, only supplied by us instead of invented by the agent, and it
# was wrong for three of the four cases it covered: BAD_REQUEST is the API
# REJECTING the request (the one code where the connector or the pair really may
# be wrong for that endpoint), MALFORMED_BOOK is a defect in our own parser, and
# EMPTY_BOOK is a genuine book condition — the opposite of "not thin liquidity".
# So the conclusion is chosen from the code.
_FAULT_CLASS = {
    "BACKEND_UNREACHABLE": "infra",
    "TRANSPORT_TIMEOUT": "infra",
    "VENUE_UNREACHABLE": "infra",
    "API_ERROR": "infra",
    "BAD_REQUEST": "rejected",
    "MALFORMED_BOOK": "parser",
    "EMPTY_BOOK": "book",
    "UNKNOWN": "unclassified",
}


def _fault_conclusion(cls: str, server: str, config: Config, probes: list[str],
                      book_ok: bool, funding_ok: bool, book_timeout: bool = False,
                      heal: dict | None = None) -> list[str]:
    """The paragraph the agent acts on, written for ONE fault class only.

    `probes` names the probes that failed with THIS class, so nothing here is
    asserted about a probe that did not: on a mixed tick — the order book
    rejected, funding timed out — a single paragraph would have to call the same
    tick both "rejected" and "never answered", and one of the two would be false.

    No branch tells the agent that a connector name is valid. Support on this API
    is PER-ENDPOINT — the trading catalogue lists connectors the market-data
    endpoints refuse (derive_perpetual serves funding, refuses candles, and hung
    on the order book until its pair was subscribed) — so "it is a real
    connector" is not evidence that the endpoint we just called accepts it, and a
    single blanket cause for every failure on a venue is not a thing that exists.

    `book_timeout` is passed separately from the class rather than derived from
    it: "infra" can be carried by the funding probe alone, and the subscription
    advice below is only true of the order book.
    """
    lead = "                   => "
    cont = "                      "
    which = " and ".join(probes) or "probe"
    book_hit = "order-book" in probes
    if cls == "rejected":
        head = (f"{lead}The API REJECTED the {which} probe (HTTP 400/404/422) — it answered, so "
                f"this is not an outage.")
        if book_hit:
            head += " Nothing at all was measured about liquidity."
        return [
            head,
            f"{cont}CHECK connector_name and trading_pair against the API's own error text above: "
            f"this endpoint may not accept '{config.connector_name}', or may not carry "
            f"{config.trading_pair} on it.",
            f"{cont}Support is PER-ENDPOINT here — a connector the trading catalogue lists can "
            f"still be refused by market-data — so do not conclude the name is fine because "
            f"another endpoint took it.",
            f"{cont}Retrying will not fix it; a config change will. If {config.connector_name} is "
            f"the venue the user chose, keep EXECUTING there and source the missing data from a "
            f"proxy feed (candle_connector / candle_trading_pair) instead of moving the trade.",
        ]
    if cls == "parser":
        detail = (" Order-book levels are normalised by _normalize_levels." if book_hit else "")
        return [
            f"{lead}MALFORMED_BOOK on the {which} probe is a defect in OUR OWN parsing — not a "
            f"venue fault and not a network fault. The API answered; this routine could not read "
            f"the payload shape it returned.{detail}",
            f"{cont}Do not journal it as an outage and do not change connector_name or "
            f"trading_pair — neither touches it. Report the payload shape so the routine can be "
            f"fixed.",
        ]
    if cls == "book":
        lines = [
            f"{lead}EMPTY_BOOK is a real BOOK condition on {config.connector_name} "
            f"{config.trading_pair}: the venue answered and reported no bids, or no asks. It is "
            f"not an outage, and not a parser bug.",
        ]
        if (heal or {}).get("attempted"):
            # This tick STARTED the feed: the first probe timed out, the routine
            # subscribed the pair, and the book was read seconds later. A tracker
            # that has just come up and not yet populated answers exactly like a
            # market with no orders in it — the same warm-up class of fault the
            # book_timeout paragraph exists to name before an outage. It is named
            # first here for the same reason: a repeat of this line gets journalled
            # as a permanent fact about the pair.
            lines.append(
                f"{cont}But this routine SUBSCRIBED the pair seconds earlier, on this same tick "
                f"(see the order-book heal line above), and a tracker that has just been "
                f"subscribed and has not populated yet returns an empty book too. That is the "
                f"cheaper explanation and it is UNTESTED: re-read the book next tick, with the "
                f"feed already up, before recording anything about this pair's liquidity."
            )
            lines.append(
                f"{cont}If it is STILL empty then, it is thin liquidity at its limit and the "
                f"strongest do-not-trade-this-pair evidence there is — a position opened here "
                f"could not be closed."
            )
        else:
            lines.append(
                f"{cont}That is thin liquidity at its limit and the strongest "
                f"do-not-trade-this-pair evidence there is — a position opened here could not "
                f"be closed."
            )
        return lines
    if cls == "unclassified":
        return [
            f"{lead}The {which} probe raised an exception this routine does not classify, so the "
            f"cause is NOT known. Read the type and message above, and do not infer a root cause "
            f"from silence.",
        ]
    # "infra" — the case that is actually happening in production.
    lines = [
        f"{lead}INFRASTRUCTURE fault on the path from server '{server}' to "
        f"{config.connector_name}, seen by the {which} probe — not a market condition.",
        f"{cont}This class of failure is not a rejection: a connector or pair an endpoint does not "
        f"accept comes back as HTTP 400/404/422. Here the request was taken and never answered, or "
        f"failed in transit.",
    ]
    if book_hit:
        # Only claimable when the ORDER-BOOK probe is one of these failures. If
        # the book was read and it is funding that died, "no book was read" would
        # be a false statement about the evidence — and the per-endpoint line
        # below already says which half failed.
        lines.append(
            f"{cont}It therefore says nothing whatever about the book — it is NOT thin liquidity, "
            f"because no book was read at all."
        )
    if book_timeout:
        # The actual cause, the one time this fired for real: the pair had never
        # been SUBSCRIBED, so the tracker had no feed and the request hung to the
        # cap with no error — tracker_ready false, websocket_status
        # not_connected, trading_pairs [], every listener task absent. Once the
        # pair was added the same book returned ~19,000 quote of depth within
        # 0.5% of the mid. Six journal entries blamed an outage instead. So the
        # cheap explanation is named FIRST, and the expensive one only after the
        # cheap one has been tested.
        heal = heal or {}
        lines.append(
            f"{cont}A TIMEOUT on the order book is NOT by itself evidence of an outage: a "
            f"tracker whose pair was never SUBSCRIBED hangs in exactly this way — no error, "
            f"no data, just the cap — and that is what it turned out to be last time."
        )
        if heal.get("subscribed"):
            # heal["subscribed"] is only as strong as _subscribe_trading_pair's
            # read of the response body, which reports True for any answer it
            # does not recognise as an explicit refusal. That is an ACCEPT, not a
            # confirmed live feed, so this says what was observed and points at
            # the endpoint that can actually settle it.
            lines.append(
                f"{cont}This routine called add_trading_pair for the pair this tick and the API "
                f"ACCEPTED it, then retried and timed out again (see the order-book heal line "
                f"above). That is evidence AGAINST an unsubscribed tracker but not proof — an "
                f"accepted call is not a confirmation that the feed came up. Confirm with "
                f"market_data.get_order_book_diagnostics({config.connector_name}) — "
                f"tracker_ready, websocket_status, trading_pairs — before concluding the "
                f"server's link to the venue is at fault, and still do not change "
                f"connector_name or trading_pair."
            )
        elif heal.get("attempted"):
            lines.append(
                f"{cont}This routine tried to subscribe the pair this tick and could not "
                f"(see the order-book heal line above), so the subscription is NOT ruled "
                f"out. Read market_data.get_order_book_diagnostics("
                f"{config.connector_name}) — tracker_ready, websocket_status, "
                f"trading_pairs — before concluding the venue is down."
            )
        else:
            lines.append(
                f"{cont}Check it: market_data.get_order_book_diagnostics("
                f"{config.connector_name}) reports tracker_ready / websocket_status / "
                f"trading_pairs, and market_data.add_trading_pair({config.connector_name}, "
                f"{config.trading_pair}) subscribes it. Suspect that BEFORE an outage."
            )
    if book_ok or funding_ok:
        alive, dead = ("order-book", "funding") if book_ok else ("funding", "order-book")
        lines.append(
            f"{cont}The {alive} probe DID answer for {config.connector_name} "
            f"{config.trading_pair}, so the server's route to this venue works — the fault is "
            f"confined to the {dead} endpoint, which this API supports per-endpoint."
        )
    lines += [
        f"{cont}Do NOT change connector_name or trading_pair to \"fix\" this: they are the venue "
        f"and pair the user configured, and changing them trades something else. Do not re-run the "
        f"signal against another venue,",
        f"{cont}and journal it once per outage rather than once per tick.",
    ]
    return lines


def _heal_lines(liq: dict) -> list[str]:
    """Report the order-book self-heal, so it is visible rather than magic.

    Printed on a healthy tick too: when the retry READ the book, connectivity
    reads "OK" and the single most useful fact of the tick — that the pair was
    not subscribed until this routine subscribed it, and will not be again after
    an API container restart — would otherwise vanish exactly when it was proved.

    The line leads with RECOVERED / NOT RECOVERED, straight off heal["recovered"],
    so a heal that repaired nothing cannot be skim-read as one that did.
    """
    heal = (liq or {}).get("heal") or {}
    if not heal.get("attempted"):
        return []
    # The one-word verdict is read off heal["recovered"] — the same flag
    # _exit_liquidity sets ONLY when the retry actually read a book — so the
    # status and the note it heads can never disagree.
    status = "RECOVERED" if heal.get("recovered") else "NOT RECOVERED"
    return [f"                   order-book heal  : {status} — {heal.get('note', '')}"]


def _connectivity_lines(server: str, config: Config, candle_connector: str,
                        candle_pair: str, candle_bars: int, liq: dict,
                        funding_code: str, funding_detail: str) -> list[str]:
    """Report reachability of the EXECUTION venue as a first-class signal.

    Both probes talk to connector_name/trading_pair while the candles come from a
    proxy feed, so "candles fine, both probes dead" is the most diagnostic fact
    the tick has: it isolates the fault to the server's link to the execution
    venue and rules out the pair, the connector name and market conditions.

    That fact used to be discarded entirely, and the agent invented a root cause
    to fill the gap — six consecutive journal entries blaming a venue/pair
    mismatch, one tick run off-config against a different connector, and a false
    claim written into learnings.md. So the conclusion is stated in-band, every
    tick, rather than left to be inferred — but it is BRANCHED on the fault code
    (see _FAULT_CLASS / _fault_conclusion), because one fixed conclusion for
    every code is exactly the invented root cause this block exists to stop.
    """
    # `checked` is true only after the book was actually read and parsed, so it
    # also covers NOT_SIZED (read, but no size to judge it against) — that is an
    # exit-liquidity verdict, not a connectivity fault, and belongs in the
    # exit_liquidity line rather than here.
    book_ok = bool(liq.get("checked"))
    funding_ok = funding_code in ("OK", "NO_FUNDING")
    if book_ok and funding_ok:
        return [f"  connectivity   : OK — server '{server}' is serving "
                f"{config.connector_name} {config.trading_pair}",
                *_heal_lines(liq)]

    book_desc = "OK" if book_ok else f"{liq['code']} ({liq['detail']})"
    if funding_code == "OK":
        funding_desc = "OK"
    elif funding_ok:
        funding_desc = f"OK — venue reports no funding ({funding_detail})"
    else:
        funding_desc = f"{funding_code} ({funding_detail})"

    # {fault class: the probes that failed with it} — each conclusion is then
    # attributed to the probes it is actually true of.
    faults: dict[str, list[str]] = {}
    if not book_ok:
        faults.setdefault(_FAULT_CLASS.get(liq["code"], "unclassified"), []).append("order-book")
    if not funding_ok:
        faults.setdefault(_FAULT_CLASS.get(funding_code, "unclassified"), []).append("funding")
    classes = set(faults)
    # REACHABLE, not DEGRADED, when every fault we hold is one the API ANSWERED:
    # a rejection, a defect in our parser, or an empty book. Labelling those
    # "DEGRADED connectivity" sends the agent hunting an outage that is not there.
    if not (classes & {"infra", "unclassified"}):
        head = (f"  connectivity   : REACHABLE — server '{server}' answered for "
                f"{config.connector_name} {config.trading_pair}; the fault below is not a "
                f"connectivity problem")
    elif not (book_ok or funding_ok):
        # "cannot serve" only when both probes are down; one live probe proves
        # the route works and narrows the fault to a single endpoint.
        head = (f"  connectivity   : DEGRADED — neither the order-book nor the funding probe "
                f"returned usable data from server '{server}' for {config.connector_name} "
                f"{config.trading_pair}")
    else:
        head = (f"  connectivity   : DEGRADED — server '{server}' is only partly serving "
                f"{config.connector_name} {config.trading_pair}")

    if candle_connector != config.connector_name or candle_pair != config.trading_pair:
        candles_line = (f"                   candles          : OK — {candle_bars} bars from a "
                        f"DIFFERENT feed ({candle_connector} {candle_pair}), so the server itself "
                        f"is alive")
    else:
        # Same connector AND same pair: the candles are not an independent
        # control. Printing "cannot serve X" directly above "150 bars from X"
        # reads as a contradiction, and the old conclusion then went on to name
        # the same connector twice as "both valid connector names".
        candles_line = (f"                   candles          : OK — {candle_bars} bars from the "
                        f"SAME connector and pair; no proxy feed is configured, so this is NOT an "
                        f"independent control — it shows only that the candles endpoint accepts "
                        f"{candle_connector} {candle_pair}")

    lines = [
        head,
        f"                   order-book probe : {book_desc}",
        f"                   funding probe    : {funding_desc}",
        *_heal_lines(liq),
        candles_line,
    ]
    # Only the ORDER-BOOK probe timing out licenses the subscription advice; a
    # funding timeout is the same class but a different endpoint.
    book_timeout = (not book_ok) and liq.get("code") == "TRANSPORT_TIMEOUT"
    for cls in ("rejected", "parser", "book", "infra", "unclassified"):
        if cls in faults:
            lines.extend(_fault_conclusion(cls, server, config, faults[cls], book_ok,
                                           funding_ok, book_timeout=book_timeout,
                                           heal=liq.get("heal")))
    return lines


def _exit_liquidity_lines(liq: dict, notional: float, band_pct: float) -> list[str]:
    """Render the exit-liquidity verdict.

    The word "unknown" never appears: it read as a soft maybe, and the agent
    treated it as one. An unread book — and a book read against no size at all —
    is reported as UNVERIFIED with its fault code and the explicit consequence:
    no entry.
    """
    if liq["code"] == "NOT_SIZED":
        return [
            f"  exit_liquidity : UNVERIFIED (NOT_SIZED) — the book was read "
            f"({liq['bid_quote']:,.0f} bid / {liq['ask_quote']:,.0f} ask quote within "
            f"{band_pct}% of mid) but NO exit size was supplied to test it against,",
            "                   so nothing is verified — a zero requirement is met by any book, "
            "including a one-lot one. Pass the session's total_amount_quote in the routine "
            "config; entry stays blocked until then.",
        ]
    if liq["state"] == "OK":
        return [f"  exit_liquidity : VERIFIED OK — {liq['bid_quote']:,.0f} bid / "
                f"{liq['ask_quote']:,.0f} ask quote within {band_pct}% of mid, "
                f"need {notional:,.0f} to exit"]
    if liq["state"] == "THIN":
        return [f"  exit_liquidity : VERIFIED THIN — {liq['note']}"]
    return [
        f"  exit_liquidity : UNVERIFIED ({liq['code']}) — treat as NOT OK, entry is blocked",
        f"                   {liq['detail']}",
    ]


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return "FLOWEDGE SIGNAL: no Hummingbot API server available — cannot compute. HOLD."
    # Named in the connectivity line so a reachability fault is attributed to the
    # server, not to the venue or the pair.
    server = _server_label(client)

    # derive_perpetual and similar venues do not serve candle data.
    # Allow a proxy connector so the signal can be computed from a liquid reference feed
    # (e.g. binance_perpetual XRP-USDT) while orders are placed on the execution connector.
    candle_connector = config.candle_connector or config.connector_name
    candle_pair = config.candle_trading_pair or config.trading_pair

    # The candle fetch is deliberately NOT capped with asyncio.wait_for the way
    # the two advisory probes below are — it is the signal's only mandatory
    # input, so it gets the shared client's own (60s) timeout. That means the cap
    # handed to _probe_error has to be the time ACTUALLY spent: the default would
    # render "no response within 10s" after a full minute of waiting, a false
    # statement about the evidence in the one place built to keep it honest.
    started = time.monotonic()
    try:
        fast_raw = await client.market_data.get_candles(
            candle_connector, candle_pair, config.fast_interval,
            config.fast_max_records,
        )
        slow_raw = await client.market_data.get_candles(
            candle_connector, candle_pair, config.slow_interval,
            config.slow_max_records,
        )
    except Exception as e:  # noqa: BLE001 — surface as text, never raise into the tick
        code, detail = _probe_error(e, round(time.monotonic() - started, 1))
        logger.warning("candle fetch failed on %s %s: %s: %s",
                       candle_connector, candle_pair, code, detail)
        return (f"FLOWEDGE SIGNAL: candle fetch failed on {candle_pair} @ {candle_connector} "
                f"— {code}: {detail}. HOLD this tick.")

    fast = _to_frame(fast_raw)
    slow = _to_frame(slow_raw)

    # +1 everywhere: this check runs on the raw frame, and _closed_bars drops the
    # forming bar before any indicator sees it. Without the +1, min_bars raw rows
    # become min_bars-1 usable ones and _adx_di silently returns NaN.
    min_bars = max(config.trend_ema_length, config.rsi_length + 1, ADX_LENGTH * 2)
    if len(fast) < min_bars + 1:
        return (
            f"FLOWEDGE SIGNAL: only {len(fast)} {config.fast_interval} candles available, "
            f"need {min_bars + 1} to warm up the indicators. HOLD this tick."
        )
    # The slow frame had no guard at all, so a cold or restarted 15m feed left
    # slow_adx NaN — and the regime filter that is supposed to halt trading is
    # the one thing that must not be silently absent.
    if len(slow) < ADX_LENGTH * 2 + 1:
        return (
            f"FLOWEDGE SIGNAL: only {len(slow)} {config.slow_interval} candles available, "
            f"need {ADX_LENGTH * 2 + 1} to compute the slow-frame regime filter — without it "
            f"the EXTREME halt cannot fire. HOLD this tick."
        )

    # Both probes below hit the EXECUTION connector, are advisory, and cap
    # themselves (PROBE_TIMEOUT_SEC) so an unreachable venue costs seconds rather
    # than the client's full 60s timeout — twice per tick.
    funding_rate, funding_code, funding_detail = await _fetch_funding(
        client, config.connector_name, config.trading_pair,
    )
    liq = await _exit_liquidity(
        client, config.connector_name, config.trading_pair,
        config.total_amount_quote, config.exit_depth_band_pct,
    )

    signal = _compute_signal(config, fast, slow, funding_rate)
    signal["exit_liquidity"] = liq
    # Ladder anchor: the mid the exit probe already read from the EXECUTION
    # venue. It exists only when that probe succeeded — which is also the only
    # case in which a ladder is emitted at all, so the two gates line up, and
    # reusing it spares a second capped call against a possibly dead venue.
    signal["exec_price"] = liq.get("mid")
    signal["basis_pct"] = (
        (liq["mid"] - signal["candle_close"]) / signal["candle_close"] * 100.0
        if liq.get("mid") and signal["candle_close"] else None
    )
    ladder, refusal = _build_ladder(config, signal)

    if signal["exec_price"]:
        exec_line = (f"  exec price     : {signal['exec_price']:.8g}  "
                     f"({config.connector_name} mid — the ladder anchor)")
        if signal["basis_pct"] is not None:
            exec_line += f"  basis {signal['basis_pct']:+.3f}% vs candle close"
    else:
        exec_line = ("  exec price     : UNAVAILABLE — no live price from the execution venue; "
                     "the candle close is a proxy and cannot price limit orders")

    budget_line = (f"  budget         : {config.total_amount_quote:,.2f} quote per entry "
                   f"@ {config.leverage}x leverage")
    if config.total_amount_quote <= 0:
        budget_line += " — NOT SUPPLIED, pass total_amount_quote in the routine config"
    budget_lines = [budget_line]
    # leverage is emitted inside the block labelled "use these values verbatim",
    # so a value nobody forwarded must be visible as such rather than passing for
    # a decision — the same warning the budget already gets. model_fields_set
    # separates "explicitly passed" from "defaulted"; the <= 1 arm also catches
    # callers that materialise every default before constructing the config.
    leverage_supplied = "leverage" in getattr(config, "model_fields_set", set())
    if not leverage_supplied or config.leverage <= 1:
        why = ("NOT SUPPLIED in the routine config" if not leverage_supplied
               else "supplied as 1")
        budget_lines.append(
            f"                   leverage {why} — any ladder below asserts "
            f"\"leverage\": {config.leverage} verbatim; pass the session's configured leverage "
            f"if it is not {config.leverage}x"
        )

    if funding_code == "OK":
        funding_line = f"  funding_rate   : {signal['funding_rate']}"
    elif funding_code == "NO_FUNDING":
        funding_line = f"  funding_rate   : none reported by the venue ({funding_detail})"
    else:
        funding_line = (f"  funding_rate   : UNVERIFIED ({funding_code}) — the funding tilt is "
                        f"absent from the score")

    c = signal["components"]
    lines = [
        f"FLOWEDGE SIGNAL — {config.trading_pair} @ {config.connector_name} "
        f"(execution venue, server '{server}')",
        f"  candle source  : {candle_pair} @ {candle_connector} (proxy feed — indicators only)",
        f"  candle close   : {signal['candle_close']}  "
        f"(last closed {config.fast_interval} bar of the candle feed)",
        exec_line,
        *budget_lines,
        f"  decision       : {signal['direction']}  ({signal['reason']})",
        f"  score          : {signal['score']:+.3f}   threshold {config.signal_threshold:.2f}",
        f"  entry_gate     : {signal['entry_gate']}",
        *_connectivity_lines(server, config, candle_connector, candle_pair, len(fast),
                             liq, funding_code, funding_detail),
        *_exit_liquidity_lines(liq, config.total_amount_quote, config.exit_depth_band_pct),
        f"  regime slow    : {signal['slow_regime']} (ADX {signal['slow_adx']})",
        f"  regime fast    : {signal['fast_regime']} (ADX {signal['fast_adx']})",
        f"  components     : cfi {c['cfi']:+.3f} | vwap {c['vwap']:+.3f} | "
        f"trend {c['trend']:+.3f} | di {c['di']:+.3f} | funding {c['funding']:+.3f}",
        f"  natr           : {signal['natr_pct']:.3f}%  -> vol multiplier {signal['vol_multiplier']:.2f}x",
        f"  rsi            : {signal['rsi']:.1f}",
        funding_line,
    ]

    lines.append("")
    if signal["direction"] == "HOLD":
        lines.append("No ladder produced — the decision is HOLD.")
    elif liq["state"] != "OK":
        # The structural gate. A ladder is a submittable instruction, so it is
        # withheld whenever the exit book is not verified deep enough to close
        # what it would open — the failure that produced 21 entry fills against
        # 3 closes. The decision itself is still reported, so nothing is hidden.
        if liq["code"] == "NOT_SIZED":
            # Distinct from a failed probe and from a thin book: the book was
            # read fine, there was simply no size to judge it against.
            lines.append(
                f"No ladder emitted — decision was {signal['direction']} ({signal['score']:+.3f}) "
                f"but exit_liquidity is UNVERIFIED (NOT_SIZED): the book was read, and with no "
                f"total_amount_quote there is no exit size to test it against. Pass the session's "
                f"total_amount_quote and re-run — nothing here says the book is deep enough."
            )
        else:
            state = (f"UNVERIFIED ({liq['code']})" if liq["state"] == "UNVERIFIED"
                     else "VERIFIED THIN")
            lines.append(
                f"No ladder emitted — decision was {signal['direction']} ({signal['score']:+.3f}) "
                f"but exit_liquidity is {state}. A ladder is emitted only when the exit book is "
                f"verified deep enough to close the position; entering here risks a position that "
                f"cannot be exited."
            )
    elif ladder is None:
        lines.append(f"No ladder emitted — {refusal}.")
    else:
        lines.append(
            f"READY-TO-SUBMIT dca_executor FIELDS (use these values verbatim — sized to the "
            f"{config.total_amount_quote:,.2f} quote budget at {config.leverage}x):"
        )
        if not leverage_supplied:
            # In-band at the point of use: the JSON below states a leverage this
            # routine was never told, and it is pasted as authoritative.
            lines.append(
                f"  ! leverage was NOT supplied to this routine and defaulted to "
                f"{config.leverage} — check it against the session's configured leverage before "
                f"submitting, and pass it in the routine config so this line stops guessing."
            )
        lines.append(json.dumps(ladder, indent=2))

    return "\n".join(lines)

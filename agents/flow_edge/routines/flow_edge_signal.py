"""Compute the FlowEdge regime signal and a ready-to-submit DCA ladder."""

import json
import logging
import math
from typing import Any

import numpy as np
import pandas as pd
from config_manager import get_client
from pydantic import BaseModel, Field
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)

CATEGORY = "Analysis"
ADX_LENGTH = 14


class Config(BaseModel):
    """FlowEdge signal — dual-timeframe ADX regime, volatility-normalised score, sized DCA ladder."""

    connector_name: str = Field(default="hyperliquid_perpetual", description="Exchange connector")
    trading_pair: str = Field(default="XRP-USD", description="Trading pair")
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
    total_amount_quote: float = Field(default=100.0, description="Quote capital for one entry")

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
    if math.isnan(adx):
        return "RANGING"
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


async def _fetch_funding(client, connector_name: str, trading_pair: str) -> float | None:
    """Live funding rate, or None on spot / when the venue does not report it."""
    try:
        info = await client.market_data.get_funding_info(connector_name, trading_pair)
    except Exception as e:  # noqa: BLE001 — a missing funding feed is not fatal
        logger.debug("funding info unavailable: %s", e)
        return None
    if not isinstance(info, dict):
        return None
    for key in ("rate", "funding_rate", "fundingRate", "next_funding_rate"):
        if info.get(key) is not None:
            try:
                return float(info[key])
            except (TypeError, ValueError):
                return None
    return None


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
    # EXTREME on the fast frame is a short-term impulse; it just fails to confirm.
    if slow_regime == "EXTREME":
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
        "price": float(fast["close"].iloc[-1]),
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


def _build_ladder(config: Config, signal: dict) -> dict | None:
    """Turn the signal into the exact numbers a dca_executor needs."""
    if signal["direction"] == "HOLD":
        return None

    price = signal["price"]
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
        "stop_loss": round(config.stop_loss * mult, 6),
        "take_profit": round(take_profit, 6),
        "time_limit": config.time_limit,
        "mode": "MAKER",
    }


async def _exit_liquidity(client, connector_name: str, trading_pair: str,
                         notional: float, band_pct: float) -> dict:
    """Can the book absorb the exit we are about to owe?

    The entry is a resting maker ladder and always fills politely. The exit is a
    market order, and a venue will reject it outright when there is no liquidity
    inside its price band. On derive_perpetual XRP-USDC that killed 53 of ~56
    closes: 21 entry fills against 3 close fills, positions opened that could not
    be exited. Sizing has to be judged against the exit, not the entry.
    """
    out = {"checked": False, "ok": True, "bid_quote": None, "ask_quote": None, "note": ""}
    try:
        ob = await client.market_data.get_order_book(connector_name, trading_pair, depth=50)
        bids = ob.get("bids", []) or []
        asks = ob.get("asks", []) or []
        if not bids or not asks:
            out["note"] = "empty order book"
            return out
        mid = (float(bids[0][0]) + float(asks[0][0])) / 2.0
        band = mid * band_pct / 100.0
        bid_q = sum(float(p) * float(a) for p, a in bids if float(p) >= mid - band)
        ask_q = sum(float(p) * float(a) for p, a in asks if float(p) <= mid + band)
        out.update(checked=True, bid_quote=bid_q, ask_quote=ask_q,
                   ok=min(bid_q, ask_q) >= notional)
        if not out["ok"]:
            out["note"] = (f"only {min(bid_q, ask_q):,.0f} quote within "
                           f"{band_pct}% of mid, need {notional:,.0f} to exit")
    except Exception as e:  # noqa: BLE001 — advisory only, never break the tick
        logger.warning("order book probe failed: %s", e)
        out["note"] = f"depth probe failed ({e})"
    return out


async def run(config: Config, context: ContextTypes.DEFAULT_TYPE) -> str:
    client = await get_client(context._chat_id, context=context)
    if not client:
        return "FLOWEDGE SIGNAL: no Hummingbot API server available — cannot compute. HOLD."

    try:
        fast_raw = await client.market_data.get_candles(
            config.connector_name, config.trading_pair, config.fast_interval,
            config.fast_max_records,
        )
        slow_raw = await client.market_data.get_candles(
            config.connector_name, config.trading_pair, config.slow_interval,
            config.slow_max_records,
        )
    except Exception as e:  # noqa: BLE001 — surface as text, never raise into the tick
        logger.warning("candle fetch failed: %s", e)
        return f"FLOWEDGE SIGNAL: candle fetch failed ({e}). HOLD this tick."

    fast = _to_frame(fast_raw)
    slow = _to_frame(slow_raw)

    min_bars = max(config.trend_ema_length, config.rsi_length + 1, ADX_LENGTH * 2)
    if len(fast) < min_bars:
        return (
            f"FLOWEDGE SIGNAL: only {len(fast)} {config.fast_interval} candles available, "
            f"need {min_bars} to warm up the indicators. HOLD this tick."
        )

    funding_rate = await _fetch_funding(client, config.connector_name, config.trading_pair)
    signal = _compute_signal(config, fast, slow, funding_rate)
    signal["exit_liquidity"] = await _exit_liquidity(
        client, config.connector_name, config.trading_pair,
        config.total_amount_quote, config.exit_depth_band_pct,
    )
    ladder = _build_ladder(config, signal)

    c = signal["components"]
    lines = [
        f"FLOWEDGE SIGNAL — {config.trading_pair} @ {config.connector_name}",
        f"price          : {signal['price']}",
        f"decision       : {signal['direction']}  ({signal['reason']})",
        f"score          : {signal['score']:+.3f}   threshold {config.signal_threshold:.2f}",
        f"entry_gate     : {signal['entry_gate']}",
        (f"exit_liquidity : {'OK' if signal['exit_liquidity']['ok'] else 'THIN — ' + signal['exit_liquidity']['note']}"
         if signal["exit_liquidity"]["checked"]
         else f"exit_liquidity : unknown ({signal['exit_liquidity']['note']})"),
        f"regime slow    : {signal['slow_regime']} (ADX {signal['slow_adx']})",
        f"regime fast    : {signal['fast_regime']} (ADX {signal['fast_adx']})",
        f"components     : cfi {c['cfi']:+.3f} | vwap {c['vwap']:+.3f} | "
        f"trend {c['trend']:+.3f} | di {c['di']:+.3f} | funding {c['funding']:+.3f}",
        f"natr           : {signal['natr_pct']:.3f}%  -> vol multiplier {signal['vol_multiplier']:.2f}x",
        f"rsi            : {signal['rsi']:.1f}",
        f"funding_rate   : {signal['funding_rate']}",
    ]

    if ladder:
        lines.append("")
        lines.append("READY-TO-SUBMIT dca_executor FIELDS (use these values verbatim):")
        lines.append(json.dumps(ladder, indent=2))
    else:
        lines.append("")
        lines.append("No ladder produced — the decision is HOLD.")

    return "\n".join(lines)

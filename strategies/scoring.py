"""
strategies.scoring — Signal quality scoring for both strategies.

Each scorer returns a float 0–100. Higher = better expected edge.

Mispricing score formula (natural max 100):
  Edge Ratio    40 pts  — deviation / fee_hurdle times above break-even
  Source        25 pts  — confirmation source quality (kalshi_confirmed > nd2_only)
                          Kalshi bonus uses sqrt curve (diminishing returns; max +7 pts)
  Timing        20 pts  — TTE sweet-spot (3–7 days optimal; <6h = 0; >30d = 5)
  Liquidity     15 pts  — type-aware log-scale volume ($500 dust floor; type cap)

Maker score formula (natural max 105):
  Effective Edge    30 pts  — half_spread + rebate; 2%+ = max
  Volume            35 pts  — type-aware log-scale volume ($500 dust floor; type cap)
  TTE Quality       25 pts  — lifecycle-relative (bucket) or absolute (milestone);
                               <10% of life = 0; 10–25% = 0–12; 25–85% = 12–25 (sweet);
                               >85% (brand-new) = 22–25 (slight thin-book discount)
  Price Balance     10 pts  — mid near 50¢ = max; steeper exponent near edges
  Capital Velocity   0–5 extra — bonus when BOTH volume is high AND in prime lifecycle zone

All component weights are multiplied by ``SCORE_WEIGHT_*`` config params so they can
be re-calibrated without code changes.  Final normalisation divides by the THEORETICAL
MAX at the current weights so that a perfect signal always scores 100, regardless of
how weights are tuned.

Lifecycle-relative TTE (bucket markets only)
--------------------------------------------
The old absolute 6h floor treated a freshly-opened bucket_1h (TTE=1h) the same as
a daily market in its final hour — both scored 0.  This was incorrect: the 1h bucket
at TTE=1h is 100% of its natural life remaining (healthy), while the daily at 1h
remaining is at 4% of its life (expiry risk).  The lifecycle-fraction model fixes
this by scoring relative to the market's canonical total duration.
"""
from __future__ import annotations

import math

import config
from strategies.mispricing.signals import MispricingSignal
from strategies.maker.signals import MakerSignal

from logger import get_bot_logger
_score_log = get_bot_logger(__name__)


# ── Per-type canonical market durations ──────────────────────────────────────
# Used to compute lifecycle fractions (fraction-of-life-remaining) so that
# a fresh bucket_1h (TTE=1h) and a fresh bucket_daily (TTE=24h) both score
# identically — they're both at ~100% of their natural lifetime remaining.
# Milestone / unknown markets fall back to absolute TTE logic.
_MARKET_TYPE_DURATION_SECS: dict[str, float] = {
    "bucket_5m":      300.0,
    "bucket_15m":     900.0,
    "bucket_1h":     3600.0,
    "bucket_4h":    14400.0,
    "bucket_daily":  86400.0,
    "bucket_weekly":604800.0,
}

# ── Per-type volume soft-caps ─────────────────────────────────────────────────
# Volume above the cap gives no additional score; prevents long two-sided markets
# from dominating on raw volume vs. short, fast-revolving buckets.
_VOLUME_TYPE_CAP: dict[str, float] = {
    "bucket_5m":     15_000.0,
    "bucket_15m":    30_000.0,
    "bucket_1h":     60_000.0,
    "bucket_4h":    100_000.0,
    "bucket_daily": 250_000.0,
    "bucket_weekly":500_000.0,
}
_VOLUME_DEFAULT_CAP = 100_000.0   # milestone / unknown
_VOLUME_DUST_FLOOR  =      500.0  # markets below this are too illiquid to score


# ── Shared helpers ────────────────────────────────────────────────────────────

def _volume_pts(volume_24hr: float, max_pts: float, market_type: str = "") -> float:
    """Log-scale volume component with type-aware soft cap and dust floor.

    Below $500 → 0 pts (illiquid dust market).
    Scores log10-scale from $500 up to the type cap; capped at max_pts.
    """
    vol = float(volume_24hr or 0.0)
    if vol < _VOLUME_DUST_FLOOR:
        return 0.0
    cap = _VOLUME_TYPE_CAP.get(market_type, _VOLUME_DEFAULT_CAP)
    norm_vol = min(vol, cap) / cap                      # 0.0 – 1.0
    # log10 scale: norm_vol=0.01 → 40%, 0.1 → 60%, 1.0 → 100%
    log_frac = math.log10(max(norm_vol * cap, _VOLUME_DUST_FLOOR)) / math.log10(cap)
    return min(log_frac, 1.0) * max_pts


def _tte_pts_mispricing(tte_years: float) -> float:
    """
    TTE quality for mispricing signals. Peak 20 pts at 3–7 days.
    Below 6 h:  0 pts (N(d2) unreliable near expiry).
    6 h – 3 d:  ramp 0 → 20 pts.
    3 – 7 d:   20 pts (sweet-spot, model stable + fast capital turn).
    7 – 30 d:  decay 20 → 5 pts (capital efficiency falls off).
    > 30 d:     5 pts (capital idle for weeks).
    """
    tte_days = max(0.0, tte_years * 365.0)
    if tte_days < 0.25:        # <6 h
        return 0.0
    if tte_days < 3.0:
        return (tte_days - 0.25) / (3.0 - 0.25) * 20.0
    if tte_days <= 7.0:
        return 20.0
    if tte_days <= 30.0:
        return 20.0 - (tte_days - 7.0) / (30.0 - 7.0) * 15.0  # 20 → 5
    return 5.0


def _tte_pts_maker(tte_secs: float, market_type: str = "") -> float:
    """
    TTE quality for maker signals.

    For bucket_* markets — lifecycle-relative scoring:
      Scores are based on the fraction of the market's canonical lifetime that
      remains, NOT absolute hours.  A freshly-opened bucket_1h (TTE=1h, frac=1.0)
      and a freshly-opened bucket_daily (TTE=24h, frac=1.0) score identically.

      Lifecycle zones (frac = tte_secs / canonical_duration):
        < 10%  of life  →   0 pts   (imminent expiry: gamma spike + adverse selection)
        10–25% of life  →  0–12 pts (final approach: gamma still elevated)
        25–50% of life  → 12–25 pts (ramping toward sweet-spot)
        50–85% of life  →  25 pts   (sweet-spot: good fill time + fast capital revolve)
        85–100% of life → 22–25 pts (brand new: thin book, fewer takers registered yet)

    For milestone/unknown markets — absolute TTE logic (unchanged):
        < 6 h   →   0 pts
        6–12 h  →  0–12 pts
        12h–1d  → 12–25 pts
        1–5 d   →  25 pts
        5–14 d  →  2–25 pts decay
        > 14 d  →   2 pts
    """
    tte_secs_c = max(0.0, tte_secs)
    total_dur = _MARKET_TYPE_DURATION_SECS.get(market_type)

    if total_dur is not None:
        # ── Bucket market: lifecycle-relative ────────────────────────────────
        frac = min(1.0, tte_secs_c / total_dur)   # 1.0 = just opened, 0.0 = expiry
        if frac < 0.10:          # imminent expiry — gamma spikes, adverse selection
            return 0.0
        if frac < 0.25:          # final approach: 0 → 12 pts
            return (frac - 0.10) / (0.25 - 0.10) * 12.0
        if frac < 0.50:          # lower sweet-spot: 12 → 25 pts
            return 12.0 + (frac - 0.25) / (0.50 - 0.25) * 13.0
        if frac <= 0.85:         # sweet-spot: full 25 pts
            return 25.0
        # 85–100% (brand-new): book thin, fewer takers registered; slight discount
        return 25.0 - (frac - 0.85) / (1.00 - 0.85) * 3.0   # 25 → 22 pts

    else:
        # ── Milestone / unknown: absolute TTE (original logic) ───────────────
        tte_days = tte_secs_c / 86_400.0
        if tte_days < 0.25:      # <6 h
            return 0.0
        if tte_days < 0.5:       # 6 h – 12 h: capped at 12 pts
            return (tte_days - 0.25) / (0.5 - 0.25) * 12.0
        if tte_days < 1.0:       # 12 h – 1 d
            return 12.0 + (tte_days - 0.5) / (1.0 - 0.5) * 13.0
        if tte_days <= 5.0:
            return 25.0
        if tte_days <= 14.0:
            return 25.0 - (tte_days - 5.0) / (14.0 - 5.0) * 23.0  # 25 → 2
        return 2.0


# ── Mispricing scorer ─────────────────────────────────────────────────────────

def score_mispricing(sig: MispricingSignal, volume_24hr: float,
                     market_type: str = "") -> float:
    """
    Score a mispricing signal 0–100. Higher = stronger evidence and better
    capital-efficiency characteristics.

    Args:
        sig:         MispricingSignal from the scanner.
        volume_24hr: PM 24h USD volume for this market (from PMMarket.volume_24hr).
        market_type: PMMarket.market_type string (used for type-aware volume cap).

    Returns:
        Score 0.0–100.0 (rounded to 1 decimal place).
    """
    w_edge      = config.SCORE_WEIGHT_EDGE
    w_source    = config.SCORE_WEIGHT_SOURCE
    w_timing    = config.SCORE_WEIGHT_TIMING
    w_liquidity = config.SCORE_WEIGHT_LIQUIDITY

    # ── Factor 1: Edge Ratio (40 pts) ────────────────────────────────────────
    # deviation / fee_hurdle shows how many times above the break-even threshold.
    # Nonlinear: 1× hurdle = 0 pts; ≥4× hurdle = 40 pts.
    if sig.fee_hurdle > 0:
        ratio = sig.deviation / sig.fee_hurdle
        edge_raw = min((ratio - 1.0) / 3.0, 1.0) * 40.0
    else:
        edge_raw = 0.0
    edge_pts = max(0.0, edge_raw) * w_edge

    # ── Factor 2: Source Confidence (25 pts) ─────────────────────────────────
    source_base = {
        "kalshi_confirmed": 25.0,
        "nd2_only":         15.0,
        "kalshi_only":      10.0,
    }.get(sig.signal_source, 15.0)

    # Bonus: up to +7 pts when Kalshi also shows a large spread.
    # sqrt curve for diminishing returns: 8¢ gap >> 4¢ gap, but 20¢ ≈ 12¢.
    kalshi_bonus = 0.0
    if sig.kalshi_deviation is not None and sig.kalshi_deviation > config.KALSHI_MIN_DEVIATION:
        bonus_range = 0.10 - config.KALSHI_MIN_DEVIATION
        if bonus_range > 0:
            frac = max(0.0, (sig.kalshi_deviation - config.KALSHI_MIN_DEVIATION) / bonus_range)
            kalshi_bonus = math.sqrt(min(frac, 1.0)) * 7.0

    source_pts = min(source_base + kalshi_bonus, 25.0) * w_source

    # ── Factor 3: Timing Quality (20 pts) ────────────────────────────────────
    timing_pts = _tte_pts_mispricing(sig.tte_years) * w_timing

    # ── Factor 4: Liquidity (15 pts) ─────────────────────────────────────────
    liquidity_pts = _volume_pts(volume_24hr, 15.0, market_type) * w_liquidity

    raw = edge_pts + source_pts + timing_pts + liquidity_pts

    # Normalise by theoretical max so score=100 for a perfect signal at any weights
    theoretical_max = 40.0*w_edge + 25.0*w_source + 20.0*w_timing + 15.0*w_liquidity
    normalised = (raw / theoretical_max * 100.0) if theoretical_max > 0 else raw
    normalised = min(normalised, 100.0)

    _score_log.debug(
        "score_mispricing",
        edge_pts=round(edge_pts, 2),
        source_pts=round(source_pts, 2),
        timing_pts=round(timing_pts, 2),
        liquidity_pts=round(liquidity_pts, 2),
        raw=round(raw, 2),
        score=round(normalised, 1),
    )
    return round(normalised, 1)


# ── Maker scorer ──────────────────────────────────────────────────────────────

def score_maker(sig: MakerSignal, volume_24hr: float, tte_secs: float,
                market_type: str = "") -> float:
    """
    Score a maker signal 0–100. Higher = better expected fill-weighted P&L.

    Args:
        sig:         MakerSignal from _evaluate_signal().
        volume_24hr: PM 24h USD volume (from PMMarket.volume_24hr).
        tte_secs:    Remaining time to market resolution in seconds.
        market_type: PMMarket.market_type string (used for type-aware volume cap).

    Returns:
        Score 0.0–100.0 (rounded to 1 decimal place).
    """
    w_edge      = config.SCORE_WEIGHT_EDGE
    w_volume    = config.SCORE_WEIGHT_LIQUIDITY   # volume = fill probability proxy
    w_timing    = config.SCORE_WEIGHT_TIMING
    w_balance   = config.SCORE_WEIGHT_SOURCE      # price balance = inventory risk proxy

    # ── Factor 1: Effective Edge (30 pts) ────────────────────────────────────
    # Directly proportional to expected P&L per filled contract.
    # 2%+ effective edge = max. (Most realistic markets are 0.5–2%)
    edge_pts = min(sig.effective_edge / 0.02, 1.0) * 30.0 * w_edge

    # ── Factor 2: Volume / Fill Probability (35 pts) ─────────────────────────
    # Maker profit requires getting filled. No takers = zero P&L.
    # Type-aware cap prevents $8K daily markets from outscoring $8K 5m markets
    # (same absolute vol but very different relative liquidity within each type).
    volume_pts = _volume_pts(volume_24hr, 35.0, market_type) * w_volume

    # ── Factor 3: TTE Quality (25 pts) ───────────────────────────────────────
    # Lifecycle-relative for bucket markets; absolute for milestone.
    tte_pts = _tte_pts_maker(tte_secs, market_type) * w_timing

    # ── Factor 4: Price Balance (10 pts) ─────────────────────────────────────
    # Markets near 50¢ fill both sides (symmetric book = earn full spread).
    # Steeper exponent (1.5): mid=0.20 scores ~2 pts (down from 3.3 pts linear),
    # reflecting higher adverse-selection risk in deep OTM/ITM ranges.
    dist = abs(sig.mid - 0.5)
    balance_raw = max(0.0, 1.0 - (dist / 0.35) ** 1.5) * 10.0
    balance_pts = balance_raw * w_balance

    # ── Capital Velocity bonus (0–5 pts) ─────────────────────────────────────
    # Extra reward when BOTH volume is high AND market is in its prime lifecycle
    # zone (25%–85% of life remaining) — maximises fill probability and capital
    # revolve rate simultaneously.
    cap = _VOLUME_TYPE_CAP.get(market_type, _VOLUME_DEFAULT_CAP)
    _vol_frac = min(math.log10(max(float(volume_24hr or 0.0), _VOLUME_DUST_FLOOR)) / math.log10(cap), 1.0)
    _total_dur = _MARKET_TYPE_DURATION_SECS.get(market_type)
    if _total_dur:
        # Bucket market: use lifecycle fraction
        _lc_frac = min(1.0, max(0.0, tte_secs) / _total_dur)
        # Prime zone 25%–85%; taper off outside it
        _tte_vel_frac = 1.0 if 0.25 <= _lc_frac <= 0.85 else max(
            0.0, 1.0 - abs(_lc_frac - 0.55) / 0.55
        )
    else:
        # Milestone: 1–5d absolute sweet-spot
        _tte_days = max(0.0, tte_secs) / 86_400.0
        _tte_vel_frac = 1.0 if 1.0 <= _tte_days <= 5.0 else max(0.0, 1.0 - abs(_tte_days - 3.0) / 4.0)
    cap_vel_pts = math.sqrt(_vol_frac * _tte_vel_frac) * 5.0

    raw = edge_pts + volume_pts + tte_pts + balance_pts + cap_vel_pts

    # Normalise by theoretical max so score=100 for a perfect signal at any weights.
    # The cap_vel bonus (5 pts) is always unweighted; include it in max.
    theoretical_max = 30.0*w_edge + 35.0*w_volume + 25.0*w_timing + 10.0*w_balance + 5.0
    normalised = (raw / theoretical_max * 100.0) if theoretical_max > 0 else raw
    normalised = min(normalised, 100.0)

    _score_log.debug(
        "score_maker",
        market=sig.market_id[:16] if sig.market_id else "",
        edge_pts=round(edge_pts, 2),
        volume_pts=round(volume_pts, 2),
        tte_pts=round(tte_pts, 2),
        balance_pts=round(balance_pts, 2),
        cap_vel_pts=round(cap_vel_pts, 2),
        raw=round(raw, 2),
        score=round(normalised, 1),
    )
    return round(normalised, 1)

"""
models/feature_snapshot.py — ML-04 (Phase 2)

Converts a raw CSV row dict (from on_fills.csv, momentum_fills.csv, or trades.csv)
into a feature vector dict ready for XGBClassifier inference.

Missing features are filled with -999 (the same sentinel used during training in
train_model.py).  The caller is responsible for selecting the correct feature list
(MODEL_B_FEATURES or MODEL_A_FEATURES) and converting the dict to a DataFrame.

These lists must stay in sync with analysis/train_model.py.
"""

from __future__ import annotations

import datetime
import time
from typing import Any

# ── Feature lists (must match train_model.py) ────────────────────────────────

# Model B — exit quality gate
# NOTE: tte_seconds_at_entry is intentionally excluded — feature_builder.py does
# not populate it for Opening Neutral rows (all zero, zero variance).
MODEL_B_FEATURES: list[str] = [
    # Entry-time: CLOB depth signal (8x difference between wrong vs correct exits)
    "clob_yes_bid_depth_5",    # YES-leg top-5 bid depth in USDC at entry — strongest signal
    "clob_yes_best_bid",       # YES best bid at entry
    "on_clob_no_bid_depth_5",  # NO-leg bid depth at entry
    # Entry-time: fill quality
    "on_loser_confidence_score",
    "on_loser_fill_price",
    "on_loser_fill_time_secs",
    # Entry-time: oracle / IV context (sparse but real when available)
    "deribit_iv",
    "on_price_to_beat",
    "on_hl_mark_price",
    # Time context
    "hour_utc",
    # v5: exit-time CLOB signals (XGBoost handles NaN natively — inert until populated)
    "on_winner_bid_at_exit",
    "on_loser_bid_at_exit",
    "on_oracle_delta_at_exit",
    "on_tte_at_exit_secs",
    # v6: early-warning SL signals — ADD once 200+ exits have been logged with
    # signal values (currently null in all historical rows — held back to avoid
    # corrupting the model).  Join on market_id + exit tick ts from momentum_ticks.csv.
    # "exit_hl_mark_div_pct",       # HL mark divergence at exit tick (Signal A)
    # "exit_hl_depth_imbalance",    # HL book position-adjusted imbalance at exit (Signal B)
    # "exit_tte_secs",              # TTE at exit tick (context for signal strength)
]

# Model A — entry quality / sizing
# NOTE: Only Momentum-specific features are included here.  ON-specific columns
# (on_yes_depth_share, on_clob_no_bid_depth_5, on_funding_rate, on_price_to_beat,
# on_hl_mark_price) were removed because they are always -999 for momentum rows
# and the trained model does not include them in its feature set.
# SYNC: must match MODEL_A_FEATURES in analysis/train_model.py.
MODEL_A_FEATURES: list[str] = [
    "mom_z_score",
    "mom_effective_z",
    "mom_sigma_ann",
    "mom_kelly_f",
    "mom_kelly_win_prob",
    "mom_kelly_multiplier",
    "oracle_delta_pct",
    # deribit_iv excluded — 87.5% null for momentum rows, dropped in model_a_v0 retrain
    "mom_yes_depth_share",
    "clob_yes_bid_depth_5",   # top-5 YES bid depth in USDC at entry
    "mom_funding_rate",
    "mom_tte_seconds",
    "tte_seconds_at_entry",
    "hour_utc",
    "day_of_week",
    "vol_regime_high",
    "mom_twap_dev_bps",
    "mom_signal_delta_pct",
    "mom_hl_depth_imbalance",  # HL perp book imbalance at entry
    "is_bucket_5m",
    "is_bucket_1h",
    "is_bucket_15m",
    "is_bucket_4h",
]

_SENTINEL = -999.0


def _safe_float(value: Any, fallback: float = _SENTINEL) -> float:
    """Convert a value to float; return fallback on any failure."""
    if value is None or value == "" or value != value:  # nan check
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def build_exit_snapshot(row: dict) -> dict[str, float]:
    """
    Build a Model B feature vector from a trades.csv row dict.

    Keys sourced from trades.csv:
      oracle_delta_pct, deribit_iv, implied_prob (= price column),
      tte_seconds_at_entry, entry_timestamp → hour_utc

    Keys sourced from on_fills.csv (via join on market_id/pair_id):
      on_yes_depth_share, on_loser_confidence_score, on_loser_fill_price,
      on_loser_fill_time_secs, on_funding_rate, on_combined_cost

    CLOB keys (best-effort from row):
      clob_yes_best_bid, clob_yes_bid_depth_5
    """
    # Derive hour_utc from entry_timestamp if present
    hour_utc: float = _SENTINEL
    ts_raw = row.get("entry_timestamp") or row.get("timestamp")
    if ts_raw is not None:
        try:
            ts_f = float(ts_raw)
            hour_utc = float(datetime.datetime.utcfromtimestamp(ts_f).hour)
        except (ValueError, TypeError, OSError):
            pass

    # Map trades.csv / on_fills columns to Model B feature names
    feature_map: dict[str, Any] = {
        "oracle_delta_pct":         row.get("oracle_delta_pct") or row.get("entry_deviation"),
        "deribit_iv":               row.get("deribit_iv"),
        "implied_prob":             row.get("implied_prob") or row.get("price"),
        "on_yes_depth_share":       row.get("on_yes_depth_share") or row.get("yes_depth_share"),
        "on_loser_confidence_score": row.get("on_loser_confidence_score") or row.get("loser_confidence_score"),
        "on_loser_fill_price":      row.get("on_loser_fill_price") or row.get("loser_fill_price"),
        "on_loser_fill_time_secs":  row.get("on_loser_fill_time_secs") or row.get("loser_fill_time_secs"),
        "tte_seconds_at_entry":     row.get("tte_seconds_at_entry") or row.get("tte_seconds"),
        "hour_utc":                 hour_utc if hour_utc != _SENTINEL else row.get("hour_utc"),
        "on_funding_rate":          row.get("on_funding_rate") or row.get("funding_rate"),
        "on_combined_cost":         row.get("on_combined_cost") or row.get("combined_cost"),
        "clob_yes_best_bid":        row.get("clob_yes_best_bid"),
        "clob_yes_bid_depth_5":     row.get("clob_yes_bid_depth_5"),
        # ON-only features (v3)
        "on_price_to_beat":         row.get("on_price_to_beat") or row.get("price_to_beat"),
        "on_clob_no_bid_depth_5":   row.get("on_clob_no_bid_depth_5") or row.get("clob_no_bid_depth_5"),
        "on_hl_mark_price":         row.get("on_hl_mark_price") or row.get("hl_mark_price"),
        # v5: exit-time signals (populated by scanner v5; NaN when absent)
        "on_winner_bid_at_exit":     row.get("on_winner_bid_at_exit") or row.get("winner_bid_at_exit"),
        "on_loser_bid_at_exit":      row.get("on_loser_bid_at_exit") or row.get("loser_bid_at_exit"),
        "on_oracle_delta_at_exit":   row.get("on_oracle_delta_at_exit") or row.get("oracle_delta_at_exit"),
        "on_tte_at_exit_secs":       row.get("on_tte_at_exit_secs") or row.get("tte_at_exit_secs"),
    }

    return {feat: _safe_float(feature_map.get(feat)) for feat in MODEL_B_FEATURES}


def build_entry_snapshot(row: dict) -> dict[str, float]:
    """
    Build a Model A feature vector from a momentum_fills.csv entry row dict.

    Keys sourced from momentum_fills.csv:
      signal_obs_z → mom_z_score, effective_z → mom_effective_z,
      signal_sigma_ann → mom_sigma_ann, kelly_f, kelly_win_prob, kelly_multiplier,
      tte_seconds → mom_tte_seconds, yes_depth_share → mom_yes_depth_share,
      funding_rate → mom_funding_rate, twap_dev_bps, signal_delta_pct,
      vol_regime → vol_regime_high, hour_utc, day_of_week, oracle_delta_pct,
      clob_yes_bid_depth_5, hl_entry_imbalance → mom_hl_depth_imbalance,
      market_type → is_bucket_5m/1h/15m/4h
    """
    # Derive hour_utc, day_of_week from timestamp
    hour_utc: float = _SENTINEL
    day_of_week: float = _SENTINEL
    ts_raw = row.get("timestamp")
    if ts_raw is not None:
        try:
            ts_f = float(ts_raw)
            dt = datetime.datetime.utcfromtimestamp(ts_f)
            hour_utc = float(dt.hour)
            day_of_week = float(dt.weekday())
        except (ValueError, TypeError, OSError):
            pass

    # vol_regime_high: 1.0 if HIGH else 0.0 else -999 if missing
    vol_regime_raw = row.get("vol_regime")
    if vol_regime_raw is not None and str(vol_regime_raw).upper() == "HIGH":
        vol_regime_high: float = 1.0
    elif vol_regime_raw is not None and vol_regime_raw != "":
        vol_regime_high = 0.0
    else:
        vol_regime_high = _SENTINEL

    # Derive bucket-type boolean flags from market_type
    market_type_raw = str(row.get("market_type") or "").lower()
    _bucket_map = {
        "is_bucket_5m":  "bucket_5m",
        "is_bucket_1h":  "bucket_1h",
        "is_bucket_15m": "bucket_15m",
        "is_bucket_4h":  "bucket_4h",
    }

    feature_map: dict[str, Any] = {
        "mom_z_score":            row.get("signal_obs_z"),
        "mom_effective_z":        row.get("effective_z"),
        "mom_sigma_ann":          row.get("signal_sigma_ann"),
        "mom_kelly_f":            row.get("kelly_f"),
        "mom_kelly_win_prob":     row.get("kelly_win_prob"),
        "mom_kelly_multiplier":   row.get("kelly_multiplier"),
        "oracle_delta_pct":       row.get("oracle_delta_pct"),
        "mom_yes_depth_share":    row.get("yes_depth_share"),
        "clob_yes_bid_depth_5":   row.get("clob_yes_bid_depth_5"),
        "mom_funding_rate":       row.get("funding_rate"),
        "mom_tte_seconds":        row.get("tte_seconds"),
        "tte_seconds_at_entry":   row.get("tte_seconds_at_entry") or row.get("tte_seconds"),
        "hour_utc":               hour_utc if hour_utc != _SENTINEL else row.get("hour_utc"),
        "day_of_week":            day_of_week if day_of_week != _SENTINEL else row.get("day_of_week"),
        "vol_regime_high":        vol_regime_high,
        "mom_twap_dev_bps":       row.get("twap_dev_bps"),
        "mom_signal_delta_pct":   row.get("signal_delta_pct"),
        "mom_hl_depth_imbalance": row.get("hl_entry_imbalance"),
        # Bucket-type one-hots (1.0 / 0.0; _SENTINEL when market_type missing)
        **{flag: (1.0 if market_type_raw == bucket else 0.0)
           if market_type_raw else None
           for flag, bucket in _bucket_map.items()},
    }

    return {feat: _safe_float(feature_map.get(feat)) for feat in MODEL_A_FEATURES}

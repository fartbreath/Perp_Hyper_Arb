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
MODEL_B_FEATURES: list[str] = [
    "oracle_delta_pct",
    "deribit_iv",
    "implied_prob",
    "on_yes_depth_share",
    "on_loser_confidence_score",
    "on_loser_fill_price",
    "on_loser_fill_time_secs",
    "tte_seconds_at_entry",
    "hour_utc",
    "on_funding_rate",
    "on_combined_cost",
    "clob_yes_best_bid",
    "clob_yes_bid_depth_5",
]

# Model A — entry quality / sizing
MODEL_A_FEATURES: list[str] = [
    "mom_z_score",
    "mom_effective_z",
    "mom_sigma_ann",
    "mom_kelly_f",
    "mom_kelly_win_prob",
    "mom_kelly_multiplier",
    "oracle_delta_pct",
    "deribit_iv",
    "mom_yes_depth_share",
    "on_yes_depth_share",
    "mom_funding_rate",
    "on_funding_rate",
    "mom_tte_seconds",
    "tte_seconds_at_entry",
    "hour_utc",
    "day_of_week",
    "vol_regime_high",
    "mom_twap_dev_bps",
    "mom_signal_delta_pct",
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
      deribit_iv, on_yes_depth_share, on_funding_rate
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

    feature_map: dict[str, Any] = {
        "mom_z_score":          row.get("signal_obs_z"),
        "mom_effective_z":      row.get("effective_z"),
        "mom_sigma_ann":        row.get("signal_sigma_ann"),
        "mom_kelly_f":          row.get("kelly_f"),
        "mom_kelly_win_prob":   row.get("kelly_win_prob"),
        "mom_kelly_multiplier": row.get("kelly_multiplier"),
        "oracle_delta_pct":     row.get("oracle_delta_pct"),
        "deribit_iv":           row.get("deribit_iv"),
        "mom_yes_depth_share":  row.get("yes_depth_share"),
        "on_yes_depth_share":   row.get("on_yes_depth_share"),
        "mom_funding_rate":     row.get("funding_rate"),
        "on_funding_rate":      row.get("on_funding_rate"),
        "mom_tte_seconds":      row.get("tte_seconds"),
        "tte_seconds_at_entry": row.get("tte_seconds_at_entry") or row.get("tte_seconds"),
        "hour_utc":             hour_utc if hour_utc != _SENTINEL else row.get("hour_utc"),
        "day_of_week":          day_of_week if day_of_week != _SENTINEL else row.get("day_of_week"),
        "vol_regime_high":      vol_regime_high,
        "mom_twap_dev_bps":     row.get("twap_dev_bps"),
        "mom_signal_delta_pct": row.get("signal_delta_pct"),
    }

    return {feat: _safe_float(feature_map.get(feat)) for feat in MODEL_A_FEATURES}

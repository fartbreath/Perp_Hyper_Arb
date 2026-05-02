"""
strategies.Momentum.signal — MomentumSignal dataclass.

Kept lightweight so api_server.py and tests can import without pulling in
the full scanner (aiohttp, DeribitFetcher, etc.).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MomentumSignal:
    # ── Market identification ──────────────────────────────────────────────
    market_id: str          # condition_id
    market_title: str
    underlying: str         # "BTC", "ETH", etc.
    market_type: str        # "bucket_5m", "bucket_15m", "bucket_1h", "bucket_4h"

    # ── Signal core ────────────────────────────────────────────────────────
    side: str               # "YES"/"NO" for standard Yes/No markets; "UP"/"DOWN" for Up-or-Down bucket markets
    token_id: str           # CLOB token_id of the token to buy
    token_price: float      # current price of the token to buy (0.80–0.90)
    p_yes: float            # YES-token CLOB mid at signal time
    p_no: float             # NO-token CLOB mid at signal time (opposite side)

    # ── Delta & threshold ──────────────────────────────────────────────────
    delta_pct: float        # signed % delta toward winning direction (> threshold)
    threshold_pct: float    # computed entry threshold y (MOMENTUM_VOL_Z_SCORE * sigma_tau)

    # ── Supporting market data ─────────────────────────────────────────────
    spot: float             # HL mid at signal time
    strike: float           # strike parsed from market title
    tte_seconds: float      # seconds to market expiry at signal time

    # ── Volatility ────────────────────────────────────────────────────────
    sigma_ann: float        # annualized vol used for threshold computation
    vol_source: str         # "deribit_atm" | "hl_realized" | "unknown"
    vol_z_score: float = 1.6449  # z-score used for this entry; anchors edge_pct computation

    # ── Metadata ──────────────────────────────────────────────────────────
    timestamp: float = field(default_factory=time.time)
    score: float = 0.0      # reserved for future scoring / filtering

    # ── Path history (Kelly TTE floor / persistence — Phase A) ────────────
    signal_valid_since_ts: float = field(default_factory=time.time)  # Unix ts when signal first cleared all gates

    # ── M-12: gate context at entry time (set by scanner after all gates pass) ──
    entry_funding_rate: Optional[float] = None      # HL funding rate at scan time
    entry_yes_depth_share: Optional[float] = None   # YES bid depth share at scan time
    entry_twap_dev_bps: Optional[float] = None      # oracle TWAP deviation in bps at scan time
    entry_vol_regime: str = "UNKNOWN"               # vol regime at scan time ("HIGH"/"LOW"/"UNKNOWN")

    @property
    def edge_pct(self) -> float:
        """
        Approximate edge in probability terms.

        The delta_pct / threshold_pct ratio > 1 by construction at signal time.
        The excess above the threshold translates roughly to fair-prob minus token_price.
        This is an approximation — use for logging/display only.
        """
        # At threshold y = z * sigma_tau * 100, fair_prob ~ N(z) = 95% (default).
        # Edge ≈ fair_prob - token_price.  Clip to [0, 1].
        import math
        excess_z = (self.delta_pct - self.threshold_pct) / (self.sigma_ann * 100 + 1e-9)
        fair_prob = min(0.9999, 0.5 * (1.0 + math.erf((self.vol_z_score + excess_z) / 2 ** 0.5)))
        return max(0.0, fair_prob - self.token_price)

    def summary(self) -> str:
        return (
            f"[MOMENTUM] {self.underlying} {self.side} @ {self.token_price:.2f}\n"
            f"  Market : {self.market_title[:70]}\n"
            f"  Delta  : {self.delta_pct:+.3f}% ≥ threshold {self.threshold_pct:.3f}%\n"
            f"  Spot   : {self.spot:.4f} | Strike: {self.strike:.4f} | "
            f"TTE: {self.tte_seconds:.0f}s ({self.tte_seconds/60:.1f}min)\n"
            f"  Vol    : {self.sigma_ann:.1%} ann ({self.vol_source})\n"
            f"  Edge   : ~{self.edge_pct:.1%}"
        )

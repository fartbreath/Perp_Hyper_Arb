"""
agent.py — Ollama LLM Agent Decision Layer

Architecture:
  1.  In SHADOW mode (config.AGENT_AUTO=False):
        - Agent evaluates every signal and logs its recommendation
        - Human still must approve/deny in CLI prompt
        - Validates itself: correct recommendations increment trust_score
  2.  In AUTO mode (config.AGENT_AUTO=True, requires trust_score >= 10):
        - Agent recommendation IS the decision
        - Hard overrides always block (risk limits, drawdown, paper mode)
  3.  Hard overrides (applied regardless of mode):
        - PAPER_TRADING=True → agent output is advisory only; no real orders
        - RiskEngine says no → blocked unconditionally
        - Drawdown near HARD_STOP → HALT command forces shutdown

Agent tool schema (JSON output):
  {
    "decision": "EXECUTE" | "SKIP" | "HALT",
    "confidence": 0.0-1.0,
    "reason": "...",
    "suggested_size_pct": 0.0-1.0   # fraction of max per-market allocation
  }

If Ollama is unavailable, falls back to SKIP (fail-safe).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Optional, Callable, Awaitable

import config
from logger import get_bot_logger
from risk import RiskEngine
from strategies.mispricing.signals import MispricingSignal

log = get_bot_logger(__name__)

# ---------------------------------------------------------------------------
# Signal type union — whatever the agent can evaluate
# ---------------------------------------------------------------------------

SignalType = MispricingSignal  # extend with MakerSignal etc. as needed


@dataclass
class AgentDecision:
    decision: str            # "EXECUTE" | "SKIP" | "HALT"
    confidence: float        # 0.0-1.0
    reason: str
    suggested_size_pct: float = 0.5
    override_applied: str = ""  # set to reason if a hard override fired
    latency_ms: float = 0.0
    timestamp: float = field(default_factory=time.time)

    @property
    def is_execute(self) -> bool:
        return self.decision == "EXECUTE" and not self.override_applied

    @property
    def is_halt(self) -> bool:
        return self.decision == "HALT"


# ---------------------------------------------------------------------------
# Prompt builder
# ---------------------------------------------------------------------------

def _build_prompt(signal: MispricingSignal, risk_state: dict) -> str:
    return f"""You are a quantitative trading agent assessing a Polymarket binary option mispricing signal.

SIGNAL:
  Market:        {signal.market_title}
  PM price:      {signal.pm_price:.3f}
  BS implied:    {signal.implied_prob:.3f}
  Deviation:     {signal.deviation:.3f}  (fee hurdle: {signal.fee_hurdle:.4f})
  Direction:     {signal.direction}
  Deribit IV:    {signal.deribit_iv:.1%}
  Instrument:    {signal.deribit_instrument}
  Strike:        ${signal.strike:,.0f}
  TTE (years):   {signal.tte_years:.3f}
  Spot:          ${signal.spot_price:,.0f}
  Fees enabled:  {signal.fees_enabled}

RISK STATE:
  Total PM exposure (USD): {risk_state.get('total_pm_exposure', 0):.0f} / {config.MAX_TOTAL_PM_EXPOSURE}
  Open position count:     {risk_state.get('open_positions', 0)} / {config.MAX_CONCURRENT_POSITIONS}
  Realized P&L (USD):      {risk_state.get('realized_pnl', 0):.2f}
  Hard stop threshold:     -{config.HARD_STOP_DRAWDOWN:.0f}

INSTRUCTIONS:
Evaluate whether to EXECUTE, SKIP, or HALT.

- EXECUTE: deviation is genuine, fees are covered, risk limits allow it.
- SKIP:    deviation too small, IV unreliable, risk limits too tight, or uncertainty too high.
- HALT:    P&L near hard stop or something looks dangerously wrong.

Respond ONLY with a valid JSON object, no other text:
{{
  "decision": "EXECUTE" | "SKIP" | "HALT",
  "confidence": <0.0-1.0>,
  "reason": "<one sentence>",
  "suggested_size_pct": <0.0-1.0>
}}"""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class AgentDecisionLayer:
    """
    Wraps Ollama to make structured trading decisions.

    Usage:
        agent = AgentDecisionLayer(risk_engine)
        decision = await agent.evaluate(signal)
    """

    def __init__(self, risk: RiskEngine) -> None:
        self._risk = risk
        self.trust_score: int = 0    # increments when shadow predictions match outcomes
        self._shadow_log: list[dict] = []

    # ── Public API ──────────────────────────────────────────────────────────

    async def evaluate(self, signal: MispricingSignal) -> AgentDecision:
        """
        Evaluate a signal and return a decision.
        Hard overrides are applied before and after LLM call.
        """
        t0 = time.monotonic()

        # 1. Hard overrides — pre-LLM
        pre_override = self._check_hard_overrides(signal)
        if pre_override:
            return pre_override

        # 2. Ask Ollama
        risk_state = self._risk.get_state()
        prompt = _build_prompt(signal, risk_state)
        raw = await self._call_ollama(prompt)
        latency_ms = (time.monotonic() - t0) * 1000

        # 3. Parse response
        decision = self._parse_response(raw, latency_ms)

        # 4. Hard overrides — post-LLM (cap size if near limits)
        decision = self._apply_post_overrides(decision, signal)

        # 5. Shadow mode logging
        if not config.AGENT_AUTO:
            log.info(
                "SHADOW decision (auto=off)",
                decision=decision.decision,
                confidence=round(decision.confidence, 4),
                reason=decision.reason,
                latency_ms=round(latency_ms, 1),
            )
            self._shadow_log.append({
                "signal": signal.market_title,
                "agent_decision": decision.decision,
                "confidence": decision.confidence,
                "timestamp": time.time(),
            })
        else:
            log.info(
                "AUTO decision",
                decision=decision.decision,
                confidence=round(decision.confidence, 4),
                reason=decision.reason,
                latency_ms=round(latency_ms, 1),
            )

        return decision

    def record_outcome(self, market_title: str, was_profitable: bool) -> None:
        """
        Call after a trade resolves. Finds the most recent shadow-logged
        decision for this market and increments trust_score if agent was correct.
        """
        for entry in reversed(self._shadow_log):
            if entry["signal"] == market_title:
                agent_said_execute = entry["agent_decision"] == "EXECUTE"
                if agent_said_execute == was_profitable:
                    self.trust_score += 1
                    log.info("Trust score increased", score=self.trust_score)
                else:
                    log.info("Trust score unchanged", score=self.trust_score)
                return

    def is_auto_eligible(self) -> bool:
        """True when agent has enough validated decisions to run in auto mode."""
        return self.trust_score >= config.AGENT_MIN_TRUST_SCORE

    def get_shadow_log(self) -> list[dict]:
        return list(self._shadow_log)

    # ── Ollama call ─────────────────────────────────────────────────────────

    async def _call_ollama(self, prompt: str) -> str:
        """
        Send prompt to local Ollama. Returns raw text response.
        Falls back to "" on any error (caller will produce SKIP).
        """
        try:
            import ollama  # type: ignore
            response = await asyncio.to_thread(
                ollama.chat,
                model=config.AGENT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                options={"temperature": 0.1, "num_predict": 200},
            )
            return response["message"]["content"]
        except ImportError:
            log.error("ollama package not installed")
            return ""
        except Exception as exc:
            log.error("Ollama call failed", exc=str(exc))
            return ""

    # ── Response parsing ────────────────────────────────────────────────────

    def _parse_response(self, raw: str, latency_ms: float) -> AgentDecision:
        """
        Parse the JSON blob from Ollama. Returns SKIP on any parse failure
        (fail-safe: we never accidentally execute on a bad response).
        """
        if not raw:
            return AgentDecision(
                decision="SKIP",
                confidence=0.0,
                reason="Ollama unavailable — fail-safe SKIP",
                latency_ms=latency_ms,
            )

        # Strip markdown code blocks if model wrapped it
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            lines = cleaned.split("\n")
            cleaned = "\n".join(lines[1:-1]) if len(lines) > 2 else cleaned

        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError:
            # Try to find the first { ... } block
            import re
            match = re.search(r"\{.*?\}", cleaned, re.DOTALL)
            if match:
                try:
                    obj = json.loads(match.group())
                except json.JSONDecodeError:
                    obj = {}
            else:
                obj = {}

        decision_str = obj.get("decision", "SKIP").upper()
        if decision_str not in ("EXECUTE", "SKIP", "HALT"):
            decision_str = "SKIP"

        confidence = float(obj.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))

        reason = str(obj.get("reason", "no reason provided"))[:200]
        size_pct = float(obj.get("suggested_size_pct", 0.5))
        size_pct = max(0.0, min(1.0, size_pct))

        return AgentDecision(
            decision=decision_str,
            confidence=confidence,
            reason=reason,
            suggested_size_pct=size_pct,
            latency_ms=latency_ms,
        )

    # ── Hard overrides ──────────────────────────────────────────────────────

    def _check_hard_overrides(self, signal: MispricingSignal) -> Optional[AgentDecision]:
        """
        Return a blocking decision if a hard override condition is met.
        Returns None if no override applies.
        """
        state = self._risk.get_state()

        # 1. Near hard stop → HALT
        pnl = state.get("realized_pnl", 0.0)
        halt_threshold = -(config.HARD_STOP_DRAWDOWN * 0.90)
        if pnl <= halt_threshold:
            reason = f"Drawdown ${pnl:.2f} near hard stop ${-config.HARD_STOP_DRAWDOWN:.0f}"
            log.critical("HARD OVERRIDE: HALT", reason=reason)
            return AgentDecision(
                decision="HALT",
                confidence=1.0,
                reason=reason,
                override_applied="drawdown_near_hard_stop",
            )

        # 2. Risk engine blocks this trade
        ok, risk_reason = self._risk.can_open(signal.market_id, signal.suggested_size_usd)
        if not ok:
            return AgentDecision(
                decision="SKIP",
                confidence=1.0,
                reason=f"Risk engine blocked: {risk_reason}",
                override_applied="risk_engine",
            )

        # 3. Paper trading — still run through logic but flag it
        # (actual order placement in pm_client / hl_client handles paper mode)
        return None

    def _apply_post_overrides(
        self, decision: AgentDecision, signal: MispricingSignal
    ) -> AgentDecision:
        """Cap sizing if risk is near limits."""
        state = self._risk.get_state()
        remaining = config.MAX_TOTAL_PM_EXPOSURE - state.get("total_pm_exposure", 0.0)
        if remaining < signal.suggested_size_usd:
            adjusted = remaining / config.MAX_PM_EXPOSURE_PER_MARKET
            decision.suggested_size_pct = min(decision.suggested_size_pct, adjusted)
            decision.override_applied = "size_capped_near_limit"
        return decision

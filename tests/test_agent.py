"""
tests/test_agent.py — Unit tests for agent.py

Run:  pytest tests/test_agent.py -v
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import config
config.PAPER_TRADING = True
config.AGENT_AUTO = False
config.AGENT_MIN_TRUST_SCORE = 10

from agent import AgentDecisionLayer, AgentDecision, _build_prompt
from strategies.mispricing.signals import MispricingSignal
from risk import RiskEngine


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _make_signal(
    pm_price=0.25,
    implied_prob=0.55,
    market_id="cond_001",
    title="BTC $120k by Dec 2025",
    deviation=0.30,
    fee_hurdle=0.04,
) -> MispricingSignal:
    return MispricingSignal(
        market_id=market_id,
        market_title=title,
        underlying="BTC",
        pm_price=pm_price,
        implied_prob=implied_prob,
        deviation=deviation,
        direction="BUY_YES" if pm_price < implied_prob else "BUY_NO",
        fee_hurdle=fee_hurdle,
        deribit_iv=0.80,
        deribit_instrument="BTC-27DEC25-120000-C",
        spot_price=95000.0,
        strike=120000.0,
        tte_years=0.50,
        fees_enabled=False,
        suggested_size_usd=100.0,
    )


def _make_agent() -> AgentDecisionLayer:
    risk = RiskEngine()
    return AgentDecisionLayer(risk)


# ── _build_prompt ─────────────────────────────────────────────────────────────

class TestBuildPrompt:
    def test_contains_market_title(self):
        signal = _make_signal()
        prompt = _build_prompt(signal, {})
        assert "BTC $120k by Dec 2025" in prompt

    def test_contains_pm_price(self):
        signal = _make_signal(pm_price=0.25)
        prompt = _build_prompt(signal, {})
        assert "0.250" in prompt

    def test_contains_implied_prob(self):
        signal = _make_signal(implied_prob=0.55)
        prompt = _build_prompt(signal, {})
        assert "0.550" in prompt

    def test_contains_decision_instructions(self):
        signal = _make_signal()
        prompt = _build_prompt(signal, {})
        assert "EXECUTE" in prompt
        assert "SKIP" in prompt
        assert "HALT" in prompt

    def test_contains_hard_stop_info(self):
        signal = _make_signal()
        prompt = _build_prompt(signal, {})
        assert str(int(config.HARD_STOP_DRAWDOWN)) in prompt


# ── _parse_response ───────────────────────────────────────────────────────────

class TestParseResponse:
    def setup_method(self):
        self.agent = _make_agent()

    def test_valid_execute(self):
        raw = json.dumps({
            "decision": "EXECUTE",
            "confidence": 0.85,
            "reason": "Deviation exceeds fee hurdle significantly.",
            "suggested_size_pct": 0.6,
        })
        d = self.agent._parse_response(raw, latency_ms=50.0)
        assert d.decision == "EXECUTE"
        assert d.confidence == pytest.approx(0.85)
        assert d.suggested_size_pct == pytest.approx(0.6)

    def test_valid_skip(self):
        raw = json.dumps({"decision": "SKIP", "confidence": 0.3, "reason": "Too small."})
        d = self.agent._parse_response(raw, latency_ms=50.0)
        assert d.decision == "SKIP"

    def test_valid_halt(self):
        raw = json.dumps({"decision": "HALT", "confidence": 0.99, "reason": "Near drawdown limit."})
        d = self.agent._parse_response(raw, latency_ms=50.0)
        assert d.decision == "HALT"

    def test_empty_raw_returns_skip(self):
        d = self.agent._parse_response("", latency_ms=10.0)
        assert d.decision == "SKIP"
        assert "Ollama unavailable" in d.reason

    def test_malformed_json_returns_skip(self):
        d = self.agent._parse_response("not json at all", latency_ms=10.0)
        assert d.decision == "SKIP"

    def test_json_in_markdown_block_parsed(self):
        raw = '```json\n{"decision":"EXECUTE","confidence":0.9,"reason":"good","suggested_size_pct":0.5}\n```'
        d = self.agent._parse_response(raw, latency_ms=30.0)
        assert d.decision == "EXECUTE"

    def test_invalid_decision_value_defaults_skip(self):
        raw = json.dumps({"decision": "MAYBE", "confidence": 0.7, "reason": "idk"})
        d = self.agent._parse_response(raw, latency_ms=10.0)
        assert d.decision == "SKIP"

    def test_confidence_clamped_to_unit_interval(self):
        raw = json.dumps({"decision": "EXECUTE", "confidence": 5.0, "reason": "ok"})
        d = self.agent._parse_response(raw, latency_ms=10.0)
        assert d.confidence == pytest.approx(1.0)

    def test_size_pct_clamped_to_unit_interval(self):
        raw = json.dumps({"decision": "EXECUTE", "confidence": 0.8,
                           "reason": "fine", "suggested_size_pct": -0.5})
        d = self.agent._parse_response(raw, latency_ms=10.0)
        assert d.suggested_size_pct == pytest.approx(0.0)


# ── Hard overrides ─────────────────────────────────────────────────────────────

class TestHardOverrides:
    def test_halt_when_near_hard_stop(self):
        """If P&L is near -$500, override must return HALT regardless of signal."""
        risk = RiskEngine()
        # Simulate a loss near the hard stop threshold
        risk._realized_pnl = -(config.HARD_STOP_DRAWDOWN * 0.95)
        agent = AgentDecisionLayer(risk)
        signal = _make_signal()
        result = agent._check_hard_overrides(signal)
        assert result is not None
        assert result.decision == "HALT"
        assert result.override_applied != ""

    def test_skip_when_risk_engine_blocks(self):
        """If can_open returns False, must return SKIP."""
        risk = MagicMock(spec=RiskEngine)
        risk.can_open.return_value = (False, "per-market limit reached")
        risk.get_state.return_value = {"realized_pnl": 0.0, "total_pm_exposure": 0.0}
        agent = AgentDecisionLayer(risk)
        signal = _make_signal()
        result = agent._check_hard_overrides(signal)
        assert result is not None
        assert result.decision == "SKIP"
        assert result.override_applied == "risk_engine"

    def test_no_override_when_safe(self):
        """No override when P&L fine and risk engine allows."""
        risk = MagicMock(spec=RiskEngine)
        risk.can_open.return_value = (True, "")
        risk.get_state.return_value = {"realized_pnl": 10.0, "total_pm_exposure": 100.0}
        agent = AgentDecisionLayer(risk)
        signal = _make_signal()
        result = agent._check_hard_overrides(signal)
        assert result is None

    def test_halt_override_fires_before_llm(self):
        """Even if Ollama says EXECUTE, HALT override must win."""
        risk = RiskEngine()
        risk._realized_pnl = -(config.HARD_STOP_DRAWDOWN * 0.95)
        agent = AgentDecisionLayer(risk)
        decision = AgentDecision(decision="EXECUTE", confidence=0.9, reason="LLM said go")
        # Simulate post-override: but more importantly test pre-override path in evaluate()
        override = agent._check_hard_overrides(_make_signal())
        assert override.decision == "HALT"


# ── AgentDecision properties ──────────────────────────────────────────────────

class TestAgentDecisionProperties:
    def test_is_execute_true(self):
        d = AgentDecision(decision="EXECUTE", confidence=0.9, reason="ok", override_applied="")
        assert d.is_execute is True

    def test_is_execute_false_when_override(self):
        d = AgentDecision(decision="EXECUTE", confidence=0.9, reason="ok",
                          override_applied="risk_engine")
        assert d.is_execute is False

    def test_is_halt_true(self):
        d = AgentDecision(decision="HALT", confidence=1.0, reason="drawdown")
        assert d.is_halt is True

    def test_is_halt_false_for_skip(self):
        d = AgentDecision(decision="SKIP", confidence=0.5, reason="meh")
        assert d.is_halt is False


# ── Shadow mode + trust score ─────────────────────────────────────────────────

class TestShadowMode:
    def test_trust_score_increments_on_correct_prediction(self):
        agent = _make_agent()
        agent._shadow_log.append({
            "signal": "BTC $120k by Dec 2025",
            "agent_decision": "EXECUTE",
            "confidence": 0.85,
            "timestamp": time.time(),
        })
        agent.record_outcome("BTC $120k by Dec 2025", was_profitable=True)
        assert agent.trust_score == 1

    def test_trust_score_unchanged_on_wrong_prediction(self):
        agent = _make_agent()
        agent._shadow_log.append({
            "signal": "BTC $120k by Dec 2025",
            "agent_decision": "EXECUTE",
            "confidence": 0.85,
            "timestamp": time.time(),
        })
        agent.record_outcome("BTC $120k by Dec 2025", was_profitable=False)
        assert agent.trust_score == 0

    def test_is_auto_eligible_false_below_threshold(self):
        agent = _make_agent()
        agent.trust_score = 5
        assert agent.is_auto_eligible() is False

    def test_is_auto_eligible_true_at_threshold(self):
        agent = _make_agent()
        agent.trust_score = config.AGENT_MIN_TRUST_SCORE
        assert agent.is_auto_eligible() is True

    def test_shadow_log_recorded_on_evaluate(self):
        """evaluate() must append to shadow log in shadow mode."""
        config.AGENT_AUTO = False
        agent = _make_agent()
        # Mock ollama to return a SKIP immediately
        agent._call_ollama = AsyncMock(return_value=json.dumps({
            "decision": "SKIP", "confidence": 0.5, "reason": "test", "suggested_size_pct": 0.5
        }))
        import asyncio
        signal = _make_signal()
        # Run evaluate — it should not raise and should add to shadow log
        asyncio.get_event_loop().run_until_complete(agent.evaluate(signal))
        assert len(agent.get_shadow_log()) == 1
        assert agent.get_shadow_log()[0]["agent_decision"] == "SKIP"


# ── Full evaluate() integration (mocked Ollama) ──────────────────────────────

class TestEvaluateIntegration:
    def test_evaluate_returns_skip_when_ollama_unavailable(self):
        agent = _make_agent()
        agent._call_ollama = AsyncMock(return_value="")
        import asyncio
        decision = asyncio.get_event_loop().run_until_complete(
            agent.evaluate(_make_signal())
        )
        assert decision.decision == "SKIP"

    def test_evaluate_execute_passes_through(self):
        agent = _make_agent()
        agent._call_ollama = AsyncMock(return_value=json.dumps({
            "decision": "EXECUTE", "confidence": 0.88,
            "reason": "good edge", "suggested_size_pct": 0.7
        }))
        import asyncio
        decision = asyncio.get_event_loop().run_until_complete(
            agent.evaluate(_make_signal())
        )
        assert decision.decision == "EXECUTE"
        assert decision.confidence == pytest.approx(0.88)

    def test_evaluate_hard_stop_blocks_execute(self):
        """Even if Ollama says EXECUTE, hard stop override fires first."""
        risk = RiskEngine()
        risk._realized_pnl = -(config.HARD_STOP_DRAWDOWN * 0.95)
        agent = AgentDecisionLayer(risk)
        agent._call_ollama = AsyncMock(return_value=json.dumps({
            "decision": "EXECUTE", "confidence": 0.95,
            "reason": "great trade", "suggested_size_pct": 1.0
        }))
        import asyncio
        decision = asyncio.get_event_loop().run_until_complete(
            agent.evaluate(_make_signal())
        )
        assert decision.decision == "HALT"
        assert decision.override_applied != ""

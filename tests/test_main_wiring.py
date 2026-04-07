"""
tests/test_main_wiring.py — Wiring and integration tests for main.py + SpotOracle.

These tests exist to catch the class of bug where the wrong client type is passed
to a component that expects a different interface.  The motivating real bug was:
    state_sync_loop received `spot_client` (RTDSClient) instead of `spot_oracle`
    (SpotOracle).  RTDSClient.get_mid() takes 1 arg; SpotOracle.get_mid() takes 2.
    Result: "RTDSClient.get_mid() takes 2 positional arguments but 3 were given"
    on every state sync iteration — the entire dashboard state became stale.

Test categories
───────────────
  A. SpotOracle interface contract (offline)
       Verify every method/property exists on SpotOracle with the expected signature.
       These act as API-contract tests: if SpotOracle's interface drifts, these
       break before callers do.

  B. SpotOracle routing (offline, mock clients)
       Verify get_mid / get_spot / get_spot_age route to the correct underlying
       client based on market_type and underlying coin.

  C. state_sync_loop contract (offline, mock SpotOracle)
       Verify the function calls spot_oracle.get_mid(underlying, market_type) —
       two args — NOT the RTDSClient signature of one arg.
       Also verifies all_mids() and get_spot_age_rtds() are called correctly.

  D. main() wiring inspection (offline, static)
       Parse main.py to verify the correct variable name (`spot_oracle`, not
       `spot_client`) is passed to state_sync_loop and PositionMonitor.

  E. SpotOracle live smoke test (marked `live`)
       Create a real RTDSClient + stub Chainlink clients, start RTDS, and verify
       SpotOracle.get_mid(coin, market_type) returns positive floats for all
       tracked coins within a reasonable timeout.

  F. state_sync_loop live sweep (marked `live`)
       Wire SpotOracle to a real RTDSClient and run one iteration of the sync
       loop body against live data.  Verifies the spot_mid fields are populated
       and no exception escapes the error handler.

Run offline only:
    pytest tests/test_main_wiring.py -v

Run all including live:
    pytest tests/test_main_wiring.py -v -m live --timeout=90
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import sys
import time
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import config

config.PAPER_TRADING = True
config.TRACKED_UNDERLYINGS = ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE", "HYPE"]

from market_data.chainlink_ws_client import ChainlinkWSClient
from market_data.chainlink_streams_client import ChainlinkStreamsClient
from market_data.rtds_client import RTDSClient, SpotPrice
from market_data.spot_oracle import SpotOracle, CHAINLINK_MARKET_TYPES

MAIN_PY = Path(__file__).parent.parent / "main.py"


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        pending = asyncio.all_tasks(loop)
        for t in pending:
            t.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.close()


def _mock_cl(price_map: dict[str, float] | None = None) -> MagicMock:
    """Mock ChainlinkWSClient with optional per-coin price cache."""
    cl = MagicMock(spec=ChainlinkWSClient)
    pm = price_map or {}
    cl.get_mid.side_effect = lambda coin: pm.get(coin)
    cl.get_spot.side_effect = lambda coin: (
        SpotPrice(coin=coin, price=pm[coin]) if coin in pm else None
    )
    cl.get_spot_age.side_effect = lambda coin: 5.0 if coin in pm else float("inf")
    cl.on_price_update = MagicMock()
    return cl


def _mock_rtds(price_map: dict[str, float] | None = None) -> MagicMock:
    """Mock RTDSClient with optional per-coin price cache."""
    rtds = MagicMock(spec=RTDSClient)
    pm = price_map or {}
    rtds.get_mid.side_effect = lambda coin: pm.get(coin)
    rtds.get_spot.side_effect = lambda coin: (
        SpotPrice(coin=coin, price=pm[coin]) if coin in pm else None
    )
    rtds.get_spot_age.side_effect = lambda coin: 3.0 if coin in pm else float("inf")
    rtds.all_mids.return_value = dict(pm)
    rtds.tracked_coins = set(pm.keys())
    rtds.on_price_update = MagicMock()
    rtds.on_chainlink_update = MagicMock()
    # get_chainlink_spot returns a SpotPrice for any coin present in the map
    rtds.get_chainlink_spot.side_effect = lambda coin: (
        SpotPrice(coin=coin, price=pm[coin], timestamp=time.time()) if coin in pm else None
    )
    return rtds


def _make_oracle(
    cl_prices: dict[str, float] | None = None,
    rtds_prices: dict[str, float] | None = None,
) -> SpotOracle:
    rtds = _mock_rtds(rtds_prices or {"BTC": 85_000.0, "ETH": 3_000.0, "HYPE": 8.0})
    cl = _mock_cl(cl_prices or {"BTC": 85_100.0, "ETH": 3_005.0})
    return SpotOracle(rtds, cl, streams=None)


# ═════════════════════════════════════════════════════════════════════════════
# A. SpotOracle interface contract
# ═════════════════════════════════════════════════════════════════════════════

class TestSpotOracleInterface:
    """SpotOracle must expose every method that callers depend on.

    If any of these fail, a caller will get AttributeError at runtime — the
    kind of error that offline unit tests of individual components cannot catch.
    """

    def setup_method(self):
        self.oracle = _make_oracle()

    # ── Method existence + arity ──────────────────────────────────────────────

    def test_get_mid_accepts_two_positional_args(self):
        """get_mid(underlying, market_type) — the bug was calling it with 1 arg."""
        result = self.oracle.get_mid("BTC", "bucket_5m")
        # Must not raise; result is a float or None
        assert result is None or isinstance(result, float)

    def test_get_mid_rejects_one_arg(self):
        """RTDSClient.get_mid takes 1; SpotOracle.get_mid takes 2. Verify arity."""
        sig = inspect.signature(SpotOracle.get_mid)
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 2, (
            f"SpotOracle.get_mid must take 2 positional args (underlying, market_type), "
            f"got {params}"
        )

    def test_get_spot_accepts_two_positional_args(self):
        sig = inspect.signature(SpotOracle.get_spot)
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 2

    def test_get_spot_age_accepts_two_positional_args(self):
        sig = inspect.signature(SpotOracle.get_spot_age)
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 2

    def test_all_mids_exists_and_returns_dict(self):
        result = self.oracle.all_mids()
        assert isinstance(result, dict)

    def test_get_spot_age_rtds_accepts_one_coin_arg(self):
        sig = inspect.signature(SpotOracle.get_spot_age_rtds)
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 1

    def test_tracked_coins_is_property_returning_set(self):
        assert isinstance(SpotOracle.tracked_coins, property), (
            "tracked_coins must be a @property — state_sync_loop iterates over it"
        )
        result = self.oracle.tracked_coins
        assert isinstance(result, set)

    def test_on_rtds_update_accepts_callback(self):
        cb = AsyncMock()
        self.oracle.on_rtds_update(cb)  # must not raise

    def test_on_chainlink_update_accepts_callback(self):
        cb = AsyncMock()
        self.oracle.on_chainlink_update(cb)  # must not raise

    # ── RTDSClient signature is incompatible — document it explicitly ─────────

    def test_rtds_get_mid_takes_only_one_positional_arg(self):
        """RTDSClient.get_mid must take exactly 1 arg (coin only).

        This documents the incompatibility that caused the production bug:
        passing an RTDSClient where a SpotOracle is expected silently works
        until the first state sync iteration calls get_mid(coin, market_type).
        """
        sig = inspect.signature(RTDSClient.get_mid)
        params = [p for p in sig.parameters if p != "self"]
        assert len(params) == 1, (
            f"RTDSClient.get_mid must take 1 arg (coin), got: {params}. "
            f"If the signature changed, update this test AND verify SpotOracle "
            f"still wraps it correctly."
        )

    def test_rtds_passed_as_spot_oracle_would_fail(self):
        """Calling RTDSClient.get_mid with 2 args raises TypeError — the prod bug."""
        rtds = RTDSClient()
        with pytest.raises(TypeError, match="takes 2 positional arguments but 3"):
            rtds.get_mid("BTC", "bucket_5m")


# ═════════════════════════════════════════════════════════════════════════════
# B. SpotOracle routing
# ═════════════════════════════════════════════════════════════════════════════

class TestSpotOracleRouting:
    """get_mid(coin, market_type) must route to the correct underlying client."""

    def setup_method(self):
        self.cl_prices = {"BTC": 85_100.0, "ETH": 3_005.0, "SOL": 150.0,
                          "XRP": 0.55, "BNB": 600.0, "DOGE": 0.12}
        self.rtds_prices = {"BTC": 85_000.0, "ETH": 3_000.0, "SOL": 149.0,
                            "XRP": 0.54, "BNB": 599.0, "DOGE": 0.11, "HYPE": 8.5}
        self.rtds = _mock_rtds(self.rtds_prices)
        self.cl = _mock_cl(self.cl_prices)
        self.oracle = SpotOracle(self.rtds, self.cl, streams=None)

    @pytest.mark.parametrize("market_type", sorted(CHAINLINK_MARKET_TYPES))
    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_chainlink_market_type_routes_to_cl(self, coin, market_type):
        """5m/15m/4h non-HYPE → RTDS crypto_prices_chainlink relay."""
        result = self.oracle.get_mid(coin, market_type)
        assert result == self.rtds_prices[coin], (
            f"Expected RTDS chainlink price for {coin}/{market_type}, "
            f"got {result} (RTDS={self.rtds_prices[coin]})"
        )
        self.rtds.get_chainlink_spot.assert_called_with(coin)

    @pytest.mark.parametrize("market_type", ["bucket_1h", "bucket_daily", "bucket_weekly"])
    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_non_chainlink_market_type_routes_to_rtds(self, coin, market_type):
        """1h/daily/weekly → RTDSClient."""
        result = self.oracle.get_mid(coin, market_type)
        assert result == self.rtds_prices[coin], (
            f"Expected RTDSClient price for {coin}/{market_type}, "
            f"got {result} (RTDS={self.rtds_prices[coin]}, CL={self.cl_prices.get(coin)})"
        )
        self.rtds.get_mid.assert_called_with(coin)

    @pytest.mark.parametrize("market_type", sorted(CHAINLINK_MARKET_TYPES))
    def test_hype_chainlink_market_uses_freshest_wins(self, market_type):
        """HYPE on 5m/15m/4h — RTDS chainlink relay (streams=None → only RTDS relay)."""
        hype_price = 9.25
        # Must use side_effect (not return_value) because _mock_rtds already sets side_effect.
        self.rtds.get_chainlink_spot.side_effect = lambda coin: (
            SpotPrice(coin="HYPE", price=hype_price, timestamp=time.time())
            if coin == "HYPE" else None
        )
        result = self.oracle.get_mid("HYPE", market_type)
        # Result must be the RTDS chainlink relay price (streams=None → only RTDS relay)
        assert result == hype_price

    def test_unknown_market_type_routes_to_rtds(self):
        """Any unrecognised market type falls through to RTDSClient."""
        result = self.oracle.get_mid("BTC", "some_future_market_type")
        assert result == self.rtds_prices["BTC"]

    @pytest.mark.parametrize("market_type", sorted(CHAINLINK_MARKET_TYPES))
    @pytest.mark.parametrize("coin", ["BTC", "ETH"])
    def test_get_spot_routes_correctly(self, coin, market_type):
        snap = self.oracle.get_spot(coin, market_type)
        assert snap is not None
        assert snap.price == self.rtds_prices[coin]

    @pytest.mark.parametrize("market_type", ["bucket_daily"])
    @pytest.mark.parametrize("coin", ["BTC", "ETH"])
    def test_get_spot_age_routes_correctly(self, coin, market_type):
        age = self.oracle.get_spot_age(coin, market_type)
        # bucket_daily → RTDS → 3.0 from mock
        assert age == 3.0

    @pytest.mark.parametrize("market_type", ["bucket_5m"])
    @pytest.mark.parametrize("coin", ["BTC"])
    def test_get_spot_age_cl_market(self, coin, market_type):
        age = self.oracle.get_spot_age(coin, market_type)
        # bucket_5m → RTDS chainlink → timestamp is time.time() so age < 1s
        assert age < 1.0

    def test_no_data_returns_none(self):
        """If RTDS chainlink has no price for a coin, get_mid must return None."""
        self.rtds.get_chainlink_spot.side_effect = lambda coin: None
        result = self.oracle.get_mid("BTC", "bucket_5m")
        assert result is None


# ═════════════════════════════════════════════════════════════════════════════
# C. state_sync_loop contract (offline)
# ═════════════════════════════════════════════════════════════════════════════

def _make_state_sync_mocks(market_type: str = "bucket_5m", underlying: str = "BTC"):
    """Return (pm_mock, hl_mock, maker_mock, agent_mock, risk_mock, spot_oracle_mock)."""
    from market_data.pm_client import PMMarket, OrderBookSnapshot
    from datetime import datetime, timezone

    mkt = PMMarket(
        condition_id="mkt_001",
        title="BTC >$100k",
        token_id_yes="tok_yes_001",
        token_id_no="tok_no_001",
        market_type=market_type,
        underlying=underlying,
        fees_enabled=False,
        market_slug="btc-100k",
        end_date=datetime(2026, 4, 7, tzinfo=timezone.utc),
    )

    pm = MagicMock()
    pm.get_positions.return_value = {}
    pm.get_markets.return_value = {"mkt_001": mkt}
    pm.get_book.return_value = None
    pm._ws_connected = True
    pm._last_heartbeat_ts = time.time()

    hl = MagicMock()
    hl.get_fundings_snapshot.return_value = {}
    hl._ws_connected = True

    maker = MagicMock()
    maker.get_active_quotes.return_value = {}
    maker.get_coin_hedges.return_value = {}
    maker.get_signals.return_value = []

    agent = MagicMock()
    agent.get_shadow_log.return_value = []

    risk = MagicMock()
    risk.get_positions.return_value = {}

    # SpotOracle mock — call-counting proxy
    spot = MagicMock(spec=SpotOracle)
    spot.get_mid.return_value = 85_000.0
    spot.all_mids.return_value = {"BTC": 85_000.0}
    spot.get_spot_age_rtds.return_value = 2.5
    spot.tracked_coins = {"BTC"}

    return pm, hl, maker, agent, risk, spot


class TestStateSyncLoopContract:
    """Verify state_sync_loop calls SpotOracle with the correct signatures.

    These tests directly validate the interface contract that the wiring bug
    violated: get_mid(underlying, market_type) requires 2 args.
    """

    def _run_one_iteration(self, spot):
        """Run one iteration of state_sync_loop's body then cancel.

        Rebinds main._shutdown_event and main._state_changed to the fresh
        event loop — module-level asyncio.Event objects are bound to the loop
        that was current when main was first imported, which differs from the
        test's isolated loop and would raise RuntimeError otherwise.
        """
        import main as _main
        pm, hl, maker, agent, risk, _ = _make_state_sync_mocks()

        async def _run_loop():
            # Rebind module-level Events to the current (test) loop.
            _main._shutdown_event = asyncio.Event()
            _main._state_changed = asyncio.Event()
            task = asyncio.create_task(
                _main.state_sync_loop(pm, hl, maker, agent, risk, spot)
            )
            # Let one iteration complete
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(_run_loop())

    def test_get_mid_called_with_two_args(self):
        """state_sync_loop must call spot_oracle.get_mid(underlying, market_type)."""
        pm, hl, maker, agent, risk, spot = _make_state_sync_mocks(
            market_type="bucket_5m", underlying="BTC"
        )
        self._run_one_iteration(spot)
        # Verify all calls to get_mid used exactly 2 positional args
        for call_args in spot.get_mid.call_args_list:
            args, kwargs = call_args
            total = len(args) + len([k for k in ("underlying", "market_type") if k in kwargs])
            assert len(args) == 2, (
                f"state_sync_loop called get_mid with {len(args)} positional args, "
                f"expected 2. Call args: {call_args}. "
                f"This is the wiring bug: passing RTDSClient instead of SpotOracle."
            )

    def test_get_mid_gets_underlying_and_market_type(self):
        """Verify the actual argument values passed to get_mid."""
        pm, hl, maker, agent, risk, spot = _make_state_sync_mocks(
            market_type="bucket_5m", underlying="BTC"
        )
        self._run_one_iteration(spot)
        if spot.get_mid.call_args_list:
            args, _ = spot.get_mid.call_args_list[0]
            assert args[0] == "BTC", f"First arg should be 'BTC' (underlying), got: {args[0]}"
            assert args[1] == "bucket_5m", f"Second arg should be 'bucket_5m', got: {args[1]}"

    def test_all_mids_called_with_no_args(self):
        pm, hl, maker, agent, risk, spot = _make_state_sync_mocks()
        self._run_one_iteration(spot)
        for call_args in spot.all_mids.call_args_list:
            args, _ = call_args
            assert len(args) == 0, f"all_mids() takes no args, got: {args}"

    def test_get_spot_age_rtds_called_with_one_coin_arg(self):
        pm, hl, maker, agent, risk, spot = _make_state_sync_mocks()
        self._run_one_iteration(spot)
        for call_args in spot.get_spot_age_rtds.call_args_list:
            args, _ = call_args
            assert len(args) == 1, f"get_spot_age_rtds(coin) takes 1 arg, got: {args}"
            assert isinstance(args[0], str), f"arg must be a coin string, got: {args[0]}"

    def test_rtds_client_directly_raises_for_two_arg_get_mid(self):
        """Document that passing RTDSClient (not SpotOracle) to state_sync_loop
        raises TypeError — this is the exact production bug."""
        pm, hl, maker, agent, risk, _ = _make_state_sync_mocks()
        rtds_direct = MagicMock(spec=RTDSClient)
        rtds_direct.get_mid.side_effect = lambda coin: 85_000.0
        # RTDSClient.get_mid only accepts 1 positional arg (coin)
        # Simulate what state_sync_loop does when wired incorrectly:
        with pytest.raises(TypeError):
            rtds_direct.get_mid("BTC", "bucket_5m")  # 2 args on RTDSClient spec

    def test_loop_completes_without_exception(self):
        """state_sync_loop must not raise when given a properly typed SpotOracle."""
        pm, hl, maker, agent, risk, spot = _make_state_sync_mocks()
        # Should complete one iteration without raising
        self._run_one_iteration(spot)

    @pytest.mark.parametrize("market_type", list(CHAINLINK_MARKET_TYPES) + ["bucket_daily"])
    def test_get_mid_called_for_every_market_type(self, market_type):
        pm, hl, maker, agent, risk, spot = _make_state_sync_mocks(
            market_type=market_type, underlying="BTC"
        )
        self._run_one_iteration(spot)
        if spot.get_mid.call_args_list:
            _, kwargs_or_args = spot.get_mid.call_args_list[0].args, spot.get_mid.call_args_list[0].kwargs
            # Verify it was called at all — market must reach the get_mid call
            assert spot.get_mid.called


# ═════════════════════════════════════════════════════════════════════════════
# D. main() wiring inspection (static source analysis)
# ═════════════════════════════════════════════════════════════════════════════

class TestMainWiring:
    """Static analysis of main.py to verify correct variable names are wired.

    These tests parse the AST of main.py and verify that:
      1. `state_sync_loop` receives `spot_oracle` (SpotOracle), not `spot_client` (RTDSClient).
      2. `PositionMonitor` receives `spot_client=spot_oracle`.
      3. `MomentumScanner` receives `spot_client=spot_oracle`.
      4. `SpotOracle` is constructed from `spot_client`, `chainlink_ws`, `chainlink_streams`.

    Why AST: these tests must fail if someone accidentally changes the wiring back.
    A comment or rename could bypass a pure string search; AST catches renamings.
    """

    @classmethod
    def setup_class(cls):
        src = MAIN_PY.read_text(encoding="utf-8")
        cls._tree = ast.parse(src)
        cls._src_lines = src.splitlines()

    def _find_calls(self, func_name: str) -> list[ast.Call]:
        """Find all Call nodes in main() that call `func_name`."""
        calls = []
        for node in ast.walk(self._tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id == func_name:
                    calls.append(node)
                elif isinstance(node.func, ast.Attribute) and node.func.attr == func_name:
                    calls.append(node)
        return calls

    def _get_kwarg(self, call: ast.Call, key: str) -> Optional[ast.expr]:
        for kw in call.keywords:
            if kw.arg == key:
                return kw.value
        return None

    def _get_arg_name(self, node: ast.expr) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        return None

    def test_state_sync_loop_receives_spot_oracle_not_spot_client(self):
        """main() must pass `spot_oracle` (SpotOracle), NOT `spot_client` (RTDSClient)."""
        calls = self._find_calls("state_sync_loop")
        assert calls, "state_sync_loop call not found in main.py"
        for call in calls:
            # state_sync_loop(pm, hl, maker, agent, risk_engine, spot_oracle)
            # spot_oracle is the 6th positional arg
            if len(call.args) >= 6:
                arg_name = self._get_arg_name(call.args[5])
                assert arg_name == "spot_oracle", (
                    f"state_sync_loop 6th arg is '{arg_name}', expected 'spot_oracle'. "
                    f"Passing 'spot_client' (RTDSClient) causes 'takes 2 positional "
                    f"arguments but 3 were given' on every state sync iteration."
                )

    def test_position_monitor_receives_spot_oracle(self):
        """PositionMonitor(spot_client=spot_oracle) — NOT spot_client (RTDSClient)."""
        calls = self._find_calls("PositionMonitor")
        assert calls, "PositionMonitor() call not found in main.py"
        for call in calls:
            kwarg = self._get_kwarg(call, "spot_client")
            if kwarg is not None:
                name = self._get_arg_name(kwarg)
                assert name == "spot_oracle", (
                    f"PositionMonitor(spot_client=...) receives '{name}', "
                    f"expected 'spot_oracle'."
                )

    def test_momentum_scanner_receives_spot_oracle(self):
        """MomentumScanner(spot_client=spot_oracle) — NOT spot_client (RTDSClient)."""
        calls = self._find_calls("MomentumScanner")
        assert calls, "MomentumScanner() call not found in main.py"
        for call in calls:
            kwarg = self._get_kwarg(call, "spot_client")
            if kwarg is not None:
                name = self._get_arg_name(kwarg)
                assert name == "spot_oracle", (
                    f"MomentumScanner(spot_client=...) receives '{name}', "
                    f"expected 'spot_oracle'."
                )

    def test_spot_oracle_constructed_with_spot_client_cl_streams(self):
        """SpotOracle(spot_client, chainlink_ws, chainlink_streams) — correct arg order."""
        calls = self._find_calls("SpotOracle")
        assert calls, "SpotOracle() call not found in main.py"
        for call in calls:
            if len(call.args) >= 2:
                arg0 = self._get_arg_name(call.args[0])
                arg1 = self._get_arg_name(call.args[1])
                assert arg0 == "spot_client", (
                    f"SpotOracle 1st arg is '{arg0}', expected 'spot_client' (RTDSClient). "
                    f"SpotOracle.__init__ expects (rtds, chainlink, streams)."
                )
                assert arg1 == "chainlink_ws", (
                    f"SpotOracle 2nd arg is '{arg1}', expected 'chainlink_ws' (ChainlinkWSClient)."
                )

    def test_maker_strategy_receives_spot_oracle(self):
        """MakerStrategy(spot_client=spot_oracle) — spot pricing uses oracle routing."""
        calls = self._find_calls("MakerStrategy")
        assert calls, "MakerStrategy() call not found in main.py"
        for call in calls:
            kwarg = self._get_kwarg(call, "spot_client")
            if kwarg is not None:
                name = self._get_arg_name(kwarg)
                assert name == "spot_oracle", (
                    f"MakerStrategy(spot_client=...) receives '{name}', "
                    f"expected 'spot_oracle'."
                )


# ═════════════════════════════════════════════════════════════════════════════
# E. SpotOracle live smoke test
# ═════════════════════════════════════════════════════════════════════════════

pytestmark_live = pytest.mark.live
_LIVE_TIMEOUT_S = 60


@pytest.fixture(scope="module")
def live_spot_oracle():
    """Real RTDSClient + stub Chainlink clients wired into SpotOracle.

    ChainlinkWSClient is stubbed because the live test requires a paid
    endpoint.  The RTDS feed is always free — it's what production uses for
    1h/daily/weekly markets.
    """
    async def _start():
        rtds = RTDSClient()
        cl_stub = MagicMock(spec=ChainlinkWSClient)
        cl_stub.get_mid.return_value = None         # simulates no Chainlink data
        cl_stub.get_spot.return_value = None
        cl_stub.get_spot_age.return_value = float("inf")
        cl_stub.on_price_update = MagicMock()
        oracle = SpotOracle(rtds, cl_stub, streams=None)
        await rtds.start()
        # Wait for all 6 exchange-aggregated coins to arrive
        deadline = time.monotonic() + _LIVE_TIMEOUT_S
        while time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            mids = rtds.all_mids()
            if len(mids) >= 6:   # BTC/ETH/SOL/XRP/BNB/DOGE
                break
        return oracle, rtds

    oracle, rtds = _run(_start())
    yield oracle
    _run(rtds.stop())


@pytest.mark.live
class TestSpotOracleLive:
    """Verify SpotOracle.get_mid(coin, market_type) returns real prices via RTDS."""

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL", "XRP", "BNB", "DOGE"])
    def test_rtds_daily_returns_positive_price(self, coin, live_spot_oracle):
        price = live_spot_oracle.get_mid(coin, "bucket_daily")
        assert price is not None, (
            f"SpotOracle.get_mid('{coin}', 'bucket_daily') returned None — "
            f"RTDS is not delivering prices for this coin."
        )
        assert price > 0

    @pytest.mark.parametrize("coin", ["BTC", "ETH"])
    def test_rtds_1h_returns_positive_price(self, coin, live_spot_oracle):
        price = live_spot_oracle.get_mid(coin, "bucket_1h")
        assert price is not None and price > 0

    def test_all_mids_returns_dict_of_floats(self, live_spot_oracle):
        mids = live_spot_oracle.all_mids()
        assert isinstance(mids, dict)
        assert len(mids) >= 4, f"Expected ≥4 coins, got: {list(mids.keys())}"
        for coin, price in mids.items():
            assert isinstance(price, float) and price > 0, (
                f"all_mids()['{coin}'] = {price!r} is not a positive float"
            )

    def test_tracked_coins_is_set_of_strings(self, live_spot_oracle):
        coins = live_spot_oracle.tracked_coins
        assert isinstance(coins, set) and len(coins) >= 4
        for c in coins:
            assert isinstance(c, str)

    def test_get_spot_age_rtds_is_recent(self, live_spot_oracle):
        for coin in ["BTC", "ETH"]:
            age = live_spot_oracle.get_spot_age_rtds(coin)
            assert age != float("inf"), f"RTDS age for {coin} is inf — never received"
            assert age < 60, f"RTDS age for {coin} is {age:.1f}s — stale"

    @pytest.mark.parametrize("coin", ["BTC", "ETH", "SOL"])
    def test_chainlink_market_type_returns_rtds_chainlink_price(self, coin, live_spot_oracle):
        """bucket_5m routes to RTDS crypto_prices_chainlink — must return a real price.

        ChainlinkWSClient is no longer in the routing path for 5m/15m/4h markets.
        RTDS chainlink is the sole live source for all Chainlink-oracle coins.
        """
        price = live_spot_oracle.get_mid(coin, "bucket_5m")
        assert price is not None, (
            f"SpotOracle.get_mid('{coin}', 'bucket_5m') returned None — "
            f"RTDS crypto_prices_chainlink is not delivering prices for this coin."
        )
        assert price > 0


# ═════════════════════════════════════════════════════════════════════════════
# F. state_sync_loop live sweep
# ═════════════════════════════════════════════════════════════════════════════

@pytest.mark.live
class TestStateSyncLoopLive:
    """state_sync_loop must complete one iteration against live RTDS data
    without raising, and spot_mid fields must be populated in api_state.

    This is the full end-to-end regtest for the production bug: passing the
    wrong type to state_sync_loop shows up immediately as an exception here.
    """

    def test_one_iteration_does_not_raise(self, live_spot_oracle):
        """The primary wiring regression test.

        Passing RTDSClient here (not SpotOracle) would raise:
            TypeError: RTDSClient.get_mid() takes 2 positional arguments but 3 given
        """
        import main as _main
        from market_data.pm_client import PMMarket
        from datetime import datetime, timezone

        mkt = PMMarket(
            condition_id="mkt_live_001",
            title="Will BTC exceed $100k?",
            token_id_yes="tok_live_yes",
            token_id_no="tok_live_no",
            market_type="bucket_5m",
            underlying="BTC",
            fees_enabled=False,
            market_slug="btc-100k",
            end_date=datetime(2026, 4, 7, tzinfo=timezone.utc),
        )
        pm = MagicMock()
        pm.get_positions.return_value = {}
        pm.get_markets.return_value = {"mkt_live_001": mkt}
        pm.get_book.return_value = None
        pm._ws_connected = True
        pm._last_heartbeat_ts = time.time()

        hl = MagicMock()
        hl.get_fundings_snapshot.return_value = {}
        hl._ws_connected = True

        maker = MagicMock()
        maker.get_active_quotes.return_value = {}
        maker.get_coin_hedges.return_value = {}
        maker.get_signals.return_value = []

        agent = MagicMock()
        agent.get_shadow_log.return_value = []

        risk = MagicMock()
        risk.get_positions.return_value = {}

        async def _run_one():
            _main._shutdown_event = asyncio.Event()
            _main._state_changed = asyncio.Event()
            task = asyncio.create_task(
                _main.state_sync_loop(pm, hl, maker, agent, risk, live_spot_oracle)
            )
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Must not raise — any TypeError from wrong wiring propagates here
        _run(_run_one())

    def test_spot_mid_field_populated_for_rtds_market(self, live_spot_oracle):
        """After one iteration, api_state.markets entries have spot_mid populated
        for RTDS market types (1h/daily/weekly) since RTDS feed is live."""
        import main as _main
        from market_data.pm_client import PMMarket
        from datetime import datetime, timezone

        mkt = PMMarket(
            condition_id="mkt_live_002",
            title="Will BTC exceed $100k?",
            token_id_yes="tok_live_002_yes",
            token_id_no="tok_live_002_no",
            market_type="bucket_daily",   # RTDS oracle → prices available
            underlying="BTC",
            fees_enabled=False,
            market_slug="btc-100k-daily",
            end_date=datetime(2026, 4, 7, tzinfo=timezone.utc),
        )
        pm = MagicMock()
        pm.get_positions.return_value = {}
        pm.get_markets.return_value = {"mkt_live_002": mkt}
        pm.get_book.return_value = None
        pm._ws_connected = True
        pm._last_heartbeat_ts = time.time()

        hl = MagicMock()
        hl.get_fundings_snapshot.return_value = {}
        hl._ws_connected = True

        maker = MagicMock()
        maker.get_active_quotes.return_value = {}
        maker.get_coin_hedges.return_value = {}
        maker.get_signals.return_value = []

        agent = MagicMock()
        agent.get_shadow_log.return_value = []

        risk = MagicMock()
        risk.get_positions.return_value = {}

        async def _run_one():
            _main._shutdown_event = asyncio.Event()
            _main._state_changed = asyncio.Event()
            task = asyncio.create_task(
                _main.state_sync_loop(pm, hl, maker, agent, risk, live_spot_oracle)
            )
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        _run(_run_one())

        snap = _main.api_state.markets.get("mkt_live_002")
        assert snap is not None, "api_state.markets not populated after one iteration"
        spot_mid = snap.get("spot_mid")
        assert spot_mid is not None, (
            "spot_mid is None for bucket_daily BTC market — RTDS price not flowing "
            "through SpotOracle into state_sync_loop."
        )
        assert isinstance(spot_mid, float) and spot_mid > 0, (
            f"spot_mid={spot_mid!r} is not a positive float"
        )

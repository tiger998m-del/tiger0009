from __future__ import annotations

import pytest

from app.database.repository import TradeRepository
from app.risk.risk_manager import RiskManager
from tests.conftest import make_settings


def _risk_manager(**settings_overrides) -> RiskManager:
    settings = make_settings(**settings_overrides)
    repo = TradeRepository(":memory:")
    return RiskManager(settings, repo)


def test_per_trade_is_48_percent_of_capital():
    rm = _risk_manager(bot_capital_sar=1000.0, sar_per_usdt=3.75, trade_allocation_pct=0.48)
    capital_usdt = 1000.0 / 3.75
    assert abs(rm.per_trade_usdt - capital_usdt * 0.48) < 1e-9


def test_two_trades_use_at_most_96_percent_of_capital():
    rm = _risk_manager(bot_capital_sar=1000.0, sar_per_usdt=3.75, trade_allocation_pct=0.48, max_open_positions=2)
    total_committed = rm.per_trade_usdt * rm.max_open_positions
    capital_usdt = 1000.0 / 3.75
    assert total_committed <= capital_usdt * 0.96 + 1e-6
    assert total_committed > capital_usdt * 0.95  # sanity: it should indeed be ~96%, not something smaller


def test_scales_automatically_when_capital_env_changes():
    rm_small = _risk_manager(bot_capital_sar=1000.0)
    rm_big = _risk_manager(bot_capital_sar=20000.0)
    assert rm_big.per_trade_usdt == pytest.approx(rm_small.per_trade_usdt * 20)


def test_compute_trade_capital_never_exceeds_available_balance():
    rm = _risk_manager(bot_capital_sar=1000.0, sar_per_usdt=3.75)
    theoretical = rm.per_trade_usdt
    capital, reduced = rm.compute_trade_capital(available_balance_usdt=theoretical / 2)
    assert reduced is True
    assert capital == theoretical / 2


def test_compute_trade_capital_uses_full_allocation_when_funds_sufficient():
    rm = _risk_manager(bot_capital_sar=1000.0, sar_per_usdt=3.75)
    capital, reduced = rm.compute_trade_capital(available_balance_usdt=10_000)
    assert reduced is False
    assert capital == rm.per_trade_usdt


def test_max_open_positions_blocks_third_trade():
    rm = _risk_manager(max_open_positions=2)
    allowed, _ = rm.can_open_new_trade(current_open_count=2)
    assert allowed is False
    allowed, _ = rm.can_open_new_trade(current_open_count=1)
    assert allowed is True

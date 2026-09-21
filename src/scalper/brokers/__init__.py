"""Execution venues: simulated (paper) and MetaTrader 5."""

from __future__ import annotations

from typing import Any

from .base import Broker, BrokerError
from .paper import PaperBroker

__all__ = ["Broker", "BrokerError", "PaperBroker", "build_broker"]


def build_broker(cfg: Any, *, allow_live: bool = False) -> Broker:
    """Construct the configured broker.

    `broker.mode: paper` always wins unless the caller explicitly passes
    `allow_live=True` *and* the environment allows live trading. Two switches,
    because a config typo should not be able to spend real money.
    """
    mode = str(cfg.broker.mode).lower()

    if mode == "paper":
        broker = PaperBroker(
            cfg.broker.paper,
            initial_balance=cfg.account.initial_balance,
            leverage=cfg.account.leverage,
            currency=cfg.account.currency,
            intrabar_priority=cfg.backtest.intrabar_priority,
            spread_overrides={s.symbol: s.spread_pips for s in cfg.instruments},
        )
        broker.register_specs(list(cfg.instruments))
        return broker

    if mode == "mt5":
        from ..config import is_live_allowed
        from .mt5 import build_mt5_broker

        effective_allow = bool(allow_live and is_live_allowed())
        broker = build_mt5_broker(cfg, allow_live=effective_allow)
        if allow_live and not is_live_allowed():
            import logging

            logging.getLogger("scalper").warning(
                "--live was requested but SCALPER_ALLOW_LIVE is not 'yes' in the environment. "
                "Live routing stays disabled; a real-money account will be refused."
            )
        return broker

    raise ValueError(f"Unknown broker.mode {mode!r}. Use 'paper' or 'mt5'.")

"""scalper — a small, honest forex scalping research and execution toolkit.

The package is deliberately layered so the same strategy code can be driven by
three different backends:

* :mod:`scalper.backtest` — bar-by-bar replay over historical/synthetic data.
* :mod:`scalper.paper` (via :class:`scalper.brokers.paper.PaperBroker`) — the
  identical engine, fed by a live data source, with no money at risk.
* :mod:`scalper.live` — real MetaTrader 5 order routing, gated behind an
  explicit opt-in (config ``broker.mode: mt5``, ``--live``,
  ``SCALPER_ALLOW_LIVE=yes`` and ``--i-understand-the-risk``).

Strategies live in :mod:`scalper.strategies` and are registered by name, so
adding one does not require touching the engine.
"""

__version__ = "0.1.0"
__all__ = ["__version__"]

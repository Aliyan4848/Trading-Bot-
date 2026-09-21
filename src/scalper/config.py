"""Configuration loading: YAML file -> validated dataclasses -> env overrides.

Design rule: the rest of the codebase never touches raw dicts. It receives typed
objects, so a typo in `config.yaml` fails loudly at startup instead of silently
falling back to a default in the middle of a trading session.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import time as dtime
from pathlib import Path
from typing import Any

import yaml

# -----------------------------------------------------------------------------
# Timeframes
# -----------------------------------------------------------------------------
TIMEFRAME_MINUTES: dict[str, int] = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}

# FX spot: 24h x 5d, ~52 weeks per year (holidays ignored -> slightly conservative).
TRADING_DAYS_PER_YEAR = 260
TRADING_HOURS_PER_DAY = 24


def bars_per_year(timeframe: str) -> float:
    """Annualisation factor for risk metrics on a given timeframe."""
    minutes = TIMEFRAME_MINUTES.get(timeframe.upper())
    if minutes is None:
        raise ValueError(f"Unknown timeframe {timeframe!r}. Known: {sorted(TIMEFRAME_MINUTES)}")
    weeks = TRADING_DAYS_PER_YEAR / 5.0
    return weeks * 5 * TRADING_HOURS_PER_DAY * 60 / minutes


# -----------------------------------------------------------------------------
# Instrument specification
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class InstrumentSpec:
    """Contract details for one symbol, as used for sizing and P&L.

    `pip_value_per_lot` is expressed in the ACCOUNT currency for a 1.00 lot
    position. Broker contract specs vary — verify before going live.
    """

    symbol: str
    pip_size: float
    contract_size: float = 100_000.0
    pip_value_per_lot: float = 10.0
    spread_pips: float = 1.0
    digits: int = 5

    def pips(self, price_delta: float) -> float:
        return abs(price_delta) / self.pip_size if self.pip_size else 0.0

    def price_from_pips(self, pips: float) -> float:
        return pips * self.pip_size

    def value_of_pips(self, pips: float, lots: float) -> float:
        """Signed account-currency value of a pip move for a given size."""
        return pips * self.pip_value_per_lot * lots


# -----------------------------------------------------------------------------
# Sections
# -----------------------------------------------------------------------------
@dataclass(slots=True)
class MetaConfig:
    name: str = "fx-scalper"
    log_level: str = "INFO"
    random_seed: int = 7


@dataclass(slots=True)
class AccountConfig:
    currency: str = "USD"
    initial_balance: float = 10_000.0
    leverage: int = 30


@dataclass(slots=True)
class CsvDataConfig:
    path: str = "data/EURUSD_M1.csv"
    timestamp_column: str = "time"
    tz: str = "UTC"


@dataclass(slots=True)
class Mt5DataConfig:
    bars: int = 50_000


@dataclass(slots=True)
class SyntheticDataConfig:
    #: Empty means "generate every symbol listed under `instruments:`".
    symbols: list[str] = field(default_factory=list)
    bars: int = 40_000
    start: str = "2024-01-01"
    seed: int = 7
    annual_vol: float = 0.08
    trend_strength: float = 0.15
    regime_flip_bars: int = 3_000
    spread_pips: float = 0.6


@dataclass(slots=True)
class DataConfig:
    source: str = "synthetic"
    timeframe: str = "M1"
    csv: CsvDataConfig = field(default_factory=CsvDataConfig)
    mt5: Mt5DataConfig = field(default_factory=Mt5DataConfig)
    synthetic: SyntheticDataConfig = field(default_factory=SyntheticDataConfig)


@dataclass(slots=True)
class Mt5BrokerConfig:
    magic: int = 20260921
    deviation_points: int = 20
    filling_mode: str = "IOC"
    order_retries: int = 3
    retry_sleep_sec: float = 0.5


@dataclass(slots=True)
class PaperBrokerConfig:
    slippage_pips: float = 0.3
    commission_per_lot: float = 7.0
    stop_out_level_pct: float = 50.0


@dataclass(slots=True)
class BrokerConfig:
    mode: str = "paper"
    mt5: Mt5BrokerConfig = field(default_factory=Mt5BrokerConfig)
    paper: PaperBrokerConfig = field(default_factory=PaperBrokerConfig)

    def __post_init__(self) -> None:
        if self.mode not in ("paper", "mt5"):
            raise ValueError(f"broker.mode must be 'paper' or 'mt5', got {self.mode!r}")


@dataclass(slots=True)
class RiskConfig:
    risk_per_trade_pct: float = 0.5
    max_daily_loss_pct: float = 3.0
    max_drawdown_pct: float = 12.0
    max_concurrent_positions: int = 2
    max_trades_per_day: int = 12
    min_lot: float = 0.01
    max_lot: float = 5.0
    lot_step: float = 0.01
    max_spread_pips: float = 2.0
    min_seconds_between_trades: int = 0
    trailing_stop: bool = False
    trailing_start_r: float = 1.0
    trailing_distance_r: float = 0.5
    break_even_at_r: float | None = None


@dataclass(slots=True)
class SessionWindow:
    name: str
    start: str
    end: str

    def as_times(self) -> tuple[dtime, dtime]:
        return _parse_hhmm(self.start), _parse_hhmm(self.end)

    def contains(self, t: dtime) -> bool:
        start, end = self.as_times()
        if start <= end:
            return start <= t <= end
        return t >= start or t <= end  # window crosses midnight


@dataclass(slots=True)
class SessionConfig:
    enabled: bool = True
    timezone: str = "UTC"
    windows: list[SessionWindow] = field(default_factory=list)
    skip_friday_after: str | None = None
    skip_weekend: bool = True
    #: Close any open position on the last bar of a session (scalpers do not
    #: hold through the close; overnight gaps belong to swing traders).
    flat_at_close: bool = True


@dataclass(slots=True)
class StrategyConfig:
    name: str = "ema_rsi_momentum"
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BacktestConfig:
    warmup_bars: int = 250
    entry_on_next_open: bool = True
    intrabar_priority: str = "stop"
    annualization_bars: float | None = None  # None -> derived from timeframe
    risk_free_rate: float = 0.0
    export_trades: bool = True


@dataclass(slots=True)
class ReportingConfig:
    output_dir: str = "results"
    write_json: bool = True
    write_markdown: bool = True
    write_equity_csv: bool = True
    plot_equity_curve: bool = True


@dataclass(slots=True)
class AppConfig:
    meta: MetaConfig = field(default_factory=MetaConfig)
    account: AccountConfig = field(default_factory=AccountConfig)
    data: DataConfig = field(default_factory=DataConfig)
    broker: BrokerConfig = field(default_factory=BrokerConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    session: SessionConfig = field(default_factory=SessionConfig)
    strategy: StrategyConfig = field(default_factory=StrategyConfig)
    instruments: list[InstrumentSpec] = field(default_factory=list)
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    reporting: ReportingConfig = field(default_factory=ReportingConfig)
    config_path: str | None = None

    # -- lookups -------------------------------------------------------------
    def instrument(self, symbol: str) -> InstrumentSpec:
        for spec in self.instruments:
            if spec.symbol.upper() == symbol.upper():
                return spec
        raise KeyError(
            f"Symbol {symbol!r} is not defined under `instruments:` in the config. "
            f"Known: {[i.symbol for i in self.instruments]}"
        )

    @property
    def symbols(self) -> list[str]:
        return [i.symbol for i in self.instruments]

    @property
    def annualization_bars(self) -> float:
        if self.backtest.annualization_bars:
            return float(self.backtest.annualization_bars)
        return bars_per_year(self.data.timeframe)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
def _parse_hhmm(value: str) -> dtime:
    parts = str(value).strip().split(":")
    if len(parts) < 2:
        raise ValueError(f"Time {value!r} must look like 'HH:MM'")
    hour, minute = int(parts[0]), int(parts[1])
    return dtime(hour=hour, minute=minute)


def _to_instrument(raw: Any) -> InstrumentSpec:
    return raw if isinstance(raw, InstrumentSpec) else InstrumentSpec(**raw)


def _to_session_window(raw: Any) -> SessionWindow:
    return raw if isinstance(raw, SessionWindow) else SessionWindow(**raw)


# Explicit nesting map: which config keys hold a nested dataclass (or list of
# them). Written out by hand on purpose — introspection of `Optional[...]`
# annotations is brittle across Python versions.
_NESTED: dict[type, dict[str, Any]] = {}


def _register_nested() -> None:
    _NESTED.update(
        {
            AppConfig: {
                "meta": MetaConfig,
                "account": AccountConfig,
                "data": DataConfig,
                "broker": BrokerConfig,
                "risk": RiskConfig,
                "session": SessionConfig,
                "strategy": StrategyConfig,
                "backtest": BacktestConfig,
                "reporting": ReportingConfig,
                "instruments": [_to_instrument],
                "config_path": None,
            },
            DataConfig: {
                "csv": CsvDataConfig,
                "mt5": Mt5DataConfig,
                "synthetic": SyntheticDataConfig,
            },
            BrokerConfig: {"mt5": Mt5BrokerConfig, "paper": PaperBrokerConfig},
            SessionConfig: {"windows": [_to_session_window]},
        }
    )


_register_nested()


def _build(cls: type, raw: Any, path: str = "") -> Any:
    """Recursively construct a dataclass from a dict, rejecting unknown keys."""
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise TypeError(
            f"Config section `{path or cls.__name__}` must be a mapping, got {type(raw).__name__}"
        )

    known = {f.name: f for f in fields(cls)}
    unknown = set(raw) - set(known)
    if unknown:
        raise ValueError(
            f"Unknown key(s) in config section `{path or cls.__name__}`: {sorted(unknown)}.\n"
            f"Valid keys: {sorted(known)}"
        )

    spec = _NESTED.get(cls, {})
    kwargs: dict[str, Any] = {}
    for name, value in raw.items():
        target = spec.get(name)
        child_path = f"{path}.{name}" if path else name
        if is_dataclass(target) and isinstance(target, type) and isinstance(value, dict):
            kwargs[name] = _build(target, value, child_path)
        elif isinstance(target, list) and isinstance(value, list):
            convert = target[0]
            kwargs[name] = [convert(v) for v in value]
        else:
            kwargs[name] = value

    return cls(**kwargs)


def load_env_file(path: str | Path = ".env", override: bool = False) -> dict[str, str]:
    """Minimal `.env` reader (no python-dotenv dependency needed)."""
    loaded: dict[str, str] = {}
    p = Path(path)
    if not p.exists():
        return loaded
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        loaded[key] = value
        if override or key not in os.environ:
            os.environ[key] = value
    return loaded


def load_config(path: str | Path, overrides: dict[str, Any] | None = None) -> AppConfig:
    """Load, validate, and return the application config.

    `overrides` is a shallow dotted-path map applied on top of the YAML, e.g.
    ``{"risk.risk_per_trade_pct": 1.0, "broker.mode": "mt5"}`` — used by the CLI
    so you can experiment without editing the file.
    """
    cfg_path = Path(path)
    if not cfg_path.exists():
        raise FileNotFoundError(
            f"Config file not found: {cfg_path}. Start from config/config.yaml."
        )

    raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    raw = _apply_overrides(raw, overrides or {})
    cfg: AppConfig = _build(AppConfig, raw)
    cfg.config_path = str(cfg_path)

    if not cfg.instruments:
        raise ValueError("Config must define at least one instrument under `instruments:`")
    _validate(cfg)
    return cfg


def _apply_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    import copy

    out = copy.deepcopy(raw)
    for dotted, value in overrides.items():
        if value is None:
            continue
        node = out
        parts = dotted.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
            if not isinstance(node, dict):
                raise ValueError(f"Cannot override `{dotted}`: `{part}` is not a mapping")
        node[parts[-1]] = value
    return out


def _validate(cfg: AppConfig) -> None:
    if cfg.risk.risk_per_trade_pct <= 0:
        raise ValueError("risk.risk_per_trade_pct must be > 0")
    if not 0 < cfg.risk.max_daily_loss_pct <= 100:
        raise ValueError("risk.max_daily_loss_pct must be within (0, 100]")
    if cfg.risk.min_lot <= 0 or cfg.risk.max_lot < cfg.risk.min_lot:
        raise ValueError("risk.min_lot must be > 0 and <= risk.max_lot")
    if cfg.data.timeframe.upper() not in TIMEFRAME_MINUTES:
        raise ValueError(f"Unknown data.timeframe {cfg.data.timeframe!r}")
    if cfg.data.source not in ("synthetic", "csv", "mt5"):
        raise ValueError("data.source must be one of: synthetic, csv, mt5")
    for spec in cfg.instruments:
        if spec.pip_size <= 0:
            raise ValueError(f"{spec.symbol}: pip_size must be > 0")
        if spec.pip_value_per_lot <= 0:
            raise ValueError(f"{spec.symbol}: pip_value_per_lot must be > 0")


def is_live_allowed() -> bool:
    """Hard safety gate for real-money routing.

    Live orders require `SCALPER_ALLOW_LIVE=yes` in the environment *and* the
    ``--live`` CLI flag. Two independent switches, so a single mistake is not
    enough to send real money to the market.
    """
    return str(os.environ.get("SCALPER_ALLOW_LIVE", "")).strip().lower() in ("yes", "true", "1", "y")

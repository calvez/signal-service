"""Settings: secrets from .env / environment, everything else from config.yaml."""

from datetime import datetime, time
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.timeconv import validate_mode


class Secrets(BaseSettings):
    """Values from the environment or `.env`. Never log these."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    openrouter_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    ingest_token: str = ""
    db_path: str = "data/signal.db"
    spool_dir: str = "/var/spool/signal-mt5"  # where the EA drops its files (app/spool.py)
    config_path: str = "config.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SymbolCfg(_Strict):
    name: str
    session: Literal["eu", "us"]
    role: Literal["traded", "context"]
    calendar: str


class SessionCfg(_Strict):
    tz: str
    start: time
    end: time
    brief_at: time
    cash_close: time


class RulesCfg(_Strict):
    # His H1/D1 rule: no setups when H1 and D1 disagree. Switchable for backtests.
    require_htf_alignment: bool = True
    max_trades_per_day: int
    cooldown_after_win_min: int
    htf_neutral_counts_as_conflict: bool
    min_reward_risk: float
    stop_atr_min: float
    stop_atr_max: float
    entry_max_atr_from_close: float
    push_grades: list[Literal["A", "B"]]


class FeaturesCfg(_Strict):
    ema_period: int
    atr_period: int
    swing_confirm_bars: int
    opening_range_bars: int
    prompt_bars: int


class LlmCfg(_Strict):
    model: str
    provider_order: list[str]
    allow_fallbacks: bool
    temperature: float
    timeout_sec: int
    daily_budget_usd: float
    prompt_version: str


class NewsItem(_Strict):
    at: datetime  # naive wall-clock time in `tz`
    tz: str
    name: str


class ChartCfg(_Strict):
    bars: int
    width: int
    height: int


class QuietHours(_Strict):
    start: time
    end: time


class TelegramCfg(_Strict):
    display_tz: str
    quiet_hours: QuietHours
    chart: ChartCfg


class ReportsCfg(_Strict):
    daily_at: time
    tz: str


class FtmoCfg(_Strict):
    initial_balance: float
    daily_loss_pct: float
    max_loss_pct: float
    day_reset_tz: str
    warn_levels_pct: list[int]


class MonitorsCfg(_Strict):
    heartbeat_timeout_session_sec: int
    heartbeat_timeout_other_sec: int
    disk_min_free_pct: int


class ManagementCfg(_Strict):
    """How a position is managed after the entry (app/position.py)."""

    mode: Literal["pyramid", "fixed_target"] = "pyramid"
    risk_pct: float = 0.3  # % of the initial balance risked by the first unit (= 1R)
    max_adds: int | None = None  # None = no limit
    add_every_r: float = 1.0
    add_size: float = 1.0
    max_open_risk_r: float = 0.0  # after an add the whole position risks at most this
    trail: Literal["swing", "none"] = "swing"
    exit_on_reversal: bool = True  # exit at the close of a reversal bar against the position
    flat_before_close_min: int = 5  # flat this many minutes before the cash close
    # "No limit" on adds still has a physical limit: the margin. Total exposure (notional) may
    # not exceed balance x max_leverage. PLACEHOLDER: verify FTMO's index leverage in MT5.
    max_leverage: float = 20.0


class BrooksCfg(_Strict):
    """Inputs of docs/spec-brooks-ea.md (Lorant's spec). Section numbers in the comments.
    Defaults are the spec's; values marked GAP were not in the spec (docs/strategy.md)."""

    # §2 context filter
    ema_slope_bars: int = 5
    allow_h1: bool = False
    max_pullback_bars: int = 10
    ema_touch_avg_range: float = 0.5  # GAP: "pullback to around EMA20" = low within this x AvgRange
    range_lookback: int = 10
    range_overlap_count: int = 6
    consec_opp_bars: int = 3
    min_target_r: float = 2.0
    # GAP: the high the pullback started from is the high an H2 is expected to break (Brooks'
    # first target). true = it does not count as "in the way" for the room-to-target check.
    room_ignores_pullback_high: bool = False
    # §1/§3 signal bar
    avg_range_bars: int = 20
    sb_body_min: float = 0.5
    sb_close_pos_min: float = 0.75
    sb_tail_max: float = 0.15
    min_sb_range_pts: float = 0.0  # GAP: no value in the spec; 0 = off until the backtest says
    max_sb_range_avg: float = 1.5
    max_spread_sb_range: float = 0.15
    # §4 entry orders and timing
    max_pending_bars: int = 1
    max_campaigns_per_day: int = 3
    skip_open_bars: int = 3
    no_new_order_mins: int = 30
    # §5 invalidation
    trade_failed_setups: bool = False
    early_exit_on_strong_opp: bool = False
    # §6/§7 risk and management
    risk_per_trade_pct: float = 0.3  # Lorant: 0.3 %
    use_be: bool = True
    be_at_r: float = 1.0
    max_bars_in_trade: int = 6
    time_exit_min_r: float = 0.5
    flatten_mins: int = 15
    daily_loss_limit_pct: float = 4.0  # GAP: "below FTMO's" (5 %)
    commission_pts: float = 0.0  # GAP: FTMO index CFDs, to verify in MT5
    # §8 pyramiding and dynamic stop
    enable_pyramiding: bool = True
    add_min_r: float = 1.0
    max_adds: int = 2
    add_size_factors: list[float] = [0.5, 0.25]
    climax_mult: float = 2.5
    swing_confirm_bars: int = 2
    # §10 backtest variant: trail (A/B) or fixed_tp (C, 2R take profit, no pyramiding)
    exit_mode: Literal["trail", "fixed_tp"] = "trail"


class EngineCfg(_Strict):
    # Python strategy that evaluates each bar (app/strategies). Empty: no strategy yet, the LLM
    # reads the chart itself (prompt v2). Set: Python proposes candidates, the LLM only
    # recommends take / watch / skip for them (prompt v3, prices always from Python).
    strategy: str = ""
    prompt_version: str = "v3"


class AppConfig(_Strict):
    server_time_mode: str
    symbols: dict[str, SymbolCfg]
    sessions: dict[str, SessionCfg]
    rules: RulesCfg
    features: FeaturesCfg
    llm: LlmCfg
    news: list[NewsItem]
    news_window_min: int
    telegram: TelegramCfg
    reports: ReportsCfg
    ftmo: FtmoCfg
    monitors: MonitorsCfg
    engine: EngineCfg = EngineCfg()
    management: ManagementCfg = ManagementCfg()
    brooks: BrooksCfg = BrooksCfg()

    @field_validator("server_time_mode")
    @classmethod
    def _check_mode(cls, v: str) -> str:
        return validate_mode(v)


class Settings(BaseModel):
    """Everything the app needs: secrets, the db path and the parsed config.yaml."""

    secrets: Secrets
    config: AppConfig

    @property
    def db_path(self) -> str:
        return self.secrets.db_path


def load_settings() -> Settings:
    secrets = Secrets()
    raw = yaml.safe_load(Path(secrets.config_path).read_text())
    return Settings(secrets=secrets, config=AppConfig.model_validate(raw))


@lru_cache
def get_settings() -> Settings:
    return load_settings()

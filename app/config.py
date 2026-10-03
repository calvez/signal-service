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

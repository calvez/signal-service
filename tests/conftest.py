import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import AppConfig, Secrets, Settings
from app.main import create_app

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    raw = yaml.safe_load(open("config.example.yaml"))
    raw["ftmo"]["initial_balance"] = 80000  # the risk tests are about logic, not the real account
    secrets = Secrets(
        _env_file=None,
        ingest_token=TOKEN,
        db_path=str(tmp_path / "t.db"),
        spool_dir=str(tmp_path / "no-spool"),  # never the real one
    )
    return Settings(secrets=secrets, config=AppConfig.model_validate(raw))


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as c:
        yield c


def bars_payload(**over) -> dict:
    p = {
        "schema": 1,
        "source": "mt5",
        "account_login": 1234567,
        "server": "FTMO-Demo",
        "symbol": "GER40.cash",
        "timeframe": "M5",
        "digits": 2,
        "server_utc_offset_sec": 10800,
        "bars": [
            {
                "t": 1759480200,
                "o": 24310.5,
                "h": 24322.0,
                "l": 24301.2,
                "c": 24318.7,
                "tv": 1834,
                "sp": 120,
            },
        ],
    }
    p.update(over)
    return p


def heartbeat_payload(**over) -> dict:
    p = {
        "schema": 1,
        "ea_version": "1.00",
        "account_login": 1234567,
        "server": "FTMO-Demo",
        "company": "FTMO S.R.O.",
        "balance": 80000.0,
        "equity": 80000.0,
        "connected": True,
        "trade_allowed": False,
        "positions": 1,
        "floating_pl": -22.4,
        "currency": "EUR",
        "time_server": 1759484100,
        "server_utc_offset_sec": 10800,
    }
    p.update(over)
    return p

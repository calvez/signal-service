import yaml

from app.config import AppConfig


def test_example_config_parses():
    cfg = AppConfig.model_validate(yaml.safe_load(open("config.example.yaml")))
    assert cfg.symbols["GER40.cash"].role == "traded"
    assert cfg.sessions["eu"].tz == "Europe/Berlin"
    assert cfg.server_time_mode == "ny_plus_7"


def test_shipped_config_matches_example_except_the_model():
    shipped = yaml.safe_load(open("config.yaml"))
    example = yaml.safe_load(open("config.example.yaml"))
    shipped["llm"]["model"] = example["llm"]["model"] = None  # the only intended difference
    assert shipped == example

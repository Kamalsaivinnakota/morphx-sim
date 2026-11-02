"""Device agent configuration, read only from the "device" section of the JSON config file."""

import argparse
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class ConfigError(ValueError):
    """The config file is missing, unreadable, or holds invalid settings."""


@dataclass(frozen=True)
class Config:
    """Immutable runtime settings for one device agent process."""

    device_id: str
    interval: float
    server_url: str
    batch_size: int
    db_path: str
    request_timeout: float
    backoff_min: float
    backoff_max: float


def config_path_from_args(argv: list[str] | None = None) -> Path:
    """Return the config file to use: --config if given, otherwise config.json in the project root."""
    parser = argparse.ArgumentParser(
        prog="python -m device_agent.main",
        description="MorphX simulated blood-analyser device agent",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH, help="JSON config file")
    return parser.parse_args(argv).config


def read_section(path: Path, section: str, schema: type) -> dict[str, Any]:
    """Load one top-level object from the config file and check it has exactly the schema's keys and types."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not valid JSON: {exc}") from None
    values = data.get(section) if isinstance(data, dict) else None
    if not isinstance(values, dict):
        raise ConfigError(f"{path} has no '{section}' object")

    expected = {field.name: field.type for field in fields(schema)}
    missing = sorted(expected.keys() - values.keys())
    unknown = sorted(values.keys() - expected.keys())
    if missing:
        raise ConfigError(f"'{section}' is missing: {', '.join(missing)}")
    if unknown:
        raise ConfigError(f"'{section}' has unknown keys: {', '.join(unknown)}")
    for name, kind in expected.items():
        value = values[name]
        allowed = (int, float) if kind is float else kind
        if isinstance(value, bool) or not isinstance(value, allowed):
            raise ConfigError(f"'{section}.{name}' must be {kind.__name__}, got {value!r}")
    return values


def load_config(path: Path) -> Config:
    """Build a Config from the "device" section; relative db_path is resolved against the config file's folder."""
    values = read_section(path, "device", Config)
    config = Config(
        device_id=values["device_id"].strip(),
        interval=float(values["interval"]),
        server_url=values["server_url"].rstrip("/"),
        batch_size=values["batch_size"],
        db_path=str(path.parent / values["db_path"]),
        request_timeout=float(values["request_timeout"]),
        backoff_min=float(values["backoff_min"]),
        backoff_max=float(values["backoff_max"]),
    )
    if not config.device_id:
        raise ConfigError("'device.device_id' must not be empty")
    if min(config.interval, config.request_timeout, config.backoff_min) <= 0:
        raise ConfigError("'device.interval', 'device.request_timeout' and 'device.backoff_min' must be greater than 0")
    if config.batch_size < 1:
        raise ConfigError("'device.batch_size' must be at least 1")
    if config.backoff_max < config.backoff_min:
        raise ConfigError("'device.backoff_max' must be greater than or equal to 'device.backoff_min'")
    return config

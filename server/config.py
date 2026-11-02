"""Server configuration, read only from the "server" section of the JSON config file."""

import argparse
import json
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.json"


class ConfigError(ValueError):
    """The config file is missing, unreadable, or holds invalid settings."""


@dataclass(frozen=True)
class ServerConfig:
    """Immutable runtime settings for the server process."""

    host: str
    port: int
    db_path: str


def config_path_from_args(argv: list[str] | None = None) -> Path:
    """Return the config file to use: --config if given, otherwise config.json in the project root."""
    parser = argparse.ArgumentParser(prog="python -m server.main", description="MorphX central server")
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


def load_config(path: Path) -> ServerConfig:
    """Build a ServerConfig from the "server" section; relative db_path is resolved against the config file's folder."""
    values = read_section(path, "server", ServerConfig)
    config = ServerConfig(
        host=values["host"].strip(),
        port=values["port"],
        db_path=str(path.parent / values["db_path"]),
    )
    if not config.host:
        raise ConfigError("'server.host' must not be empty")
    if not 1 <= config.port <= 65535:
        raise ConfigError("'server.port' must be between 1 and 65535")
    return config

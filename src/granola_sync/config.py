"""Configuration loading and validation."""

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field


class WebhookConfig(BaseModel):
    """Webhook configuration."""

    url: str
    secret: str


class GranolaConfig(BaseModel):
    """Granola API configuration."""

    folders: list[str] = Field(default_factory=list)
    include_transcript: bool = True


class SyncConfig(BaseModel):
    """Sync settings."""

    interval: int = 300
    batch_size: int = 10
    retry_attempts: int = 3
    retry_delay: int = 30


class LoggingConfig(BaseModel):
    """Logging configuration."""

    level: str = "INFO"
    file: Optional[str] = None
    max_size_mb: int = 10
    backup_count: int = 3


class StateConfig(BaseModel):
    """State file configuration."""

    file: str = "~/.granola-sync/state.json"


class Config(BaseModel):
    """Main configuration model."""

    webhook: WebhookConfig
    granola: GranolaConfig = Field(default_factory=GranolaConfig)
    sync: SyncConfig = Field(default_factory=SyncConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    state: StateConfig = Field(default_factory=StateConfig)


def get_default_config_path() -> Path:
    """Get the default configuration file path."""
    return Path.home() / ".granola-sync" / "config.yaml"


def load_config(config_path: Optional[Path] = None) -> Config:
    """Load configuration from a YAML file.

    Args:
        config_path: Path to the configuration file. Defaults to ~/.granola-sync/config.yaml

    Returns:
        Loaded and validated configuration

    Raises:
        FileNotFoundError: If the config file doesn't exist
        ValueError: If the config is invalid
    """
    if config_path is None:
        config_path = get_default_config_path()

    config_path = Path(config_path).expanduser()

    if not config_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {config_path}")

    with open(config_path) as f:
        data = yaml.safe_load(f)

    return Config.model_validate(data)


def save_config(config: Config, config_path: Optional[Path] = None) -> None:
    """Save configuration to a YAML file.

    Args:
        config: Configuration to save
        config_path: Path to save to. Defaults to ~/.granola-sync/config.yaml
    """
    if config_path is None:
        config_path = get_default_config_path()

    config_path = Path(config_path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    data = config.model_dump()
    with open(config_path, "w") as f:
        yaml.dump(data, f, default_flow_style=False, sort_keys=False)

    # Set restrictive permissions for security (contains webhook secret)
    config_path.chmod(0o600)

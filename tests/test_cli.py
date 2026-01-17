"""Tests for CLI commands."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from granola_sync.cli import app
from granola_sync.config import (
    Config,
    GranolaConfig,
    StateConfig,
    SyncConfig,
    WebhookConfig,
    save_config,
)

runner = CliRunner()


class TestVersion:
    """Tests for version command."""

    def test_version_flag(self):
        """Test --version flag shows version."""
        result = runner.invoke(app, ["--version"])

        assert result.exit_code == 0
        assert "granola-sync" in result.output
        assert "1.0.0" in result.output

    def test_version_short_flag(self):
        """Test -V flag shows version."""
        result = runner.invoke(app, ["-V"])

        assert result.exit_code == 0
        assert "granola-sync" in result.output


class TestConfigCommand:
    """Tests for config command."""

    def test_config_creates_file(self, tmp_path: Path):
        """Test config command creates config file."""
        config_path = tmp_path / ".granola-sync" / "config.yaml"

        with patch("granola_sync.cli.get_default_config_path", return_value=config_path):
            result = runner.invoke(
                app,
                ["config"],
                input="https://example.com/webhook\nsecret123\nSQP,CLIENT-A\n300\ny\n",
            )

        assert result.exit_code == 0
        assert config_path.exists()
        assert "Configuration saved" in result.output

    def test_config_generate_secret(self, tmp_path: Path):
        """Test config command with --generate-secret."""
        config_path = tmp_path / ".granola-sync" / "config.yaml"

        with patch("granola_sync.cli.get_default_config_path", return_value=config_path):
            result = runner.invoke(
                app,
                ["config", "--generate-secret"],
                input="https://example.com/webhook\nSQP\n300\ny\n",
            )

        assert result.exit_code == 0
        assert "Generated secret:" in result.output


class TestSyncOnceCommand:
    """Tests for sync-once command."""

    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        """Create a test config file."""
        config = Config(
            webhook=WebhookConfig(url="https://example.com/webhook", secret="secret"),
            granola=GranolaConfig(folders=["SQP"]),
            sync=SyncConfig(interval=60),
            state=StateConfig(file=str(tmp_path / "state.json")),
        )
        config_path = tmp_path / "config.yaml"
        save_config(config, config_path)
        return config_path

    def test_sync_once_no_config(self):
        """Test sync-once fails without config."""
        result = runner.invoke(app, ["sync-once"])

        assert result.exit_code == 1
        assert "No configuration found" in result.output

    @patch("granola_sync.cli.SyncService")
    def test_sync_once_dry_run(self, mock_service_class: MagicMock, config_file: Path):
        """Test sync-once with --dry-run."""
        mock_service = MagicMock()
        mock_service.sync_once = AsyncMock(
            return_value={
                "folders_checked": 1,
                "documents_found": 2,
                "documents_new": 1,
                "documents_synced": 1,
                "documents_failed": 0,
                "by_folder": {
                    "SQP": {
                        "total": 2,
                        "new": 1,
                        "synced": 1,
                        "failed": 0,
                        "documents": [{"id": "doc1", "title": "Test", "action": "would_sync"}],
                    }
                },
            }
        )
        mock_service.close = AsyncMock()
        mock_service_class.return_value = mock_service

        result = runner.invoke(app, ["sync-once", "--config", str(config_file), "--dry-run"])

        assert result.exit_code == 0
        assert "Dry run mode" in result.output
        mock_service.sync_once.assert_called_once_with(dry_run=True)

    @patch("granola_sync.cli.SyncService")
    def test_sync_once_with_folder_override(self, mock_service_class: MagicMock, config_file: Path):
        """Test sync-once with folder override."""
        mock_service = MagicMock()
        mock_service.sync_once = AsyncMock(
            return_value={
                "folders_checked": 1,
                "documents_found": 0,
                "documents_new": 0,
                "documents_synced": 0,
                "documents_failed": 0,
                "by_folder": {},
            }
        )
        mock_service.close = AsyncMock()
        mock_service_class.return_value = mock_service

        result = runner.invoke(
            app,
            ["sync-once", "--config", str(config_file), "-f", "OTHER-FOLDER"],
        )

        assert result.exit_code == 0
        # Check that the service was created with overridden folder
        call_args = mock_service_class.call_args
        config_arg = call_args[0][0]
        assert config_arg.granola.folders == ["OTHER-FOLDER"]


class TestStatusCommand:
    """Tests for status command."""

    @pytest.fixture
    def config_with_state(self, tmp_path: Path) -> Path:
        """Create a config with some state data."""
        config = Config(
            webhook=WebhookConfig(url="https://example.com/webhook", secret="secret"),
            granola=GranolaConfig(folders=["SQP", "CLIENT-A"]),
            sync=SyncConfig(interval=60),
            state=StateConfig(file=str(tmp_path / "state.json")),
        )
        config_path = tmp_path / "config.yaml"
        save_config(config, config_path)

        # Create some state
        state_data = {
            "version": 1,
            "last_sync": "2026-01-17T10:00:00Z",
            "folders": {},
            "seen_documents": {
                "doc1": {"title": "Test 1", "folder_name": "SQP"},
                "doc2": {"title": "Test 2", "folder_name": "SQP"},
            },
            "failed_documents": {},
            "stats": {
                "total_synced": 2,
                "total_errors": 0,
                "last_error": None,
                "by_folder": {"SQP": {"synced": 2, "errors": 0}},
            },
        }
        state_path = tmp_path / "state.json"
        with open(state_path, "w") as f:
            json.dump(state_data, f)

        return config_path

    def test_status_shows_stats(self, config_with_state: Path):
        """Test status command shows statistics."""
        result = runner.invoke(app, ["status", "--config", str(config_with_state)])

        assert result.exit_code == 0
        assert "Total synced: 2" in result.output
        assert "SQP" in result.output

    def test_status_no_config(self):
        """Test status fails without config."""
        result = runner.invoke(app, ["status"])

        assert result.exit_code == 1
        assert "No configuration found" in result.output


class TestRunCommand:
    """Tests for run command."""

    @pytest.fixture
    def config_file(self, tmp_path: Path) -> Path:
        """Create a test config file."""
        config = Config(
            webhook=WebhookConfig(url="https://example.com/webhook", secret="secret"),
            granola=GranolaConfig(folders=["SQP"]),
            sync=SyncConfig(interval=1),
            state=StateConfig(file=str(tmp_path / "state.json")),
        )
        config_path = tmp_path / "config.yaml"
        save_config(config, config_path)
        return config_path

    def test_run_no_config(self):
        """Test run fails without config."""
        result = runner.invoke(app, ["run"])

        assert result.exit_code == 1
        assert "No configuration found" in result.output

    @patch("granola_sync.cli.SyncService")
    @patch("granola_sync.cli.asyncio.run")
    def test_run_starts_service(
        self, mock_asyncio_run: MagicMock, mock_service_class: MagicMock, config_file: Path
    ):
        """Test run command starts the service."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        result = runner.invoke(app, ["run", "--config", str(config_file)])

        assert result.exit_code == 0
        assert "Starting sync service" in result.output
        mock_asyncio_run.assert_called_once()

    @patch("granola_sync.cli.SyncService")
    @patch("granola_sync.cli.asyncio.run")
    def test_run_with_overrides(
        self, mock_asyncio_run: MagicMock, mock_service_class: MagicMock, config_file: Path
    ):
        """Test run command with CLI overrides."""
        mock_service = MagicMock()
        mock_service_class.return_value = mock_service

        result = runner.invoke(
            app,
            [
                "run",
                "--config",
                str(config_file),
                "-f",
                "OTHER",
                "--interval",
                "120",
            ],
        )

        assert result.exit_code == 0
        call_args = mock_service_class.call_args
        config_arg = call_args[0][0]
        assert config_arg.granola.folders == ["OTHER"]
        assert config_arg.sync.interval == 120

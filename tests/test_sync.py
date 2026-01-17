"""Tests for sync service."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from granola_sync.config import Config, GranolaConfig, StateConfig, SyncConfig, WebhookConfig
from granola_sync.state import StateManager
from granola_sync.sync import SyncService


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Create a test configuration."""
    return Config(
        webhook=WebhookConfig(url="https://example.com/webhook", secret="test-secret"),
        granola=GranolaConfig(folders=["SQP", "CLIENT-A"], include_transcript=True),
        sync=SyncConfig(interval=60, batch_size=5, retry_attempts=2, retry_delay=0),
        state=StateConfig(file=str(tmp_path / "state.json")),
    )


@pytest.fixture
def mock_granola() -> MagicMock:
    """Create a mock Granola client."""
    mock = MagicMock()
    mock.get_folders = AsyncMock()
    mock.get_documents = AsyncMock()
    mock.get_document = AsyncMock()
    mock.get_transcript = AsyncMock()
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def mock_webhook() -> MagicMock:
    """Create a mock webhook sender."""
    mock = MagicMock()
    mock.send = AsyncMock(return_value=MagicMock(status_code=200))
    mock.close = AsyncMock()
    return mock


@pytest.fixture
def state_manager(tmp_path: Path) -> StateManager:
    """Create a state manager with temp file."""
    return StateManager(str(tmp_path / "state.json"))


class TestSyncService:
    """Tests for SyncService class."""

    @pytest.mark.asyncio
    async def test_sync_once_no_documents(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test sync cycle with no documents."""
        mock_granola.get_folders.return_value = [
            {"id": "f1", "title": "SQP", "documents": []},
            {"id": "f2", "title": "CLIENT-A", "documents": []},
        ]

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once()

        assert summary["folders_checked"] == 2
        assert summary["documents_found"] == 0
        assert summary["documents_new"] == 0
        mock_webhook.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_once_new_documents(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test sync cycle with new documents."""
        mock_granola.get_folders.return_value = [
            {
                "id": "f1",
                "title": "SQP",
                "documents": [
                    {
                        "id": "doc1",
                        "title": "Sprint Planning",
                        "created_at": "2026-01-17T10:00:00Z",
                        "people": [{"display_name": "John Doe"}],
                        "last_viewed_panel": {"content": {}},
                    },
                    {
                        "id": "doc2",
                        "title": "Retrospective",
                        "created_at": "2026-01-17T11:00:00Z",
                        "people": [],
                        "last_viewed_panel": {"content": {}},
                    },
                ],
            },
        ]
        mock_granola.get_transcript.return_value = [
            {"source": "microphone", "text": "Hello"},
        ]

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once()

        assert summary["documents_found"] == 2
        assert summary["documents_new"] == 2
        assert summary["documents_synced"] == 2
        assert mock_webhook.send.call_count == 2

    @pytest.mark.asyncio
    async def test_sync_once_already_seen_documents(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test sync cycle skips already-seen documents."""
        # Pre-mark document as seen
        state_manager.mark_synced("doc1", {"title": "Test", "updated_at": "2026-01-17T10:00:00Z"}, "SQP")

        mock_granola.get_folders.return_value = [
            {
                "id": "f1",
                "title": "SQP",
                "documents": [
                    {
                        "id": "doc1",
                        "title": "Sprint Planning",
                        "created_at": "2026-01-17T10:00:00Z",
                        "updated_at": "2026-01-17T10:00:00Z",  # Same as when marked
                        "people": [],
                        "last_viewed_panel": {"content": {}},
                    },
                ],
            },
        ]

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once()

        assert summary["documents_found"] == 1
        assert summary["documents_new"] == 0
        mock_webhook.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_sync_once_updated_document(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test sync cycle processes updated documents."""
        # Pre-mark document as seen with old timestamp
        state_manager.mark_synced("doc1", {"title": "Test", "updated_at": "2026-01-17T10:00:00Z"}, "SQP")

        mock_granola.get_folders.return_value = [
            {
                "id": "f1",
                "title": "SQP",
                "documents": [
                    {
                        "id": "doc1",
                        "title": "Sprint Planning",
                        "created_at": "2026-01-17T10:00:00Z",
                        "updated_at": "2026-01-17T12:00:00Z",  # Updated!
                        "people": [],
                        "last_viewed_panel": {"content": {}},
                    },
                ],
            },
        ]
        mock_granola.get_transcript.return_value = []

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once()

        assert summary["documents_new"] == 1
        assert summary["documents_synced"] == 1
        mock_webhook.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_once_folder_not_found(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test sync cycle handles missing folders gracefully."""
        mock_granola.get_folders.return_value = [
            {"id": "f1", "title": "OTHER", "documents": []},
        ]

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once()

        # Should not raise, just log warning for missing folders
        assert summary["folders_checked"] == 2
        assert summary["by_folder"]["SQP"]["total"] == 0
        assert summary["by_folder"]["CLIENT-A"]["total"] == 0

    @pytest.mark.asyncio
    async def test_sync_once_dry_run(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test dry run doesn't send webhooks."""
        mock_granola.get_folders.return_value = [
            {
                "id": "f1",
                "title": "SQP",
                "documents": [
                    {
                        "id": "doc1",
                        "title": "Sprint Planning",
                        "created_at": "2026-01-17T10:00:00Z",
                        "people": [],
                        "last_viewed_panel": {"content": {}},
                    },
                ],
            },
        ]

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once(dry_run=True)

        assert summary["documents_new"] == 1
        assert summary["documents_synced"] == 1
        assert summary["by_folder"]["SQP"]["documents"][0]["action"] == "would_sync"
        mock_webhook.send.assert_not_called()

        # State should not be updated in dry run
        assert not state_manager.is_document_seen("doc1")

    @pytest.mark.asyncio
    async def test_sync_once_webhook_failure(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test handling of webhook failures."""
        mock_granola.get_folders.return_value = [
            {
                "id": "f1",
                "title": "SQP",
                "documents": [
                    {
                        "id": "doc1",
                        "title": "Sprint Planning",
                        "created_at": "2026-01-17T10:00:00Z",
                        "people": [],
                        "last_viewed_panel": {"content": {}},
                    },
                ],
            },
        ]
        mock_granola.get_transcript.return_value = []
        mock_webhook.send.side_effect = httpx.HTTPStatusError(
            "Server error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once()

        assert summary["documents_failed"] == 1
        assert "doc1" in state_manager.get_failed_documents()

    @pytest.mark.asyncio
    async def test_sync_once_batch_limit(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test batch size limits documents processed."""
        # Create 10 documents but batch size is 5
        mock_granola.get_folders.return_value = [
            {
                "id": "f1",
                "title": "SQP",
                "documents": [
                    {
                        "id": f"doc{i}",
                        "title": f"Meeting {i}",
                        "created_at": "2026-01-17T10:00:00Z",
                        "people": [],
                        "last_viewed_panel": {"content": {}},
                    }
                    for i in range(10)
                ],
            },
        ]
        mock_granola.get_transcript.return_value = []

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        summary = await service.sync_once()

        assert summary["documents_new"] == 10
        assert summary["documents_synced"] == 5  # Limited by batch_size
        assert mock_webhook.send.call_count == 5


class TestBuildPayload:
    """Tests for payload building."""

    @pytest.mark.asyncio
    async def test_build_payload_basic(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test basic payload structure."""
        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )

        doc = {
            "id": "doc123",
            "title": "Sprint Planning",
            "created_at": "2026-01-17T10:00:00Z",
            "people": [{"display_name": "John Doe"}, {"name": "Jane Smith"}],
            "last_viewed_panel": {"content": {}},
        }

        payload = service._build_payload(doc, "SQP", None)

        assert payload["source"] == "Granola"
        assert payload["folder_name"] == "SQP"
        assert payload["note_id"] == "doc123"
        assert payload["title"] == "Sprint Planning"
        assert payload["meeting_started_at"] == "2026-01-17T10:00:00Z"
        assert payload["participants"] == ["John Doe", "Jane Smith"]
        assert payload["url"] == "https://notes.granola.ai/d/doc123"

    @pytest.mark.asyncio
    async def test_build_payload_with_transcript(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test payload includes formatted transcript."""
        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )

        doc = {
            "id": "doc123",
            "title": "Meeting",
            "created_at": "2026-01-17T10:00:00Z",
            "people": [],
            "last_viewed_panel": {"content": {}},
        }
        transcript = [
            {"source": "microphone", "text": "Hello everyone"},
            {"source": "speaker", "text": "Hi there"},
            {"source": "microphone", "text": "Let's begin"},
        ]

        payload = service._build_payload(doc, "SQP", transcript)

        assert "Me: Hello everyone" in payload["transcript"]
        assert "Them: Hi there" in payload["transcript"]
        assert "Me: Let's begin" in payload["transcript"]


class TestProseMirrorToText:
    """Tests for ProseMirror content conversion."""

    @pytest.fixture
    def service(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ) -> SyncService:
        """Create a sync service for testing."""
        return SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )

    def test_empty_content(self, service: SyncService):
        """Test handling of empty content."""
        assert service._prosemirror_to_text({}) == ""
        assert service._prosemirror_to_text(None) == ""

    def test_simple_paragraph(self, service: SyncService):
        """Test conversion of simple paragraph."""
        content = {
            "type": "doc",
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": "Hello world"}],
                }
            ],
        }

        text = service._prosemirror_to_text(content)
        assert "Hello world" in text

    def test_heading(self, service: SyncService):
        """Test conversion of heading."""
        content = {
            "type": "doc",
            "content": [
                {
                    "type": "heading",
                    "attrs": {"level": 2},
                    "content": [{"type": "text", "text": "Summary"}],
                }
            ],
        }

        text = service._prosemirror_to_text(content)
        assert "## Summary" in text

    def test_bullet_list(self, service: SyncService):
        """Test conversion of bullet list."""
        content = {
            "type": "doc",
            "content": [
                {
                    "type": "bulletList",
                    "content": [
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item 1"}],
                                }
                            ],
                        },
                        {
                            "type": "listItem",
                            "content": [
                                {
                                    "type": "paragraph",
                                    "content": [{"type": "text", "text": "Item 2"}],
                                }
                            ],
                        },
                    ],
                }
            ],
        }

        text = service._prosemirror_to_text(content)
        assert "- " in text or "Item 1" in text

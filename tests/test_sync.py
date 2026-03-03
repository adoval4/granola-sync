"""Tests for sync service."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from granola_sync.config import Config, GranolaConfig, StateConfig, SyncConfig, WebhookConfig
from granola_sync.state import StateManager
from granola_sync.sync import SyncService


@pytest.fixture
def config(tmp_path: Path) -> Config:
    """Create a test configuration with explicit folder IDs."""
    return Config(
        webhook=WebhookConfig(url="https://example.com/webhook", secret="test-secret"),
        granola=GranolaConfig(
            folders=["SQP", "CLIENT-A"],
            folder_ids={"SQP": "sqp-folder-id", "CLIENT-A": "client-a-folder-id"},
            include_transcript=True,
        ),
        sync=SyncConfig(interval=60, batch_size=5, retry_attempts=2, retry_delay=0),
        state=StateConfig(file=str(tmp_path / "state.json")),
    )


@pytest.fixture
def mock_granola() -> MagicMock:
    """Create a mock Granola client."""
    mock = MagicMock()
    mock.get_folders = AsyncMock()
    mock.get_documents = AsyncMock(return_value=[])
    mock.get_documents_by_folder = AsyncMock(return_value=[])
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


def _make_doc(doc_id="doc1", title="Sprint Planning", notes_markdown="Some notes", **kwargs):
    """Helper to create a document dict for testing."""
    doc = {
        "id": doc_id,
        "title": title,
        "created_at": "2026-01-17T10:00:00Z",
        "updated_at": kwargs.pop("updated_at", "2026-01-17T10:00:00Z"),
        "notes_markdown": notes_markdown,
        "notes_plain": kwargs.pop("notes_plain", notes_markdown),
        "notes": kwargs.pop("notes", {"type": "doc", "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": notes_markdown}]}
        ]} if notes_markdown else {"type": "doc", "content": [{"type": "paragraph"}]}),
        "people": kwargs.pop("people", []),
        "last_viewed_panel": kwargs.pop("last_viewed_panel", {"content": {}}),
    }
    doc.update(kwargs)
    return doc


class TestSyncService:
    """Tests for SyncService class."""

    @pytest.mark.asyncio
    async def test_sync_once_no_documents(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test sync cycle with no documents in folders."""
        mock_granola.get_documents_by_folder.return_value = []

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
        """Test sync cycle with new documents that have content."""
        docs = [
            _make_doc("doc1", "Sprint Planning", "Notes for sprint"),
            _make_doc("doc2", "Retrospective", "Retro notes"),
        ]
        mock_granola.get_documents_by_folder.return_value = docs
        mock_granola.get_transcript.return_value = [
            {"source": "microphone", "text": "Hello"},
        ]

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        # Override to only check SQP folder
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(
                folders=["SQP"],
                folder_ids={"SQP": "sqp-folder-id"},
                include_transcript=True,
            ),
            sync=config.sync,
            state=config.state,
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
        state_manager.mark_synced("doc1", {"title": "Test", "updated_at": "2026-01-17T10:00:00Z"}, "SQP")

        docs = [_make_doc("doc1", "Sprint Planning", "Notes", updated_at="2026-01-17T10:00:00Z")]
        mock_granola.get_documents_by_folder.return_value = docs

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}),
            sync=config.sync,
            state=config.state,
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
        state_manager.mark_synced("doc1", {"title": "Test", "updated_at": "2026-01-17T10:00:00Z"}, "SQP")

        docs = [_make_doc("doc1", "Sprint Planning", "Updated notes", updated_at="2026-01-17T12:00:00Z")]
        mock_granola.get_documents_by_folder.return_value = docs
        mock_granola.get_transcript.return_value = []

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}, include_transcript=True),
            sync=config.sync,
            state=config.state,
        )
        summary = await service.sync_once()

        assert summary["documents_new"] == 1
        assert summary["documents_synced"] == 1
        mock_webhook.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_once_folder_not_resolved(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test sync cycle handles unresolvable folder IDs gracefully."""
        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        # No folder_ids configured, no cache available
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["UNKNOWN"], folder_ids={}),
            sync=config.sync,
            state=config.state,
        )

        with patch("granola_sync.sync.GranolaCacheReader") as mock_cache_cls:
            mock_cache_cls.return_value.get_folder_map.side_effect = FileNotFoundError("no cache")
            summary = await service.sync_once()

        assert summary["folders_checked"] == 1
        assert summary["by_folder"]["UNKNOWN"]["total"] == 0

    @pytest.mark.asyncio
    async def test_sync_once_dry_run(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test dry run doesn't send webhooks."""
        docs = [_make_doc("doc1", "Sprint Planning", "Notes")]
        mock_granola.get_documents_by_folder.return_value = docs

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}),
            sync=config.sync,
            state=config.state,
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
        docs = [_make_doc("doc1", "Sprint Planning", "Notes")]
        mock_granola.get_documents_by_folder.return_value = docs
        mock_granola.get_transcript.return_value = []
        mock_webhook.send.side_effect = httpx.HTTPStatusError(
            "Server error",
            request=MagicMock(),
            response=MagicMock(status_code=500),
        )

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}, include_transcript=True),
            sync=config.sync,
            state=config.state,
        )
        summary = await service.sync_once()

        assert summary["documents_failed"] == 1
        assert "doc1" in state_manager.get_failed_documents()

    @pytest.mark.asyncio
    async def test_sync_once_batch_limit(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test batch size limits documents processed."""
        docs = [_make_doc(f"doc{i}", f"Meeting {i}", f"Notes {i}") for i in range(10)]
        mock_granola.get_documents_by_folder.return_value = docs
        mock_granola.get_transcript.return_value = []

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}, include_transcript=True),
            sync=SyncConfig(interval=60, batch_size=5, retry_attempts=2, retry_delay=0),
            state=config.state,
        )
        summary = await service.sync_once()

        assert summary["documents_new"] == 10
        assert summary["documents_synced"] == 5  # Limited by batch_size
        assert mock_webhook.send.call_count == 5

    @pytest.mark.asyncio
    async def test_sync_once_pending_content(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test documents without content are marked as pending."""
        # Document with no notes content
        empty_doc = _make_doc("doc1", "In Progress Meeting", notes_markdown="", notes_plain="",
                              notes={"type": "doc", "content": [{"type": "paragraph"}]})
        mock_granola.get_documents_by_folder.return_value = [empty_doc]

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}),
            sync=config.sync,
            state=config.state,
        )
        summary = await service.sync_once()

        assert summary["documents_pending"] == 1
        assert summary["documents_synced"] == 0
        assert state_manager.is_document_pending("doc1")
        mock_webhook.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_recheck_pending_syncs_when_content_appears(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test that pending documents are synced when content appears."""
        # Pre-mark document as pending
        state_manager.mark_pending("doc1", {"title": "Meeting"}, "SQP")

        # API now returns the document with content
        doc_with_content = _make_doc("doc1", "Meeting", "Now has notes!")
        mock_granola.get_documents.return_value = [doc_with_content]
        mock_granola.get_documents_by_folder.return_value = []
        mock_granola.get_transcript.return_value = []

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}, include_transcript=True),
            sync=config.sync,
            state=config.state,
        )
        summary = await service.sync_once()

        # The pending document should have been synced during recheck
        assert summary["documents_synced"] == 1
        assert not state_manager.is_document_pending("doc1")

    @pytest.mark.asyncio
    async def test_api_fallback_to_cache(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test that API failure falls back to cache for documents."""
        mock_granola.get_documents_by_folder.side_effect = httpx.HTTPStatusError(
            "Server error", request=MagicMock(), response=MagicMock(status_code=500)
        )

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={"SQP": "sqp-folder-id"}),
            sync=config.sync,
            state=config.state,
        )

        cache_docs = [_make_doc("doc1", "Cached Meeting", "Cached notes")]
        with patch("granola_sync.sync.GranolaCacheReader") as mock_cache_cls:
            mock_cache_cls.return_value.get_folder_map.return_value = {}
            mock_cache_cls.return_value.get_documents_for_folder.return_value = cache_docs
            mock_granola.get_transcript.return_value = []
            summary = await service.sync_once()

        assert summary["documents_found"] == 1
        assert summary["documents_synced"] == 1


class TestFolderMapResolution:
    """Tests for folder name → ID resolution."""

    @pytest.mark.asyncio
    async def test_config_folder_ids_take_priority(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test that config folder_ids override all other sources."""
        # State has a different ID for SQP
        state_manager.update_folder_map({"SQP": "old-state-id"})

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        folder_map = service._resolve_folder_map()

        # Config value should win
        assert folder_map["SQP"] == "sqp-folder-id"

    @pytest.mark.asyncio
    async def test_state_folder_map_used_when_no_config(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test state folder map is used when config has no folder_ids."""
        state_manager.update_folder_map({"SQP": "state-folder-id"})

        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={}),
            sync=config.sync,
            state=config.state,
        )

        with patch("granola_sync.sync.GranolaCacheReader") as mock_cache_cls:
            mock_cache_cls.return_value.get_folder_map.side_effect = FileNotFoundError("no cache")
            folder_map = service._resolve_folder_map()

        assert folder_map["SQP"] == "state-folder-id"

    @pytest.mark.asyncio
    async def test_cache_folder_map_persists_to_state(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test that cache-derived folder map is persisted in state."""
        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )
        service.config = Config(
            webhook=config.webhook,
            granola=GranolaConfig(folders=["SQP"], folder_ids={}),
            sync=config.sync,
            state=config.state,
        )

        with patch("granola_sync.sync.GranolaCacheReader") as mock_cache_cls:
            mock_cache_cls.return_value.get_folder_map.return_value = {"SQP": "cache-folder-id"}
            folder_map = service._resolve_folder_map()

        assert folder_map["SQP"] == "cache-folder-id"
        assert state_manager.get_folder_map()["SQP"] == "cache-folder-id"


class TestContentDetection:
    """Tests for _has_content and content gating."""

    @pytest.fixture
    def service(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ) -> SyncService:
        return SyncService(config, granola=mock_granola, webhook=mock_webhook, state=state_manager)

    def test_has_content_with_markdown(self, service: SyncService):
        """Test document with notes_markdown is detected as having content."""
        doc = _make_doc(notes_markdown="Some notes")
        assert service._has_content(doc) is True

    def test_has_content_with_plain_text(self, service: SyncService):
        """Test document with notes_plain is detected as having content."""
        doc = _make_doc(notes_markdown="", notes_plain="Some plain text")
        assert service._has_content(doc) is True

    def test_has_content_with_prosemirror(self, service: SyncService):
        """Test document with ProseMirror text is detected as having content."""
        doc = _make_doc(
            notes_markdown="", notes_plain="",
            notes={"type": "doc", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Real content"}]}
            ]},
        )
        assert service._has_content(doc) is True

    def test_no_content_empty_prosemirror(self, service: SyncService):
        """Test document with empty ProseMirror is detected as no content."""
        doc = _make_doc(
            notes_markdown="", notes_plain="",
            notes={"type": "doc", "content": [{"type": "paragraph"}]},
        )
        assert service._has_content(doc) is False

    def test_no_content_all_empty(self, service: SyncService):
        """Test document with no notes at all."""
        doc = {"id": "doc1", "title": "Empty", "notes_markdown": "", "notes_plain": "", "notes": None}
        assert service._has_content(doc) is False

    def test_has_content_with_last_viewed_panel_string(self, service: SyncService):
        """Test document with string content in last_viewed_panel."""
        doc = {
            "id": "doc1", "title": "Test",
            "notes_markdown": "", "notes_plain": "",
            "notes": {"type": "doc", "content": [{"type": "paragraph"}]},
            "last_viewed_panel": {"content": "Some cached content"},
        }
        assert service._has_content(doc) is True


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

    @pytest.mark.asyncio
    async def test_build_payload_with_new_api_people(
        self, config: Config, mock_granola: MagicMock, mock_webhook: MagicMock, state_manager: StateManager
    ):
        """Test payload handles new API people structure (dict with attendees)."""
        service = SyncService(
            config, granola=mock_granola, webhook=mock_webhook, state=state_manager
        )

        doc = {
            "id": "doc123",
            "title": "Meeting",
            "created_at": "2026-01-17T10:00:00Z",
            "people": {
                "attendees": [
                    {"name": "Alice", "email": "alice@example.com"},
                    {"name": "", "email": "bob@example.com"},
                ],
            },
            "last_viewed_panel": {"content": {}},
        }

        payload = service._build_payload(doc, "SQP", None)
        assert "Alice" in payload["participants"]
        assert "bob@example.com" in payload["participants"]


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

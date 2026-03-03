"""Tests for state manager."""

import json
from pathlib import Path

import pytest

from granola_sync.state import StateManager


class TestStateManager:
    """Tests for StateManager class."""

    @pytest.fixture
    def state_file(self, tmp_path: Path) -> Path:
        """Create a temporary state file path."""
        return tmp_path / "state.json"

    @pytest.fixture
    def manager(self, state_file: Path) -> StateManager:
        """Create a state manager with a temp file."""
        return StateManager(str(state_file))

    def test_creates_default_state(self, manager: StateManager, state_file: Path):
        """Test that default state is created when file doesn't exist."""
        assert not state_file.exists()
        assert manager._state["version"] == 2
        assert manager._state["seen_documents"] == {}
        assert manager._state["failed_documents"] == {}
        assert manager._state["folder_map"] == {}
        assert manager._state["pending_documents"] == {}

    def test_save_creates_file(self, manager: StateManager, state_file: Path):
        """Test that save creates the state file."""
        manager.save()

        assert state_file.exists()
        with open(state_file) as f:
            data = json.load(f)
        assert data["version"] == 2
        assert "last_sync" in data

    def test_load_existing_state(self, state_file: Path):
        """Test loading existing state from file."""
        existing_state = {
            "version": 1,
            "last_sync": "2026-01-17T10:00:00Z",
            "folders": {},
            "seen_documents": {
                "doc1": {"title": "Test", "folder_name": "SQP"},
            },
            "failed_documents": {},
            "stats": {"total_synced": 1, "total_errors": 0, "last_error": None, "by_folder": {}},
        }
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(existing_state, f)

        manager = StateManager(str(state_file))

        assert "doc1" in manager._state["seen_documents"]
        assert manager._state["seen_documents"]["doc1"]["title"] == "Test"

    def test_is_document_seen_false(self, manager: StateManager):
        """Test is_document_seen returns False for new document."""
        assert manager.is_document_seen("new_doc") is False

    def test_is_document_seen_true(self, manager: StateManager):
        """Test is_document_seen returns True for synced document."""
        manager.mark_synced(
            "doc1",
            {"title": "Test Meeting", "created_at": "2026-01-17T10:00:00Z"},
            "SQP",
        )

        assert manager.is_document_seen("doc1") is True

    def test_is_document_updated_new(self, manager: StateManager):
        """Test is_document_updated returns True for new document."""
        assert manager.is_document_updated("doc1", "2026-01-17T10:00:00Z") is True

    def test_is_document_updated_same(self, manager: StateManager):
        """Test is_document_updated returns False when unchanged."""
        manager.mark_synced(
            "doc1",
            {"title": "Test", "updated_at": "2026-01-17T10:00:00Z"},
            "SQP",
        )

        assert manager.is_document_updated("doc1", "2026-01-17T10:00:00Z") is False

    def test_is_document_updated_changed(self, manager: StateManager):
        """Test is_document_updated returns True when changed."""
        manager.mark_synced(
            "doc1",
            {"title": "Test", "updated_at": "2026-01-17T10:00:00Z"},
            "SQP",
        )

        assert manager.is_document_updated("doc1", "2026-01-17T11:00:00Z") is True

    def test_mark_synced(self, manager: StateManager):
        """Test marking a document as synced."""
        doc = {
            "title": "Sprint Planning",
            "created_at": "2026-01-17T10:00:00Z",
            "updated_at": "2026-01-17T10:30:00Z",
        }
        manager.mark_synced("doc1", doc, "SQP")

        assert "doc1" in manager._state["seen_documents"]
        seen = manager._state["seen_documents"]["doc1"]
        assert seen["title"] == "Sprint Planning"
        assert seen["folder_name"] == "SQP"
        assert seen["webhook_status"] == "success"
        assert manager._state["stats"]["total_synced"] == 1
        assert manager._state["stats"]["by_folder"]["SQP"]["synced"] == 1

    def test_mark_synced_removes_from_failed(self, manager: StateManager):
        """Test that marking synced removes from failed documents."""
        manager.mark_failed("doc1", "Connection error", "SQP")
        assert "doc1" in manager._state["failed_documents"]

        manager.mark_synced("doc1", {"title": "Test"}, "SQP")

        assert "doc1" not in manager._state["failed_documents"]
        assert "doc1" in manager._state["seen_documents"]

    def test_mark_failed(self, manager: StateManager):
        """Test marking a document as failed."""
        manager.mark_failed("doc1", "Connection timeout", "SQP", {"title": "Test Meeting"})

        assert "doc1" in manager._state["failed_documents"]
        failed = manager._state["failed_documents"]["doc1"]
        assert failed["title"] == "Test Meeting"
        assert failed["folder_name"] == "SQP"
        assert failed["attempts"] == 1
        assert failed["last_error"] == "Connection timeout"
        assert manager._state["stats"]["total_errors"] == 1

    def test_mark_failed_increments_attempts(self, manager: StateManager):
        """Test that repeated failures increment attempt count."""
        manager.mark_failed("doc1", "Error 1", "SQP")
        manager.mark_failed("doc1", "Error 2", "SQP")
        manager.mark_failed("doc1", "Error 3", "SQP")

        failed = manager._state["failed_documents"]["doc1"]
        assert failed["attempts"] == 3
        assert failed["last_error"] == "Error 3"

    def test_update_folder(self, manager: StateManager):
        """Test updating folder metadata."""
        manager.update_folder("SQP", "folder123")

        assert "SQP" in manager._state["folders"]
        folder = manager._state["folders"]["SQP"]
        assert folder["folder_id"] == "folder123"
        assert "last_sync" in folder

    def test_get_failed_documents(self, manager: StateManager):
        """Test getting failed documents."""
        manager.mark_failed("doc1", "Error 1", "SQP")
        manager.mark_failed("doc2", "Error 2", "CLIENT-A")

        failed = manager.get_failed_documents()

        assert len(failed) == 2
        assert "doc1" in failed
        assert "doc2" in failed

    def test_get_stats(self, manager: StateManager):
        """Test getting statistics."""
        manager.mark_synced("doc1", {"title": "Test 1"}, "SQP")
        manager.mark_synced("doc2", {"title": "Test 2"}, "SQP")
        manager.mark_failed("doc3", "Error", "CLIENT-A")

        stats = manager.get_stats()

        assert stats["total_synced"] == 2
        assert stats["total_errors"] == 1
        assert stats["by_folder"]["SQP"]["synced"] == 2
        assert stats["by_folder"]["CLIENT-A"]["errors"] == 1

    def test_get_seen_document_ids(self, manager: StateManager):
        """Test getting seen document IDs."""
        manager.mark_synced("doc1", {"title": "Test 1"}, "SQP")
        manager.mark_synced("doc2", {"title": "Test 2"}, "SQP")

        seen = manager.get_seen_document_ids()

        assert seen == {"doc1", "doc2"}

    def test_clear(self, manager: StateManager):
        """Test clearing state."""
        manager.mark_synced("doc1", {"title": "Test"}, "SQP")
        manager.mark_failed("doc2", "Error", "SQP")

        manager.clear()

        assert manager._state["seen_documents"] == {}
        assert manager._state["failed_documents"] == {}
        assert manager._state["stats"]["total_synced"] == 0

    def test_persistence(self, state_file: Path):
        """Test that state persists across manager instances."""
        manager1 = StateManager(str(state_file))
        manager1.mark_synced("doc1", {"title": "Persistent Doc"}, "SQP")
        manager1.save()

        manager2 = StateManager(str(state_file))

        assert manager2.is_document_seen("doc1") is True
        assert manager2._state["seen_documents"]["doc1"]["title"] == "Persistent Doc"

    def test_handles_corrupt_state_file(self, state_file: Path):
        """Test handling of corrupt state file."""
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            f.write("not valid json {{{")

        manager = StateManager(str(state_file))

        # Should fall back to default state
        assert manager._state["version"] == 2
        assert manager._state["seen_documents"] == {}

    def test_migrate_v1_to_v2(self, state_file: Path):
        """Test that v1 state is migrated to v2 with new fields."""
        v1_state = {
            "version": 1,
            "last_sync": "2026-01-17T10:00:00Z",
            "folders": {},
            "seen_documents": {"doc1": {"title": "Test"}},
            "failed_documents": {},
            "stats": {"total_synced": 1, "total_errors": 0, "last_error": None, "by_folder": {}},
        }
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            json.dump(v1_state, f)

        manager = StateManager(str(state_file))

        assert manager._state["version"] == 2
        assert manager._state["folder_map"] == {}
        assert manager._state["pending_documents"] == {}
        # Existing data preserved
        assert "doc1" in manager._state["seen_documents"]

    def test_folder_map_operations(self, manager: StateManager):
        """Test folder map get/update operations."""
        assert manager.get_folder_map() == {}

        manager.update_folder_map({"SQP": "folder-id-1", "CAL": "folder-id-2"})
        assert manager.get_folder_map() == {"SQP": "folder-id-1", "CAL": "folder-id-2"}

        # Merge additional entries
        manager.update_folder_map({"NEW": "folder-id-3"})
        assert len(manager.get_folder_map()) == 3

    def test_pending_document_lifecycle(self, manager: StateManager):
        """Test pending document mark/get/clear lifecycle."""
        assert manager.get_pending_documents() == []
        assert manager.is_document_pending("doc1") is False

        manager.mark_pending("doc1", {"title": "Meeting"}, "SQP")
        assert manager.is_document_pending("doc1") is True

        pending = manager.get_pending_documents()
        assert len(pending) == 1
        assert pending[0]["doc_id"] == "doc1"
        assert pending[0]["title"] == "Meeting"
        assert pending[0]["folder_name"] == "SQP"
        assert pending[0]["check_count"] == 1

        # Re-check increments count
        manager.mark_pending("doc1", {"title": "Meeting"}, "SQP")
        pending = manager.get_pending_documents()
        assert pending[0]["check_count"] == 2

        # Clear removes it
        manager.clear_pending("doc1")
        assert manager.is_document_pending("doc1") is False
        assert manager.get_pending_documents() == []

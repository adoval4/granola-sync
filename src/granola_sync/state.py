"""JSON state management for tracking synced documents."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import structlog

logger = structlog.get_logger()


class StateManager:
    """Manages persistent state for tracking synced documents."""

    def __init__(self, state_file: str = "~/.granola-sync/state.json"):
        """Initialize the state manager.

        Args:
            state_file: Path to the state file
        """
        self.state_file = Path(state_file).expanduser()
        self._state: dict[str, Any] = self._default_state()
        self._load()

    def _default_state(self) -> dict[str, Any]:
        """Get the default state structure."""
        return {
            "version": 2,
            "last_sync": None,
            "folders": {},
            "folder_map": {},
            "seen_documents": {},
            "failed_documents": {},
            "pending_documents": {},
            "stats": {
                "total_synced": 0,
                "total_errors": 0,
                "last_error": None,
                "by_folder": {},
            },
        }

    def _load(self) -> None:
        """Load state from file."""
        if not self.state_file.exists():
            logger.debug("state_file_not_found", path=str(self.state_file))
            return

        try:
            with open(self.state_file) as f:
                data = json.load(f)
            self._state = data
            self._migrate()
            logger.debug("state_loaded", path=str(self.state_file))
        except (json.JSONDecodeError, IOError) as e:
            logger.warning("state_load_error", error=str(e), path=str(self.state_file))
            # Keep default state

    def _migrate(self) -> None:
        """Migrate state from older versions."""
        version = self._state.get("version", 1)
        if version < 2:
            self._state.setdefault("folder_map", {})
            self._state.setdefault("pending_documents", {})
            self._state["version"] = 2
            logger.info("state_migrated", from_version=version, to_version=2)

    def save(self) -> None:
        """Save state to file."""
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        # Update last_sync timestamp
        self._state["last_sync"] = datetime.now(timezone.utc).isoformat()

        with open(self.state_file, "w") as f:
            json.dump(self._state, f, indent=2)

        logger.debug("state_saved", path=str(self.state_file))

    def is_document_seen(self, doc_id: str) -> bool:
        """Check if a document has been synced.

        Args:
            doc_id: The document ID

        Returns:
            True if the document has been successfully synced
        """
        return doc_id in self._state["seen_documents"]

    def is_document_updated(self, doc_id: str, updated_at: str) -> bool:
        """Check if a document has been updated since last sync.

        Args:
            doc_id: The document ID
            updated_at: The document's last updated timestamp

        Returns:
            True if the document has been updated since last sync
        """
        if doc_id not in self._state["seen_documents"]:
            return True

        seen = self._state["seen_documents"][doc_id]
        return seen.get("last_updated") != updated_at

    def mark_synced(
        self,
        doc_id: str,
        doc: dict[str, Any],
        folder_name: str,
    ) -> None:
        """Mark a document as successfully synced.

        Args:
            doc_id: The document ID
            doc: The document data
            folder_name: The folder the document belongs to
        """
        now = datetime.now(timezone.utc).isoformat()

        self._state["seen_documents"][doc_id] = {
            "title": doc.get("title", "Untitled"),
            "folder_name": folder_name,
            "first_seen": self._state["seen_documents"].get(doc_id, {}).get("first_seen", now),
            "last_updated": doc.get("updated_at") or doc.get("created_at"),
            "synced_at": now,
            "webhook_status": "success",
        }

        # Remove from failed if it was there
        self._state["failed_documents"].pop(doc_id, None)

        # Update stats
        self._state["stats"]["total_synced"] += 1
        if folder_name not in self._state["stats"]["by_folder"]:
            self._state["stats"]["by_folder"][folder_name] = {"synced": 0, "errors": 0}
        self._state["stats"]["by_folder"][folder_name]["synced"] += 1

        logger.debug("document_marked_synced", doc_id=doc_id, folder=folder_name)

    def mark_failed(
        self,
        doc_id: str,
        error: str,
        folder_name: str,
        doc: Optional[dict[str, Any]] = None,
    ) -> None:
        """Mark a document as failed to sync.

        Args:
            doc_id: The document ID
            error: The error message
            folder_name: The folder the document belongs to
            doc: Optional document data
        """
        now = datetime.now(timezone.utc).isoformat()

        existing = self._state["failed_documents"].get(doc_id, {})
        attempts = existing.get("attempts", 0) + 1

        self._state["failed_documents"][doc_id] = {
            "title": (doc or {}).get("title") or existing.get("title", "Unknown"),
            "folder_name": folder_name,
            "attempts": attempts,
            "last_error": error,
            "last_attempt": now,
        }

        # Update stats
        self._state["stats"]["total_errors"] += 1
        self._state["stats"]["last_error"] = now
        if folder_name not in self._state["stats"]["by_folder"]:
            self._state["stats"]["by_folder"][folder_name] = {"synced": 0, "errors": 0}
        self._state["stats"]["by_folder"][folder_name]["errors"] += 1

        logger.debug(
            "document_marked_failed",
            doc_id=doc_id,
            folder=folder_name,
            attempts=attempts,
            error=error,
        )

    def update_folder(self, folder_name: str, folder_id: str) -> None:
        """Update folder metadata.

        Args:
            folder_name: The folder name
            folder_id: The folder ID
        """
        now = datetime.now(timezone.utc).isoformat()

        if folder_name not in self._state["folders"]:
            self._state["folders"][folder_name] = {}

        self._state["folders"][folder_name].update({
            "folder_id": folder_id,
            "last_sync": now,
        })

    def get_failed_documents(self) -> dict[str, Any]:
        """Get all failed documents.

        Returns:
            Dictionary of failed documents
        """
        return self._state["failed_documents"].copy()

    def get_stats(self) -> dict[str, Any]:
        """Get sync statistics.

        Returns:
            Dictionary of statistics
        """
        return self._state["stats"].copy()

    def get_seen_document_ids(self) -> set[str]:
        """Get IDs of all seen documents.

        Returns:
            Set of document IDs
        """
        return set(self._state["seen_documents"].keys())

    def get_folder_map(self) -> dict[str, str]:
        """Get the persisted folder name → ID mapping.

        Returns:
            Dict mapping folder titles to their IDs
        """
        return self._state.get("folder_map", {}).copy()

    def update_folder_map(self, mapping: dict[str, str]) -> None:
        """Merge new folder name → ID entries into the persisted map.

        Args:
            mapping: Dict mapping folder titles to their IDs
        """
        self._state.setdefault("folder_map", {}).update(mapping)
        logger.debug("folder_map_updated", count=len(mapping))

    def mark_pending(
        self,
        doc_id: str,
        doc: dict[str, Any],
        folder_name: str,
    ) -> None:
        """Mark a document as pending content (has no notes yet).

        Args:
            doc_id: The document ID
            doc: The document data
            folder_name: The folder the document belongs to
        """
        now = datetime.now(timezone.utc).isoformat()
        existing = self._state["pending_documents"].get(doc_id, {})

        self._state["pending_documents"][doc_id] = {
            "title": doc.get("title", "Untitled"),
            "folder_name": folder_name,
            "first_seen": existing.get("first_seen", now),
            "last_checked": now,
            "check_count": existing.get("check_count", 0) + 1,
        }
        logger.debug("document_marked_pending", doc_id=doc_id, folder=folder_name)

    def get_pending_documents(self) -> list[dict[str, Any]]:
        """Get all documents pending content.

        Returns:
            List of pending document info dicts (includes doc_id key)
        """
        return [
            {"doc_id": doc_id, **info}
            for doc_id, info in self._state.get("pending_documents", {}).items()
        ]

    def clear_pending(self, doc_id: str) -> None:
        """Remove a document from the pending list.

        Args:
            doc_id: The document ID to clear
        """
        self._state.get("pending_documents", {}).pop(doc_id, None)
        logger.debug("pending_cleared", doc_id=doc_id)

    def is_document_pending(self, doc_id: str) -> bool:
        """Check if a document is in the pending content list.

        Args:
            doc_id: The document ID

        Returns:
            True if the document is pending content
        """
        return doc_id in self._state.get("pending_documents", {})

    def clear(self) -> None:
        """Clear all state (useful for testing or resetting)."""
        self._state = self._default_state()
        logger.info("state_cleared")

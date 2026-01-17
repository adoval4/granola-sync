"""Main sync loop logic."""

import asyncio
from typing import Any, Optional

import structlog

from .config import Config
from .granola_api import GranolaClient
from .state import StateManager
from .webhook import WebhookSender

logger = structlog.get_logger()


class SyncService:
    """Main service for syncing Granola documents to a webhook."""

    def __init__(
        self,
        config: Config,
        granola: Optional[GranolaClient] = None,
        webhook: Optional[WebhookSender] = None,
        state: Optional[StateManager] = None,
    ):
        """Initialize the sync service.

        Args:
            config: Service configuration
            granola: Optional Granola API client (for testing)
            webhook: Optional webhook sender (for testing)
            state: Optional state manager (for testing)
        """
        self.config = config
        self.granola = granola or GranolaClient()
        self.webhook = webhook or WebhookSender(
            url=config.webhook.url,
            secret=config.webhook.secret,
            retry_attempts=config.sync.retry_attempts,
            retry_delay=config.sync.retry_delay,
        )
        self.state = state or StateManager(config.state.file)
        self._running = False

    async def run(self) -> None:
        """Main sync loop - runs until stopped."""
        self._running = True
        logger.info("sync_started", folders=self.config.granola.folders)

        try:
            while self._running:
                try:
                    await self.sync_once()
                except Exception as e:
                    logger.error("sync_error", error=str(e))

                await asyncio.sleep(self.config.sync.interval)
        finally:
            await self.close()

    async def sync_once(self, dry_run: bool = False) -> dict[str, Any]:
        """Perform a single sync cycle across all configured folders.

        Args:
            dry_run: If True, don't send webhooks, just report what would be synced

        Returns:
            Summary of the sync cycle
        """
        logger.info("sync_cycle_started", folders=self.config.granola.folders, dry_run=dry_run)

        summary: dict[str, Any] = {
            "folders_checked": 0,
            "documents_found": 0,
            "documents_new": 0,
            "documents_synced": 0,
            "documents_failed": 0,
            "by_folder": {},
        }

        try:
            # 1. Get all folder metadata
            all_folders = await self.granola.get_folders()

            # 2. Get all documents once (more efficient than per-folder)
            documents = await self.granola.get_documents(limit=100)
            summary["documents_found"] = len(documents)

            # 3. Process each configured folder
            for folder_name in self.config.granola.folders:
                folder_summary = await self._sync_folder(
                    folder_name, all_folders, documents, dry_run=dry_run
                )
                summary["by_folder"][folder_name] = folder_summary
                summary["folders_checked"] += 1
                summary["documents_new"] += folder_summary["new"]
                summary["documents_synced"] += folder_summary["synced"]
                summary["documents_failed"] += folder_summary["failed"]

            # 4. Save state (unless dry run)
            if not dry_run:
                self.state.save()

            logger.info(
                "sync_cycle_completed",
                folders=summary["folders_checked"],
                new=summary["documents_new"],
                synced=summary["documents_synced"],
                failed=summary["documents_failed"],
            )

        except Exception as e:
            logger.error("sync_cycle_error", error=str(e))
            raise

        return summary

    async def _sync_folder(
        self,
        folder_name: str,
        all_folders: list[dict[str, Any]],
        documents: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Sync a single folder.

        Args:
            folder_name: Name of the folder to sync
            all_folders: List of all folders from the API
            documents: List of all documents from the API
            dry_run: If True, don't send webhooks

        Returns:
            Summary of documents processed for this folder
        """
        summary = {"total": 0, "new": 0, "synced": 0, "failed": 0, "documents": []}

        folder = self._find_folder(all_folders, folder_name)
        if not folder:
            logger.warning("folder_not_found", name=folder_name)
            return summary

        # Update folder metadata in state
        self.state.update_folder(folder_name, folder.get("id", ""))

        # Filter documents belonging to this folder
        folder_doc_ids = set(folder.get("document_ids", []))
        folder_docs = [d for d in documents if d.get("id") in folder_doc_ids]
        summary["total"] = len(folder_docs)

        # Find new/updated documents
        new_docs = self._filter_new_documents(folder_docs)
        summary["new"] = len(new_docs)

        logger.info(
            "sync_check",
            folder=folder_name,
            total=len(folder_docs),
            new=len(new_docs),
            dry_run=dry_run,
        )

        # Process each new document
        for doc in new_docs[: self.config.sync.batch_size]:
            if dry_run:
                summary["documents"].append({
                    "id": doc["id"],
                    "title": doc.get("title", "Untitled"),
                    "action": "would_sync",
                })
                summary["synced"] += 1
            else:
                success = await self._process_document(doc, folder_name)
                summary["documents"].append({
                    "id": doc["id"],
                    "title": doc.get("title", "Untitled"),
                    "action": "synced" if success else "failed",
                })
                if success:
                    summary["synced"] += 1
                else:
                    summary["failed"] += 1

        return summary

    def _find_folder(
        self, all_folders: list[dict[str, Any]], folder_name: str
    ) -> Optional[dict[str, Any]]:
        """Find a folder by name.

        Args:
            all_folders: List of all folders
            folder_name: Name to search for

        Returns:
            Folder dict if found, None otherwise
        """
        for folder in all_folders:
            if folder.get("name") == folder_name:
                return folder
        return None

    def _filter_new_documents(
        self, documents: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Filter documents to only include new or updated ones.

        Args:
            documents: List of documents to filter

        Returns:
            List of documents that need syncing
        """
        new_docs = []
        for doc in documents:
            doc_id = doc.get("id")
            if not doc_id:
                continue

            updated_at = doc.get("updated_at") or doc.get("created_at")

            if not self.state.is_document_seen(doc_id):
                new_docs.append(doc)
            elif self.state.is_document_updated(doc_id, updated_at):
                new_docs.append(doc)

        return new_docs

    async def _process_document(self, doc: dict[str, Any], folder_name: str) -> bool:
        """Process a single document: fetch details and send webhook.

        Args:
            doc: Document to process
            folder_name: Name of the folder

        Returns:
            True if successfully synced, False otherwise
        """
        doc_id = doc["id"]
        logger.info(
            "processing_document",
            doc_id=doc_id,
            title=doc.get("title"),
            folder=folder_name,
        )

        try:
            # Optionally fetch transcript
            transcript = None
            if self.config.granola.include_transcript:
                try:
                    transcript = await self.granola.get_transcript(doc_id)
                except Exception as e:
                    logger.warning(
                        "transcript_fetch_failed",
                        doc_id=doc_id,
                        error=str(e),
                    )

            # Build webhook payload
            payload = self._build_payload(doc, folder_name, transcript)

            # Send webhook
            await self.webhook.send(payload)

            # Update state
            self.state.mark_synced(doc_id, doc, folder_name)
            logger.info("document_synced", doc_id=doc_id, folder=folder_name)

            return True

        except Exception as e:
            self.state.mark_failed(doc_id, str(e), folder_name, doc)
            logger.error(
                "document_failed",
                doc_id=doc_id,
                folder=folder_name,
                error=str(e),
            )
            return False

    def _build_payload(
        self,
        doc: dict[str, Any],
        folder_name: str,
        transcript: Optional[list[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Build webhook payload from Granola document.

        Args:
            doc: The document data
            folder_name: The folder name
            transcript: Optional transcript segments

        Returns:
            Webhook payload dict
        """
        # Extract participants from people array and calendar attendees
        participants = []
        for person in doc.get("people", []):
            name = person.get("display_name") or person.get("name")
            if name:
                participants.append(name)

        # Convert ProseMirror content to text (simplified)
        note_text = self._prosemirror_to_text(
            doc.get("last_viewed_panel", {}).get("content", {})
        )

        # Format transcript
        transcript_text = ""
        if transcript:
            transcript_text = "\n".join(
                f"{'Me' if t.get('source') == 'microphone' else 'Them'}: {t.get('text', '')}"
                for t in transcript
            )

        return {
            "source": "Granola",
            "folder_name": folder_name,
            "note_id": doc["id"],
            "title": doc.get("title", "Untitled"),
            "meeting_started_at": doc.get("created_at"),
            "participants": participants,
            "note_text": note_text,
            "transcript": transcript_text,
            "url": f"https://notes.granola.ai/d/{doc['id']}",
        }

    def _prosemirror_to_text(self, content: dict[str, Any]) -> str:
        """Convert ProseMirror content to plain text.

        This is a simplified converter that extracts text from the content structure.

        Args:
            content: ProseMirror content dict

        Returns:
            Plain text representation
        """
        if not content:
            return ""

        lines = []
        self._extract_text(content, lines)
        return "\n".join(lines)

    def _extract_text(self, node: dict[str, Any], lines: list[str], prefix: str = "") -> None:
        """Recursively extract text from ProseMirror node.

        Args:
            node: ProseMirror node
            lines: List to append lines to
            prefix: Line prefix (for lists, etc.)
        """
        node_type = node.get("type", "")

        if node_type == "text":
            # Text node - return the text content
            text = node.get("text", "")
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += text
            else:
                lines.append(prefix + text)
            return

        if node_type == "heading":
            level = node.get("attrs", {}).get("level", 1)
            prefix = "#" * level + " "

        if node_type == "bulletList":
            for child in node.get("content", []):
                self._extract_text(child, lines, "- ")
            return

        if node_type == "orderedList":
            for i, child in enumerate(node.get("content", []), 1):
                self._extract_text(child, lines, f"{i}. ")
            return

        if node_type == "paragraph" or node_type == "heading":
            # Start a new line for paragraphs
            if lines:
                lines.append("")
            for child in node.get("content", []):
                self._extract_text(child, lines, prefix)
            return

        # For other nodes, just recurse into content
        for child in node.get("content", []):
            self._extract_text(child, lines, prefix)

    def stop(self) -> None:
        """Stop the sync loop."""
        self._running = False
        logger.info("sync_stopped")

    async def close(self) -> None:
        """Close all connections."""
        await self.granola.close()
        await self.webhook.close()

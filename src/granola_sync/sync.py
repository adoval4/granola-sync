"""Main sync loop logic."""

import asyncio
from typing import Any, Optional

import structlog

from .config import Config
from .granola_api import GranolaCacheReader, GranolaClient
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

        Uses the Granola API as the primary data source for real-time freshness,
        with the local cache as a fallback when the API is unavailable.

        Args:
            dry_run: If True, don't send webhooks, just report what would be synced

        Returns:
            Summary of the sync cycle
        """
        configured_folders = self.config.granola.folders
        logger.info("sync_cycle_started", folders=configured_folders, dry_run=dry_run)

        summary: dict[str, Any] = {
            "folders_checked": len(configured_folders),
            "documents_found": 0,
            "documents_new": 0,
            "documents_synced": 0,
            "documents_failed": 0,
            "documents_pending": 0,
            "by_folder": {},
        }

        try:
            # 1. Resolve folder name → ID mapping
            folder_map = self._resolve_folder_map()

            # 2. Process each configured folder
            for folder_name in configured_folders:
                folder_id = folder_map.get(folder_name)
                if not folder_id:
                    logger.warning("folder_id_not_resolved", name=folder_name)
                    summary["by_folder"][folder_name] = {
                        "total": 0,
                        "new": 0,
                        "synced": 0,
                        "failed": 0,
                        "pending": 0,
                        "documents": [],
                    }
                    continue

                # Fetch documents from API with cache fallback
                documents = await self._fetch_folder_documents(folder_name, folder_id)
                summary["documents_found"] += len(documents)

                # Sync documents in this folder
                folder_summary = await self._sync_documents(
                    folder_name, documents, dry_run=dry_run
                )
                summary["by_folder"][folder_name] = folder_summary
                summary["documents_new"] += folder_summary["new"]
                summary["documents_synced"] += folder_summary["synced"]
                summary["documents_failed"] += folder_summary["failed"]
                summary["documents_pending"] += folder_summary.get("pending", 0)

            # 3. Re-check documents that were previously pending content
            pending_summary = await self._recheck_pending_documents(dry_run=dry_run)
            summary["documents_synced"] += pending_summary["synced"]
            summary["documents_failed"] += pending_summary["failed"]

            # 4. Save state (unless dry run)
            if not dry_run:
                self.state.save()

            logger.info(
                "sync_cycle_completed",
                folders_checked=summary["folders_checked"],
                documents_found=summary["documents_found"],
                new=summary["documents_new"],
                synced=summary["documents_synced"],
                failed=summary["documents_failed"],
                pending=summary["documents_pending"],
            )

        except Exception as e:
            logger.error("sync_cycle_error", error=str(e))
            raise

        return summary

    def _resolve_folder_map(self) -> dict[str, str]:
        """Resolve folder names to IDs from multiple sources.

        Priority order:
        1. Explicit IDs from config.yaml (folder_ids)
        2. Persisted mapping from state.json (folder_map)
        3. Live read from cache-v4.json (updates state)

        Returns:
            Dict mapping folder titles to their IDs
        """
        folder_map: dict[str, str] = {}

        # Priority 3 (lowest): Read from cache file
        try:
            cache = GranolaCacheReader()
            cache_map = cache.get_folder_map()
            folder_map.update(cache_map)
            # Persist for future use even if cache becomes unavailable
            self.state.update_folder_map(cache_map)
            logger.debug("folder_map_from_cache", count=len(cache_map))
        except Exception as e:
            logger.debug("folder_map_cache_unavailable", error=str(e))

        # Priority 2: Persisted mapping from state (overrides cache if present)
        state_map = self.state.get_folder_map()
        folder_map.update(state_map)

        # Priority 1 (highest): Explicit config overrides
        if self.config.granola.folder_ids:
            folder_map.update(self.config.granola.folder_ids)
            logger.debug("folder_map_from_config", count=len(self.config.granola.folder_ids))

        return folder_map

    async def _fetch_folder_documents(
        self, folder_name: str, folder_id: str
    ) -> list[dict[str, Any]]:
        """Fetch documents for a folder, API-first with cache fallback.

        Args:
            folder_name: The folder title (for cache fallback)
            folder_id: The folder/list ID (for API call)

        Returns:
            List of document objects
        """
        try:
            documents = await self.granola.get_documents_by_folder(folder_id)
            logger.debug("documents_from_api", folder=folder_name, count=len(documents))
            return documents
        except Exception as api_error:
            logger.warning(
                "api_fetch_failed_using_cache",
                folder=folder_name,
                error=str(api_error),
            )
            try:
                cache = GranolaCacheReader()
                documents = cache.get_documents_for_folder(folder_name)
                logger.debug("documents_from_cache", folder=folder_name, count=len(documents))
                return documents
            except Exception as cache_error:
                logger.error(
                    "both_sources_failed",
                    folder=folder_name,
                    api_error=str(api_error),
                    cache_error=str(cache_error),
                )
                return []

    async def _sync_documents(
        self,
        folder_label: str,
        documents: list[dict[str, Any]],
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Sync documents.

        Args:
            folder_label: Label to use in webhook payload
            documents: List of all documents from the API
            dry_run: If True, don't send webhooks

        Returns:
            Summary of documents processed
        """
        summary: dict[str, Any] = {
            "total": len(documents),
            "new": 0,
            "synced": 0,
            "failed": 0,
            "pending": 0,
            "documents": [],
        }

        # Find new/updated documents
        new_docs = self._filter_new_documents(documents)
        summary["new"] = len(new_docs)

        logger.info(
            "sync_check",
            folder_label=folder_label,
            total=len(documents),
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
            elif not self._has_content(doc):
                # No content yet — mark as pending for re-check later
                self.state.mark_pending(doc["id"], doc, folder_label)
                summary["documents"].append({
                    "id": doc["id"],
                    "title": doc.get("title", "Untitled"),
                    "action": "pending_content",
                })
                summary["pending"] += 1
                logger.info(
                    "document_pending_content",
                    doc_id=doc["id"],
                    title=doc.get("title"),
                    folder=folder_label,
                )
            else:
                success = await self._process_document(doc, folder_label)
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

    def _has_content(self, doc: dict[str, Any]) -> bool:
        """Check if a document has meaningful note content.

        Args:
            doc: The document data

        Returns:
            True if the document has notes content to sync
        """
        if doc.get("notes_markdown"):
            return True
        if doc.get("notes_plain"):
            return True
        # Check ProseMirror notes dict for actual text
        notes = doc.get("notes")
        if isinstance(notes, dict) and self._prosemirror_has_text(notes):
            return True
        # Check last_viewed_panel content (cache format)
        last_viewed_panel = doc.get("last_viewed_panel") or {}
        content = last_viewed_panel.get("content") if isinstance(last_viewed_panel, dict) else None
        if isinstance(content, str) and content.strip():
            return True
        if isinstance(content, dict) and self._prosemirror_has_text(content):
            return True
        return False

    def _prosemirror_has_text(self, node: dict[str, Any]) -> bool:
        """Check if a ProseMirror node tree contains any actual text.

        Args:
            node: ProseMirror node dict

        Returns:
            True if the node tree contains text content
        """
        if not node:
            return False
        if node.get("type") == "text" and node.get("text", "").strip():
            return True
        for child in node.get("content", []):
            if isinstance(child, dict) and self._prosemirror_has_text(child):
                return True
        return False

    async def _recheck_pending_documents(
        self, dry_run: bool = False
    ) -> dict[str, Any]:
        """Re-check documents previously skipped due to missing content.

        Fetches each pending document from the API and processes it
        if content has appeared since it was first seen.

        Args:
            dry_run: If True, don't send webhooks

        Returns:
            Summary with synced/failed counts
        """
        result: dict[str, Any] = {"synced": 0, "failed": 0}
        pending = self.state.get_pending_documents()

        if not pending:
            return result

        logger.info("rechecking_pending_documents", count=len(pending))

        for doc_info in pending:
            doc_id = doc_info["doc_id"]
            folder_name = doc_info["folder_name"]

            try:
                # Fetch the latest version from the API
                docs = await self.granola.get_documents(limit=100, offset=0)
                doc = next((d for d in docs if d.get("id") == doc_id), None)

                if doc and self._has_content(doc):
                    if dry_run:
                        result["synced"] += 1
                    else:
                        success = await self._process_document(doc, folder_name)
                        if success:
                            self.state.clear_pending(doc_id)
                            result["synced"] += 1
                            logger.info(
                                "pending_document_synced",
                                doc_id=doc_id,
                                title=doc.get("title"),
                            )
                        else:
                            result["failed"] += 1
                else:
                    # Still no content — update check timestamp
                    if not dry_run:
                        self.state.mark_pending(
                            doc_id,
                            {"title": doc_info.get("title", "Untitled")},
                            folder_name,
                        )
                    logger.debug("pending_document_still_empty", doc_id=doc_id)

            except Exception as e:
                logger.warning(
                    "pending_recheck_error", doc_id=doc_id, error=str(e)
                )

        return result

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
        # Extract participants from people.attendees (new API structure)
        # or from attendees array (fallback)
        participants = []
        people = doc.get("people", {})
        if isinstance(people, dict):
            # New API structure: people.attendees is an array of {name, email}
            for attendee in people.get("attendees", []):
                name = attendee.get("name") or attendee.get("email")
                if name:
                    participants.append(name)
        elif isinstance(people, list):
            # Legacy structure: people is an array
            for person in people:
                name = person.get("display_name") or person.get("name")
                if name:
                    participants.append(name)

        # Also check top-level attendees array
        for attendee in doc.get("attendees", []):
            if isinstance(attendee, str) and attendee not in participants:
                participants.append(attendee)

        # Extract note text: prefer pre-rendered markdown from cache,
        # then ProseMirror content, then plain text fallback
        last_viewed_panel = doc.get("last_viewed_panel") or {}
        content = last_viewed_panel.get("content") if isinstance(last_viewed_panel, dict) else None

        if doc.get("notes_markdown"):
            note_text = doc["notes_markdown"]
        elif isinstance(content, str):
            note_text = content
        elif isinstance(content, dict):
            note_text = self._prosemirror_to_text(content)
        else:
            note_text = doc.get("notes_plain", "")

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

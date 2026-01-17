"""Granola API client."""

import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

GRANOLA_API_BASE = "https://api.granola.ai/v0"


def get_granola_token() -> str:
    """Get the Granola authentication token from the local storage.

    The token is stored by the Granola desktop app in a JSON file.

    Returns:
        The authentication token

    Raises:
        FileNotFoundError: If the token file doesn't exist
        ValueError: If the token cannot be found in the file
    """
    # Granola stores auth data in ~/Library/Application Support/Granola/auth.json on macOS
    # or ~/.config/Granola/auth.json on Linux
    if os.name == "nt":
        # Windows
        app_data = os.environ.get("APPDATA", "")
        token_path = Path(app_data) / "Granola" / "auth.json"
    elif os.uname().sysname == "Darwin":
        # macOS
        token_path = Path.home() / "Library" / "Application Support" / "Granola" / "auth.json"
    else:
        # Linux
        token_path = Path.home() / ".config" / "Granola" / "auth.json"

    if not token_path.exists():
        raise FileNotFoundError(
            f"Granola token not found at {token_path}. "
            "Make sure the Granola app is installed and you are logged in."
        )

    with open(token_path) as f:
        auth_data = json.load(f)

    token = auth_data.get("access_token") or auth_data.get("token")
    if not token:
        raise ValueError("Could not find access_token in Granola auth file")

    return token


class GranolaClient:
    """Client for interacting with the Granola API."""

    def __init__(self, token: Optional[str] = None, base_url: str = GRANOLA_API_BASE):
        """Initialize the Granola API client.

        Args:
            token: Authentication token. If not provided, will be loaded from local storage.
            base_url: Base URL for the API.
        """
        self._token = token
        self.base_url = base_url
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def token(self) -> str:
        """Get the authentication token, loading it if necessary."""
        if self._token is None:
            self._token = get_granola_token()
        return self._token

    async def _get_client(self) -> httpx.AsyncClient:
        """Get or create the HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )
        return self._client

    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def get_folders(self) -> list[dict[str, Any]]:
        """Get all folders from Granola.

        Returns:
            List of folder objects with id, name, and document_ids
        """
        client = await self._get_client()
        logger.debug("fetching_folders")

        response = await client.get("/folders")
        response.raise_for_status()

        data = response.json()
        folders = data.get("folders", data) if isinstance(data, dict) else data
        logger.debug("folders_fetched", count=len(folders))
        return folders

    async def get_documents(self, limit: int = 100) -> list[dict[str, Any]]:
        """Get recent documents from Granola.

        Args:
            limit: Maximum number of documents to fetch

        Returns:
            List of document objects
        """
        client = await self._get_client()
        logger.debug("fetching_documents", limit=limit)

        response = await client.get("/documents", params={"limit": limit})
        response.raise_for_status()

        data = response.json()
        documents = data.get("documents", data) if isinstance(data, dict) else data
        logger.debug("documents_fetched", count=len(documents))
        return documents

    async def get_document(self, doc_id: str) -> dict[str, Any]:
        """Get a single document by ID.

        Args:
            doc_id: The document ID

        Returns:
            Document object with full details
        """
        client = await self._get_client()
        logger.debug("fetching_document", doc_id=doc_id)

        response = await client.get(f"/documents/{doc_id}")
        response.raise_for_status()

        return response.json()

    async def get_transcript(self, doc_id: str) -> list[dict[str, Any]]:
        """Get the transcript for a document.

        Args:
            doc_id: The document ID

        Returns:
            List of transcript segments with source (microphone/speaker) and text
        """
        client = await self._get_client()
        logger.debug("fetching_transcript", doc_id=doc_id)

        response = await client.get(f"/documents/{doc_id}/transcript")
        response.raise_for_status()

        data = response.json()
        transcript = data.get("transcript", data) if isinstance(data, dict) else data
        logger.debug("transcript_fetched", doc_id=doc_id, segments=len(transcript))
        return transcript

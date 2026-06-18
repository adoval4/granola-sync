"""Granola API client."""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import structlog

logger = structlog.get_logger()

GRANOLA_API_BASE = "https://api.granola.ai"
TOKEN_REFRESH_BUFFER_SECONDS = 300  # Refresh 5 minutes before expiration
REQUIRED_CACHE_KEYS = {"documents", "documentListsMetadata"}
_CACHE_VERSION_RE = re.compile(r"^cache-v(\d+)\.json$")


def _get_granola_app_dir() -> Path:
    """Get the platform-specific Granola application data directory."""
    if os.name == "nt":
        app_data = os.environ.get("APPDATA", "")
        return Path(app_data) / "Granola"
    elif os.uname().sysname == "Darwin":
        return Path.home() / "Library" / "Application Support" / "Granola"
    else:
        return Path.home() / ".config" / "Granola"


def get_token_file_path() -> Path:
    """Get the path to the Granola credentials file.

    Returns:
        Path to the supabase.json file
    """
    return _get_granola_app_dir() / "supabase.json"


def is_token_expired(workos_tokens: dict[str, Any]) -> bool:
    """Check if the access token has expired or will expire soon.

    Args:
        workos_tokens: The parsed workos_tokens object

    Returns:
        True if the token has expired or will expire within the buffer time
    """
    current_time = time.time() * 1000  # Convert to milliseconds
    token_obtained_at = workos_tokens.get("obtained_at", 0)
    expires_in_ms = workos_tokens.get("expires_in", 0) * 1000
    expiration_time = token_obtained_at + expires_in_ms
    buffer_time = TOKEN_REFRESH_BUFFER_SECONDS * 1000

    return current_time >= (expiration_time - buffer_time)


def refresh_access_token(workos_tokens: dict[str, Any]) -> dict[str, Any]:
    """Refresh the access token using the refresh token.

    Args:
        workos_tokens: The current workos_tokens object

    Returns:
        Updated workos_tokens with new access token

    Raises:
        httpx.HTTPStatusError: If the refresh request fails
    """
    logger.debug("refreshing_access_token")

    with httpx.Client(timeout=30.0) as client:
        response = client.post(
            "https://api.granola.ai/v1/refresh-access-token",
            headers={
                "Authorization": f"Bearer {workos_tokens['access_token']}",
                "Content-Type": "application/json",
            },
            json={
                "refresh_token": workos_tokens["refresh_token"],
                "provider": "workos",
            },
        )
        response.raise_for_status()
        refresh_response = response.json()

    updated_tokens = {
        **workos_tokens,
        "access_token": refresh_response["access_token"],
        "expires_in": refresh_response["expires_in"],
        "token_type": refresh_response["token_type"],
        "obtained_at": int(time.time() * 1000),
        "refresh_token": refresh_response.get("refresh_token", workos_tokens["refresh_token"]),
    }

    logger.debug("access_token_refreshed")
    return updated_tokens


def get_granola_token() -> str:
    """Get the Granola authentication token from the local storage.

    The token is stored by the Granola desktop app in supabase.json.
    If the token has expired, it will be refreshed automatically.

    Returns:
        The authentication token

    Raises:
        FileNotFoundError: If the token file doesn't exist
        ValueError: If the token cannot be found in the file
    """
    token_path = get_token_file_path()

    if not token_path.exists():
        raise FileNotFoundError(
            f"Granola credentials not found at {token_path}. "
            "Make sure the Granola app is installed and you are logged in."
        )

    with open(token_path) as f:
        token_data = json.load(f)

    workos_tokens_str = token_data.get("workos_tokens")
    if not workos_tokens_str:
        raise ValueError("Could not find workos_tokens in Granola credentials file")

    workos_tokens = json.loads(workos_tokens_str)

    token = workos_tokens.get("access_token")
    if not token:
        raise ValueError("Could not find access_token in Granola credentials")

    if is_token_expired(workos_tokens):
        logger.debug("token_expired_refreshing")
        try:
            workos_tokens = refresh_access_token(workos_tokens)
            token = workos_tokens["access_token"]
        except Exception as e:
            raise ValueError(
                f"Access token has expired and refresh failed: {e}. "
                "Please re-authenticate in the Granola app."
            ) from e

    return token


class GranolaCacheReader:
    """Reads folder and document data from the local Granola app cache."""

    def get_cache_paths(self) -> list[Path]:
        """Auto-discover cache-v*.json files, newest version first."""
        app_dir = _get_granola_app_dir()
        candidates: list[tuple[int, Path]] = []
        if app_dir.is_dir():
            for p in app_dir.iterdir():
                m = _CACHE_VERSION_RE.match(p.name)
                if m:
                    candidates.append((int(m.group(1)), p))
        candidates.sort(reverse=True)
        return [p for _, p in candidates]

    @staticmethod
    def _parse_cache_state(raw: dict) -> dict:
        """Extract the state dict from a raw cache file's JSON."""
        cache = raw.get("cache", raw)
        if isinstance(cache, str):
            cache = json.loads(cache)
        state = cache.get("state", cache)
        if isinstance(state, str):
            state = json.loads(state)
        return state

    def read_cache(self) -> dict:
        paths = self.get_cache_paths()
        if not paths:
            app_dir = _get_granola_app_dir()
            raise FileNotFoundError(
                f"No Granola cache files (cache-v*.json) found in {app_dir}"
            )

        for cache_path in paths:
            try:
                logger.debug("reading_cache", path=str(cache_path))
                with open(cache_path) as f:
                    raw = json.load(f)
                state = self._parse_cache_state(raw)
                if REQUIRED_CACHE_KEYS.issubset(state):
                    return state
                logger.warning(
                    "cache_structure_invalid",
                    path=str(cache_path),
                    missing_keys=list(REQUIRED_CACHE_KEYS - state.keys()),
                )
            except Exception as e:
                logger.warning("cache_read_failed", path=str(cache_path), error=str(e))

        raise FileNotFoundError(
            f"No valid Granola cache found. Tried: {', '.join(str(p) for p in paths)}"
        )

    def get_folders(self) -> list[dict[str, Any]]:
        state = self.read_cache()

        metadata = state.get("documentListsMetadata", {})
        doc_lists = state.get("documentLists", {})
        documents = state.get("documents", {})

        folders: list[dict[str, Any]] = []
        for list_id, meta in metadata.items():
            doc_ids = doc_lists.get(list_id, [])
            folder_docs = [documents[did] for did in doc_ids if did in documents]
            folders.append({
                "id": meta.get("id", list_id),
                "title": meta.get("title", ""),
                "documents": folder_docs,
            })

        return folders

    def get_document(self, doc_id: str) -> dict[str, Any] | None:
        state = self.read_cache()
        return state.get("documents", {}).get(doc_id)

    def get_documents_for_folder(self, folder_title: str) -> list[dict[str, Any]]:
        for folder in self.get_folders():
            if folder["title"] == folder_title:
                return folder["documents"]
        return []

    def get_folder_map(self) -> dict[str, str]:
        """Return a mapping of folder title to folder ID from the cache.

        Returns:
            Dict mapping folder titles to their IDs
        """
        state = self.read_cache()
        metadata = state.get("documentListsMetadata", {})
        return {
            meta.get("title", ""): list_id
            for list_id, meta in metadata.items()
            if meta.get("title")
        }


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
        """Get all folders (document lists) from Granola.

        Tries the local app cache first, falls back to the API.

        Returns:
            List of folder objects with id, title, and documents
        """
        # Try cache first
        cache_error = None
        try:
            cache = GranolaCacheReader()
            folders = cache.get_folders()
            if folders:
                logger.debug("folders_from_cache", count=len(folders))
                return folders
        except Exception as e:
            cache_error = e
            logger.warning("cache_read_failed_falling_back_to_api", error=str(e))

        # Fallback to API
        try:
            logger.debug("fetching_folders")
            lists = await self._fetch_document_lists_metadata()
            folders = [
                {
                    "id": meta.get("id", list_id),
                    "title": meta.get("title", ""),
                    "documents": [],
                }
                for list_id, meta in lists.items()
                if isinstance(meta, dict)
            ]
            logger.debug("folders_fetched", count=len(folders))
            return folders
        except Exception as api_error:
            raise RuntimeError(
                f"Failed to load folders. "
                f"Cache error: {cache_error}; API error: {api_error}"
            ) from api_error

    async def _fetch_document_lists_metadata(self) -> dict[str, Any]:
        """Fetch folder (document list) metadata keyed by list ID from the API.

        Uses POST /v1/get-document-lists-metadata. (GET /v2/get-document-lists,
        the previous endpoint, returns HTTP 500.) Shared by get_folders() and
        get_folder_map().

        Returns:
            Mapping of list ID → metadata dict
        """
        client = await self._get_client()
        response = await client.post("/v1/get-document-lists-metadata", json={})
        response.raise_for_status()

        data = response.json()
        lists = data.get("lists", {}) if isinstance(data, dict) else {}
        return lists if isinstance(lists, dict) else {}

    async def get_folder_map(self) -> dict[str, str]:
        """Get a mapping of folder title → ID from the Granola API.

        API equivalent of GranolaCacheReader.get_folder_map(). This is the
        reliable way to resolve folder names when the local cache is
        unavailable — newer Granola versions encrypt cache-v6.json, leaving it
        without a usable ``documents`` key.

        Returns:
            Dict mapping folder titles to their IDs
        """
        logger.debug("fetching_folder_map")
        lists = await self._fetch_document_lists_metadata()
        folder_map = {
            meta.get("title", ""): meta.get("id", list_id)
            for list_id, meta in lists.items()
            if isinstance(meta, dict) and meta.get("title")
        }
        logger.debug("folder_map_fetched", count=len(folder_map))
        return folder_map

    async def get_documents_by_folder(
        self, list_id: str, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        """Get documents for a specific folder via the API.

        Args:
            list_id: The folder/list ID to filter by
            limit: Maximum number of documents to fetch
            offset: Number of documents to skip (for pagination)

        Returns:
            List of document objects in the folder
        """
        client = await self._get_client()
        logger.debug("fetching_documents_by_folder", list_id=list_id, limit=limit, offset=offset)

        response = await client.post(
            "/v2/get-documents",
            json={
                "list_id": list_id,
                "limit": limit,
                "offset": offset,
            },
        )
        response.raise_for_status()

        data = response.json()
        documents = data.get("docs", []) if isinstance(data, dict) else data
        logger.debug("folder_documents_fetched", list_id=list_id, count=len(documents))
        return documents

    async def get_documents(
        self, limit: int = 100, offset: int = 0, include_last_viewed_panel: bool = True
    ) -> list[dict[str, Any]]:
        """Get recent documents from Granola.

        Args:
            limit: Maximum number of documents to fetch per page
            offset: Number of documents to skip (for pagination)
            include_last_viewed_panel: Whether to include document content

        Returns:
            List of document objects
        """
        client = await self._get_client()
        logger.debug("fetching_documents", limit=limit, offset=offset)

        response = await client.post(
            "/v2/get-documents",
            json={
                "limit": limit,
                "offset": offset,
                "include_last_viewed_panel": include_last_viewed_panel,
            },
        )
        response.raise_for_status()

        data = response.json()
        documents = data.get("docs", data) if isinstance(data, dict) else data
        logger.debug("documents_fetched", count=len(documents))
        return documents

    async def get_all_documents(
        self, page_size: int = 100, include_last_viewed_panel: bool = True
    ) -> list[dict[str, Any]]:
        """Get all documents from Granola with pagination.

        Args:
            page_size: Number of documents to fetch per page
            include_last_viewed_panel: Whether to include document content

        Returns:
            List of all document objects
        """
        documents: list[dict[str, Any]] = []
        offset = 0

        while True:
            page = await self.get_documents(
                limit=page_size,
                offset=offset,
                include_last_viewed_panel=include_last_viewed_panel,
            )
            if not page:
                break

            documents.extend(page)

            if len(page) < page_size:
                break

            offset += page_size

        return documents

    async def get_transcript(self, doc_id: str) -> list[dict[str, Any]]:
        """Get the transcript for a document.

        Args:
            doc_id: The document ID

        Returns:
            List of transcript entries with speaker, text, and timestamps
        """
        client = await self._get_client()
        logger.debug("fetching_transcript", doc_id=doc_id)

        response = await client.post(
            "/v1/get-document-transcript",
            json={"document_id": doc_id},
        )
        response.raise_for_status()

        data = response.json()
        # The API returns the transcript array directly
        transcript = data if isinstance(data, list) else data.get("transcript", [])
        logger.debug("transcript_fetched", doc_id=doc_id, segments=len(transcript))
        return transcript

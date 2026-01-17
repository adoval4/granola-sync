"""Tests for Granola API client."""

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
import respx

from granola_sync.granola_api import GranolaClient, get_granola_token


class TestGetGranolaToken:
    """Tests for get_granola_token function."""

    def test_get_token_from_file(self, tmp_path: Path):
        """Test loading token from auth file."""
        auth_file = tmp_path / "auth.json"
        auth_file.write_text(json.dumps({"access_token": "test-token-123"}))

        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            # Simulate macOS path
            token_dir = tmp_path / "Library" / "Application Support" / "Granola"
            token_dir.mkdir(parents=True)
            token_file = token_dir / "auth.json"
            token_file.write_text(json.dumps({"access_token": "test-token-123"}))

            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                token = get_granola_token()

            assert token == "test-token-123"

    def test_get_token_alternative_key(self, tmp_path: Path):
        """Test loading token from auth file with 'token' key."""
        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            token_dir = tmp_path / "Library" / "Application Support" / "Granola"
            token_dir.mkdir(parents=True)
            token_file = token_dir / "auth.json"
            token_file.write_text(json.dumps({"token": "alt-token-456"}))

            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                token = get_granola_token()

            assert token == "alt-token-456"

    def test_get_token_file_not_found(self, tmp_path: Path):
        """Test error when token file doesn't exist."""
        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                with pytest.raises(FileNotFoundError, match="Granola token not found"):
                    get_granola_token()

    def test_get_token_missing_key(self, tmp_path: Path):
        """Test error when token key is missing from file."""
        with patch("granola_sync.granola_api.Path.home", return_value=tmp_path):
            token_dir = tmp_path / "Library" / "Application Support" / "Granola"
            token_dir.mkdir(parents=True)
            token_file = token_dir / "auth.json"
            token_file.write_text(json.dumps({"other_key": "value"}))

            with patch("os.uname") as mock_uname:
                mock_uname.return_value.sysname = "Darwin"
                with pytest.raises(ValueError, match="Could not find access_token"):
                    get_granola_token()


class TestGranolaClient:
    """Tests for GranolaClient class."""

    @pytest.fixture
    def client(self):
        """Create a client with a test token."""
        return GranolaClient(token="test-token")

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_folders(self, client):
        """Test fetching folders."""
        mock_folders = [
            {"id": "folder1", "name": "SQP", "document_ids": ["doc1", "doc2"]},
            {"id": "folder2", "name": "CLIENT-A", "document_ids": ["doc3"]},
        ]

        respx.get("https://api.granola.ai/v0/folders").mock(
            return_value=httpx.Response(200, json={"folders": mock_folders})
        )

        folders = await client.get_folders()

        assert len(folders) == 2
        assert folders[0]["name"] == "SQP"
        assert folders[1]["name"] == "CLIENT-A"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_documents(self, client):
        """Test fetching documents."""
        mock_documents = [
            {"id": "doc1", "title": "Meeting 1", "created_at": "2026-01-17T10:00:00Z"},
            {"id": "doc2", "title": "Meeting 2", "created_at": "2026-01-17T11:00:00Z"},
        ]

        respx.get("https://api.granola.ai/v0/documents").mock(
            return_value=httpx.Response(200, json={"documents": mock_documents})
        )

        documents = await client.get_documents(limit=100)

        assert len(documents) == 2
        assert documents[0]["title"] == "Meeting 1"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_document(self, client):
        """Test fetching a single document."""
        mock_document = {
            "id": "doc1",
            "title": "Sprint Planning",
            "created_at": "2026-01-17T10:00:00Z",
            "people": [{"display_name": "John Doe"}],
            "last_viewed_panel": {"content": {}},
        }

        respx.get("https://api.granola.ai/v0/documents/doc1").mock(
            return_value=httpx.Response(200, json=mock_document)
        )

        document = await client.get_document("doc1")

        assert document["id"] == "doc1"
        assert document["title"] == "Sprint Planning"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_get_transcript(self, client):
        """Test fetching a document transcript."""
        mock_transcript = [
            {"source": "microphone", "text": "Hello everyone"},
            {"source": "speaker", "text": "Hi there"},
        ]

        respx.get("https://api.granola.ai/v0/documents/doc1/transcript").mock(
            return_value=httpx.Response(200, json={"transcript": mock_transcript})
        )

        transcript = await client.get_transcript("doc1")

        assert len(transcript) == 2
        assert transcript[0]["text"] == "Hello everyone"

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_api_error_handling(self, client):
        """Test handling of API errors."""
        respx.get("https://api.granola.ai/v0/folders").mock(
            return_value=httpx.Response(401, json={"error": "Unauthorized"})
        )

        with pytest.raises(httpx.HTTPStatusError):
            await client.get_folders()

        await client.close()

    @respx.mock
    @pytest.mark.asyncio
    async def test_client_reuses_connection(self, client):
        """Test that the client reuses the HTTP connection."""
        respx.get("https://api.granola.ai/v0/folders").mock(
            return_value=httpx.Response(200, json={"folders": []})
        )

        # Make two requests
        await client.get_folders()
        await client.get_folders()

        # Should have reused the same client
        assert client._client is not None
        assert not client._client.is_closed

        await client.close()
        assert client._client.is_closed

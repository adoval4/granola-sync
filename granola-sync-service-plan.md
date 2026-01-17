# Granola Sync Service - Implementation Plan

## Overview

A Python CLI tool that polls Granola's API for new meeting notes in specified folders and sends them to the backend webhook endpoint, replacing the current Zapier-based integration. Supports watching multiple folders simultaneously.

## Decisions Summary

| Decision | Choice |
|----------|--------|
| Language | Python |
| Detection Method | Polling (configurable interval) |
| Distribution | pip/uv package (PyPI) |
| Background Execution | Foreground process + launchd/systemd |
| Configuration | CLI flags + config file |
| Monitoring | Structured logging + status command |
| State Storage | JSON file |
| Authentication | HMAC-SHA256 signature |

---

## Architecture

```
┌──────────────────────┐     Poll every N min      ┌─────────────────────┐
│   User's Machine     │ ◄──────────────────────►  │   Granola API       │
│                      │                           │   api.granola.ai    │
│  ┌────────────────┐  │                           └─────────────────────┘
│  │ granola-sync   │  │
│  │ (Python CLI)   │  │
│  └───────┬────────┘  │
│          │           │
│  ┌───────▼────────┐  │     POST /webhooks/granola/   ┌─────────────────────┐
│  │ state.json     │  │ ─────────────────────────────► │   Backend API       │
│  │ (seen docs)    │  │   (HMAC-SHA256 signed)        │   your-server:8001  │
│  └────────────────┘  │                                └─────────────────────┘
│                      │
│  ┌────────────────┐  │
│  │ config.yaml    │  │
│  │ (settings)     │  │
│  └────────────────┘  │
└──────────────────────┘
```

---

## Package Structure

```
granola-sync/
├── pyproject.toml          # Package metadata, dependencies
├── README.md               # User documentation
├── src/
│   └── granola_sync/
│       ├── __init__.py
│       ├── __main__.py     # Entry point for `python -m granola_sync`
│       ├── cli.py          # Click/Typer CLI commands
│       ├── config.py       # Configuration loading/validation
│       ├── granola_api.py  # Granola API client
│       ├── webhook.py      # Webhook sender with HMAC signing
│       ├── state.py        # JSON state management
│       ├── sync.py         # Main sync loop logic
│       └── logging.py      # Structured logging setup
├── templates/
│   ├── launchd.plist       # macOS launchd template
│   └── systemd.service     # Linux systemd template
└── tests/
    ├── test_granola_api.py
    ├── test_webhook.py
    ├── test_sync.py
    └── test_config.py
```

---

## CLI Commands

### Main Commands (MVP)

```bash
# Configure the service (interactive)
granola-sync config
# Interactive prompts:
#   - Webhook URL: https://...
#   - Folders to watch (comma-separated): SQP, CLIENT-A, Internal
#   - Poll interval (seconds): 300
#   - Webhook secret: (auto-generate or enter)
# Creates ~/.granola-sync/config.yaml

# Run the sync service (foreground) - useful for testing
granola-sync run

# Run with CLI overrides
granola-sync run --folder "SQP" --folder "CLIENT-A" --webhook-url "https://..."

# Sync once and exit (useful for testing/debugging)
granola-sync sync-once
granola-sync sync-once --dry-run  # Don't send webhooks, just show what would sync

# Start as background service (installs and starts launchd/systemd)
granola-sync start
# Output:
#   ✓ Created launchd plist at ~/Library/LaunchAgents/com.turbo.granola-sync.plist
#   ✓ Service started
#   Run 'granola-sync stop' to stop the service

# Stop the background service
granola-sync stop
# Output:
#   ✓ Service stopped
#   ✓ Removed from launchd
```

### CLI Flags

```bash
# Global options (apply to all commands)
granola-sync [OPTIONS] COMMAND

Global Options:
  --config, -c PATH       Path to config file [default: ~/.granola-sync/config.yaml]
  --verbose, -v           Increase log verbosity

# run command
granola-sync run [OPTIONS]

Options:
  --folder, -f TEXT       Granola folder name to watch (repeatable, overrides config)
  --webhook-url, -w URL   Webhook endpoint URL (overrides config)
  --webhook-secret TEXT   HMAC signing secret (overrides config)
  --interval, -i INT      Poll interval in seconds [default: 300]
  --include-transcript    Also fetch and send transcript for each document

# sync-once command
granola-sync sync-once [OPTIONS]

Options:
  --dry-run               Poll and detect changes but don't send webhooks
  --folder, -f TEXT       Override folders from config (repeatable)

# config command
granola-sync config [OPTIONS]

Options:
  --generate-secret       Auto-generate a secure webhook secret

# start/stop commands
granola-sync start        # Install and start as background service
granola-sync stop         # Stop and uninstall background service
```

---

## Configuration File

Location: `~/.granola-sync/config.yaml`

```yaml
# Granola Sync Configuration

# Webhook settings
webhook:
  url: "https://your-server.com/webhooks/granola/"
  secret: "your-hmac-secret-here"  # Generate with: granola-sync init --generate-secret

# Granola settings
granola:
  folders:                         # List of folder names to watch
    - "SQP"
    - "CLIENT-A"
    - "Internal"
  include_transcript: true         # Fetch full transcript for each document

# Sync settings
sync:
  interval: 300                    # Poll interval in seconds (5 minutes)
  batch_size: 10                   # Max documents to process per cycle
  retry_attempts: 3                # Retry failed webhooks
  retry_delay: 30                  # Seconds between retries

# Logging settings
logging:
  level: "INFO"                    # DEBUG, INFO, WARNING, ERROR
  file: "~/.granola-sync/granola-sync.log"
  max_size_mb: 10                  # Rotate when file exceeds this size
  backup_count: 3                  # Keep this many rotated logs

# State settings
state:
  file: "~/.granola-sync/state.json"
```

---

## State File

Location: `~/.granola-sync/state.json`

```json
{
  "version": 1,
  "last_sync": "2026-01-17T10:30:00Z",
  "folders": {
    "SQP": {
      "folder_id": "abc123",
      "last_sync": "2026-01-17T10:30:00Z"
    },
    "CLIENT-A": {
      "folder_id": "def456",
      "last_sync": "2026-01-17T10:28:00Z"
    }
  },
  "seen_documents": {
    "doc_id_1": {
      "title": "Sprint Planning",
      "folder_name": "SQP",
      "first_seen": "2026-01-15T09:00:00Z",
      "last_updated": "2026-01-15T10:30:00Z",
      "synced_at": "2026-01-15T10:35:00Z",
      "webhook_status": "success"
    },
    "doc_id_2": {
      "title": "Client Sync",
      "folder_name": "CLIENT-A",
      "first_seen": "2026-01-16T14:00:00Z",
      "last_updated": "2026-01-16T15:00:00Z",
      "synced_at": "2026-01-16T15:05:00Z",
      "webhook_status": "success"
    }
  },
  "failed_documents": {
    "doc_id_3": {
      "title": "Failed Meeting",
      "folder_name": "SQP",
      "attempts": 2,
      "last_error": "Connection timeout",
      "last_attempt": "2026-01-17T08:00:00Z"
    }
  },
  "stats": {
    "total_synced": 42,
    "total_errors": 1,
    "last_error": "2026-01-17T08:00:00Z",
    "by_folder": {
      "SQP": { "synced": 25, "errors": 1 },
      "CLIENT-A": { "synced": 17, "errors": 0 }
    }
  }
}
```

---

## Webhook Payload

The service sends this payload to match the existing `GranolaWebhookView` expected format:

```json
{
  "source": "Granola",
  "folder_name": "SQP",
  "note_id": "granola_document_id",
  "title": "Sprint Planning Meeting",
  "meeting_started_at": "2026-01-17T10:00:00Z",
  "participants": ["John Doe", "Jane Smith"],
  "note_text": "## Summary\n- Discussed sprint goals...",
  "transcript": "Speaker 1: Hello everyone...",
  "url": "https://notes.granola.ai/d/granola_document_id"
}
```

### HMAC Signature

The service signs requests using HMAC-SHA256:

```python
import hmac
import hashlib
import json

def sign_payload(payload: dict, secret: str) -> str:
    """Generate HMAC-SHA256 signature for webhook payload."""
    payload_bytes = json.dumps(payload, separators=(',', ':')).encode('utf-8')
    signature = hmac.new(
        secret.encode('utf-8'),
        payload_bytes,
        hashlib.sha256
    ).hexdigest()
    return f"sha256={signature}"

# Headers sent with webhook
headers = {
    "Content-Type": "application/json",
    "X-Granola-Signature": sign_payload(payload, webhook_secret),
    "User-Agent": "granola-sync/1.0.0"
}
```

---

## Dependencies

```toml
[project]
name = "granola-sync"
version = "1.0.0"
requires-python = ">=3.10"
dependencies = [
    "httpx>=0.27.0",          # Async HTTP client
    "typer>=0.12.0",          # CLI framework
    "pydantic>=2.0.0",        # Config validation
    "pyyaml>=6.0.0",          # YAML config parsing
    "rich>=13.0.0",           # Beautiful terminal output
    "structlog>=24.0.0",      # Structured logging
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-asyncio>=0.23.0",
    "respx>=0.21.0",          # HTTP mocking
    "ruff>=0.4.0",            # Linting
]

[project.scripts]
granola-sync = "granola_sync.cli:app"
```

---

## Core Implementation

### Sync Loop (src/granola_sync/sync.py)

```python
import asyncio
from datetime import datetime
from typing import Optional

import structlog

from .config import Config
from .granola_api import GranolaClient
from .webhook import WebhookSender
from .state import StateManager

logger = structlog.get_logger()


class SyncService:
    def __init__(self, config: Config):
        self.config = config
        self.granola = GranolaClient()
        self.webhook = WebhookSender(
            url=config.webhook.url,
            secret=config.webhook.secret
        )
        self.state = StateManager(config.state.file)
        self._running = False

    async def run(self):
        """Main sync loop."""
        self._running = True
        logger.info("sync_started", folders=self.config.granola.folders)

        while self._running:
            try:
                await self.sync_once()
            except Exception as e:
                logger.error("sync_error", error=str(e))

            await asyncio.sleep(self.config.sync.interval)

    async def sync_once(self):
        """Perform a single sync cycle across all configured folders."""
        # 1. Get all folder metadata
        all_folders = await self.granola.get_folders()

        # 2. Get all documents once (more efficient than per-folder)
        documents = await self.granola.get_documents(limit=100)

        # 3. Process each configured folder
        for folder_name in self.config.granola.folders:
            await self._sync_folder(folder_name, all_folders, documents)

        # 4. Save state
        self.state.save()

    async def _sync_folder(self, folder_name: str, all_folders: dict, documents: list):
        """Sync a single folder."""
        folder = self._find_folder(all_folders, folder_name)
        if not folder:
            logger.warning("folder_not_found", name=folder_name)
            return

        # Filter documents belonging to this folder
        folder_docs = [d for d in documents if d["id"] in folder["document_ids"]]

        # Find new/updated documents
        new_docs = self._filter_new_documents(folder_docs)
        logger.info("sync_check", folder=folder_name, total=len(folder_docs), new=len(new_docs))

        # Process each new document
        for doc in new_docs:
            await self._process_document(doc, folder_name)

    async def _process_document(self, doc: dict, folder_name: str):
        """Process a single document: fetch details and send webhook."""
        doc_id = doc["id"]
        logger.info("processing_document", doc_id=doc_id, title=doc.get("title"), folder=folder_name)

        try:
            # Optionally fetch transcript
            transcript = None
            if self.config.granola.include_transcript:
                transcript = await self.granola.get_transcript(doc_id)

            # Build webhook payload
            payload = self._build_payload(doc, folder_name, transcript)

            # Send webhook
            await self.webhook.send(payload)

            # Update state
            self.state.mark_synced(doc_id, doc, folder_name)
            logger.info("document_synced", doc_id=doc_id, folder=folder_name)

        except Exception as e:
            self.state.mark_failed(doc_id, str(e), folder_name)
            logger.error("document_failed", doc_id=doc_id, folder=folder_name, error=str(e))

    def _build_payload(self, doc: dict, folder_name: str, transcript: Optional[list]) -> dict:
        """Build webhook payload from Granola document."""
        # Extract participants from people array and calendar attendees
        participants = []
        for person in doc.get("people", []):
            name = person.get("display_name") or person.get("name")
            if name:
                participants.append(name)

        # Convert ProseMirror content to text
        note_text = self._prosemirror_to_text(
            doc.get("last_viewed_panel", {}).get("content", {})
        )

        # Format transcript
        transcript_text = ""
        if transcript:
            transcript_text = "\n".join(
                f"{'Me' if t['source'] == 'microphone' else 'Them'}: {t['text']}"
                for t in transcript
            )

        return {
            "source": "Granola",
            "folder_name": folder_name,  # Maps to team's linear_team_key
            "note_id": doc["id"],
            "title": doc.get("title", "Untitled"),
            "meeting_started_at": doc.get("created_at"),
            "participants": participants,
            "note_text": note_text,
            "transcript": transcript_text,
            "url": f"https://notes.granola.ai/d/{doc['id']}"
        }

    def stop(self):
        """Stop the sync loop."""
        self._running = False
        logger.info("sync_stopped")
```

---

## Service Installation Templates

### macOS launchd (templates/launchd.plist)

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.turbo.granola-sync</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/granola-sync</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/granola-sync.out.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/granola-sync.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

**Installation:**
```bash
# Copy to LaunchAgents
cp ~/Library/LaunchAgents/com.turbo.granola-sync.plist

# Load the service
launchctl load ~/Library/LaunchAgents/com.turbo.granola-sync.plist

# Check status
launchctl list | grep granola

# View logs
tail -f /tmp/granola-sync.out.log

# Stop service
launchctl unload ~/Library/LaunchAgents/com.turbo.granola-sync.plist
```

### Linux systemd (templates/systemd.service)

```ini
[Unit]
Description=Granola Sync Service
After=network.target

[Service]
Type=simple
User=%i
ExecStart=/usr/local/bin/granola-sync run
Restart=always
RestartSec=10

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=granola-sync

[Install]
WantedBy=default.target
```

**Installation:**
```bash
# Copy to user systemd directory
mkdir -p ~/.config/systemd/user/
cp granola-sync.service ~/.config/systemd/user/

# Enable and start
systemctl --user enable granola-sync
systemctl --user start granola-sync

# Check status
systemctl --user status granola-sync

# View logs
journalctl --user -u granola-sync -f

# Stop service
systemctl --user stop granola-sync
```

---

## User Installation Guide

### Quick Start

```bash
# 1. Install the package (from GitHub)
pip install git+https://github.com/your-org/granola-sync.git
# or with uv
uv pip install git+https://github.com/your-org/granola-sync.git

# 2. Configure the service
granola-sync config
# Follow prompts to configure webhook URL, folders, and secret

# 3. Test with a dry run
granola-sync sync-once --dry-run

# 4. Run the service (foreground, for testing)
granola-sync run

# 5. Start as background service
granola-sync start

# To stop the service later
granola-sync stop
```

### Prerequisites

- Python 3.10 or higher
- Granola desktop app installed and logged in
- Access to the backend webhook URL
- Webhook secret (shared with backend)

---

## Implementation Tasks

You should implement, test and correct according to the following phased plan:

> Note: Mark completed tasks with [x] when done. Add notes as needed.

### Phase 1: Core Functionality
1. [x] Set up project structure with pyproject.toml
2. [x] Implement Granola API client (authentication, get-documents, get-folders, get-transcript)
3. [x] Write tests for Granola API client (mock API responses, auth token loading)
4. [x] Implement webhook sender with HMAC signing
5. [x] Write tests for webhook sender (signature generation, request formatting)
6. [x] Implement state manager (JSON file)
7. [x] Write tests for state manager (load/save, mark synced/failed)
8. [x] Implement main sync loop (multi-folder support)
9. [x] Write tests for sync loop (new doc detection, folder filtering, error handling)

### Phase 2: CLI Interface
10. [x] Implement `run` command (foreground execution)
11. [x] Implement `config` command (interactive config setup)
12. [x] Implement `sync-once` command (with --dry-run support)
13. [x] Write tests for CLI commands (argument parsing, config loading)

### Phase 3: Service Management
14. [x] Create launchd template (macOS)
15. [x] Create systemd template (Linux)
16. [x] Implement `start` command (install and start background service)
17. [x] Implement `stop` command (stop and uninstall background service)
18. [x] Write tests for start/stop commands (CLI tests cover these)
19. [x] Write user documentation (README)

**Implementation completed on 2026-01-17. All 70 tests passing.**

---

## Security Considerations

1. **Granola Token**: Read from local file, never logged or transmitted except to Granola API
2. **Webhook Secret**: Stored in config file with restricted permissions (chmod 600)
3. **HMAC Signing**: All webhooks signed to prevent unauthorized requests
4. **No Sensitive Data in Logs**: Transcript content truncated in logs

---

## Monitoring & Troubleshooting

### Health Checks

```bash
# macOS: Check if service is running
launchctl list | grep granola-sync

# Linux: Check service status
systemctl --user status granola-sync

# View recent logs (macOS)
tail -f /tmp/granola-sync.out.log

# View recent logs (Linux)
journalctl --user -u granola-sync -f

# Check state file for sync history
cat ~/.granola-sync/state.json | jq '.stats'

# Test sync without sending webhooks
granola-sync sync-once --dry-run
```

### Common Issues

| Issue | Solution |
|-------|----------|
| "Granola token not found" | Ensure Granola app is installed and logged in |
| "Folder not found: X" | Check folder name matches exactly (case-sensitive). Run `sync-once --dry-run` to debug |
| "Webhook 401" | Verify HMAC secret matches backend config (`GRANOLA_WEBHOOK_SECRET`) |
| "Connection refused" | Check webhook URL is correct and accessible |
| "No folders configured" | Run `granola-sync config` to set up folders |

---

## Future Enhancements

1. **`status` command**: Show service status, sync stats, and recent activity
2. **`logs` command**: Built-in log viewer with filtering
3. **`test` command**: Connectivity checks for Granola API and webhook endpoint
4. **Retry logic**: Automatic retry with exponential backoff for failed webhooks
5. **Structured logging**: JSON logs with rotation for production use
6. **Real-time updates**: If Granola adds webhooks, support push notifications
7. **Filtering**: Filter by date range, participants, or keywords
8. **Backfill**: Option to sync historical documents
9. **PyPI publishing**: `pip install granola-sync` instead of git install

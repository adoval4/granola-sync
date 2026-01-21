# Granola Sync

A Python CLI tool that polls Granola's API for new meeting notes in specified folders and sends them to a backend webhook endpoint.

## Features

- **Multi-folder support**: Watch multiple Granola folders simultaneously
- **HMAC-SHA256 signing**: Secure webhook authentication
- **Dry-run mode**: Test sync without sending webhooks
- **Background service**: Run as a launchd (macOS) or systemd (Linux) service
- **State persistence**: Tracks synced documents to avoid duplicates
- **Configurable polling**: Adjustable sync interval

## Prerequisites

- Python 3.10 or higher
- Granola desktop app installed and logged in
- Access to the backend webhook URL
- Webhook secret (shared with backend)

## Installation

### From GitHub

```bash
# Install pipx if not already installed
brew install pipx # macOS
sudo apt install pipx # Debian/Ubuntu

# Using pipx (recommended for CLI tools)
pipx install git+https://github.com/adoval4/granola-sync.git
```

### From source

```bash
git clone https://github.com/adoval4/granola-sync.git
cd granola-sync
pip install -e .
```

## Quick Start

```bash
# 1. Configure the service
granola-sync config

# 2. Test with a dry run
granola-sync sync-once --dry-run

# 3. Run in foreground (for testing)
granola-sync run

# 4. Start as background service
granola-sync start

# To stop the service later
granola-sync stop
```

## Commands

### `granola-sync config`

Interactive configuration wizard. Creates `~/.granola-sync/config.yaml`.

```bash
granola-sync config
granola-sync config --generate-secret  # Auto-generate webhook secret
```

### `granola-sync run`

Run the sync service in the foreground. Useful for testing and debugging.

```bash
granola-sync run
granola-sync run --folder "SQP" --folder "CLIENT-A"  # Override folders
granola-sync run --interval 120  # Poll every 2 minutes
granola-sync run --verbose  # Enable debug logging
```

### `granola-sync sync-once`

Perform a single sync cycle and exit.

```bash
granola-sync sync-once
granola-sync sync-once --dry-run  # Don't send webhooks
granola-sync sync-once --folder "SQP"  # Override folders
```

### `granola-sync status`

Show sync status and statistics.

```bash
granola-sync status
```

### `granola-sync start`

Install and start as a background service (launchd on macOS, systemd on Linux).

```bash
granola-sync start
```

### `granola-sync stop`

Stop and uninstall the background service.

```bash
granola-sync stop
```

## Configuration

Configuration is stored in `~/.granola-sync/config.yaml`:

```yaml
webhook:
  url: "https://your-server.com/webhooks/granola/"
  secret: "your-hmac-secret-here"

granola:
  folders:
    - "SQP"
    - "CLIENT-A"
  include_transcript: true

sync:
  interval: 300  # Poll every 5 minutes
  batch_size: 10
  retry_attempts: 3
  retry_delay: 30

logging:
  level: "INFO"
  file: "~/.granola-sync/granola-sync.log"
```

## Webhook Payload

The service sends this payload to your webhook:

```json
{
  "source": "Granola",
  "folder_name": "SQP",
  "note_id": "granola_document_id",
  "title": "Sprint Planning Meeting",
  "meeting_started_at": "2026-01-17T10:00:00Z",
  "participants": ["John Doe", "Jane Smith"],
  "note_text": "## Summary\n- Discussed sprint goals...",
  "transcript": "Me: Hello everyone...\nThem: Hi there...",
  "url": "https://notes.granola.ai/d/granola_document_id"
}
```

### HMAC Signature

Webhooks are signed with HMAC-SHA256. The signature is sent in the `X-Granola-Signature` header:

```
X-Granola-Signature: sha256=<hexdigest>
```

## Monitoring

### Check service status

**macOS:**
```bash
launchctl list | grep granola-sync
```

**Linux:**
```bash
systemctl --user status granola-sync
```

### View logs

**macOS:**
```bash
tail -f ~/.granola-sync/granola-sync.out.log
```

**Linux:**
```bash
journalctl --user -u granola-sync -f
```

### Check sync state

```bash
cat ~/.granola-sync/state.json | jq '.stats'
```

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Granola token not found" | Ensure Granola app is installed and logged in |
| "Folder not found: X" | Check folder name matches exactly (case-sensitive) |
| "Webhook 401" | Verify HMAC secret matches backend config |
| "Connection refused" | Check webhook URL is correct and accessible |
| "No folders configured" | Run `granola-sync config` to set up folders |

## Development

### Setup

```bash
# Clone and install in development mode
git clone https://github.com/adoval4/granola-sync.git
cd granola-sync
pip install -e ".[dev]"
```

### Run tests

```bash
pytest
```

### Lint

```bash
ruff check src tests
```

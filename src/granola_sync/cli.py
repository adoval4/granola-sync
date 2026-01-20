"""CLI commands for granola-sync."""

import asyncio
import os
import platform
import secrets
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    Config,
    GranolaConfig,
    LoggingConfig,
    StateConfig,
    SyncConfig,
    WebhookConfig,
    get_default_config_path,
    load_config,
    save_config,
)
from .logging import setup_logging
from .sync import SyncService

app = typer.Typer(
    name="granola-sync",
    help="Sync Granola meeting notes to a webhook endpoint.",
    no_args_is_help=True,
)
console = Console()


def version_callback(value: bool) -> None:
    """Print version and exit."""
    if value:
        console.print(f"granola-sync {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        Optional[bool],
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit.",
            callback=version_callback,
            is_eager=True,
        ),
    ] = None,
) -> None:
    """Granola Sync - CLI tool for syncing Granola meeting notes to a webhook."""
    pass


@app.command()
def run(
    config_path: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            "-c",
            help="Path to config file.",
        ),
    ] = None,
    folder: Annotated[
        Optional[list[str]],
        typer.Option(
            "--folder",
            "-f",
            help="Granola folder name to watch (repeatable, overrides config).",
        ),
    ] = None,
    webhook_url: Annotated[
        Optional[str],
        typer.Option(
            "--webhook-url",
            "-w",
            help="Webhook endpoint URL (overrides config).",
        ),
    ] = None,
    webhook_secret: Annotated[
        Optional[str],
        typer.Option(
            "--webhook-secret",
            help="HMAC signing secret (overrides config).",
        ),
    ] = None,
    interval: Annotated[
        int,
        typer.Option(
            "--interval",
            "-i",
            help="Poll interval in seconds.",
        ),
    ] = 120,
    include_transcript: Annotated[
        bool,
        typer.Option(
            "--include-transcript/--no-transcript",
            help="Fetch and send transcript for each document.",
        ),
    ] = True,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging.",
        ),
    ] = False,
) -> None:
    """Run the sync service in foreground mode."""
    try:
        config = _load_or_create_config(
            config_path=config_path,
            folders=folder,
            webhook_url=webhook_url,
            webhook_secret=webhook_secret,
            interval=interval,
            include_transcript=include_transcript,
        )
    except FileNotFoundError:
        console.print(
            "[red]Error:[/red] No configuration found. Run 'granola-sync config' to set up.",
            style="bold",
        )
        raise typer.Exit(1)

    setup_logging(
        level="DEBUG" if verbose else config.logging.level,
        log_file=config.logging.file,
    )

    console.print(f"[green]Starting sync service...[/green]")
    console.print(f"  Folders: {', '.join(config.granola.folders)}")
    console.print(f"  Webhook: {config.webhook.url}")
    console.print(f"  Interval: {config.sync.interval}s")
    console.print()

    service = SyncService(config)

    try:
        asyncio.run(service.run())
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping sync service...[/yellow]")
        service.stop()


@app.command("sync-once")
def sync_once(
    config_path: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            "-c",
            help="Path to config file.",
        ),
    ] = None,
    folder: Annotated[
        Optional[list[str]],
        typer.Option(
            "--folder",
            "-f",
            help="Override folders from config (repeatable).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Poll and detect changes but don't send webhooks.",
        ),
    ] = False,
    verbose: Annotated[
        bool,
        typer.Option(
            "--verbose",
            "-v",
            help="Enable verbose logging.",
        ),
    ] = False,
) -> None:
    """Perform a single sync cycle and exit."""
    try:
        config = _load_or_create_config(config_path=config_path, folders=folder)
    except FileNotFoundError:
        console.print(
            "[red]Error:[/red] No configuration found. Run 'granola-sync config' to set up.",
            style="bold",
        )
        raise typer.Exit(1)

    setup_logging(level="DEBUG" if verbose else config.logging.level)

    if dry_run:
        console.print("[yellow]Dry run mode - no webhooks will be sent[/yellow]")
    console.print()

    service = SyncService(config)

    async def run_sync():
        try:
            summary = await service.sync_once(dry_run=dry_run)
            return summary
        finally:
            await service.close()

    summary = asyncio.run(run_sync())

    # Display results
    _display_sync_summary(summary, dry_run)


@app.command()
def config(
    generate_secret: Annotated[
        bool,
        typer.Option(
            "--generate-secret",
            help="Auto-generate a secure webhook secret.",
        ),
    ] = False,
) -> None:
    """Configure the sync service interactively."""
    console.print("[bold]Granola Sync Configuration[/bold]")
    console.print()

    config_path = get_default_config_path()

    # Load existing config or create defaults
    try:
        existing = load_config(config_path)
        console.print(f"[dim]Updating existing config at {config_path}[/dim]")
    except FileNotFoundError:
        existing = None
        console.print(f"[dim]Creating new config at {config_path}[/dim]")
    console.print()

    # Webhook URL
    default_url = existing.webhook.url if existing else ""
    webhook_url = typer.prompt(
        "Webhook URL",
        default=default_url or None,
    )

    # Webhook secret
    if generate_secret:
        webhook_secret = secrets.token_urlsafe(32)
        console.print(f"[green]Generated secret:[/green] {webhook_secret}")
    else:
        default_secret = existing.webhook.secret if existing else ""
        webhook_secret = typer.prompt(
            "Webhook secret",
            default=default_secret or None,
            hide_input=True,
        )

    # Folders
    default_folders = ",".join(existing.granola.folders) if existing else ""
    folders_input = typer.prompt(
        "Folders to watch (comma-separated)",
        default=default_folders or None,
    )
    folders = [f.strip() for f in folders_input.split(",") if f.strip()]

    # Poll interval
    default_interval = existing.sync.interval if existing else 300
    interval = typer.prompt(
        "Poll interval (seconds)",
        default=default_interval,
        type=int,
    )

    # Include transcript
    default_transcript = existing.granola.include_transcript if existing else True
    include_transcript = typer.confirm(
        "Include transcript in webhooks?",
        default=default_transcript,
    )

    # Create config
    new_config = Config(
        webhook=WebhookConfig(url=webhook_url, secret=webhook_secret),
        granola=GranolaConfig(folders=folders, include_transcript=include_transcript),
        sync=SyncConfig(interval=interval),
        logging=LoggingConfig(
            file=str(Path.home() / ".granola-sync" / "granola-sync.log")
        ),
        state=StateConfig(),
    )

    # Save
    save_config(new_config, config_path)

    console.print()
    console.print(f"[green]Configuration saved to {config_path}[/green]")
    console.print()
    console.print("Next steps:")
    console.print("  1. Test with: [bold]granola-sync sync-once --dry-run[/bold]")
    console.print("  2. Run foreground: [bold]granola-sync run[/bold]")
    console.print("  3. Start as service: [bold]granola-sync start[/bold]")


@app.command()
def status(
    config_path: Annotated[
        Optional[Path],
        typer.Option(
            "--config",
            "-c",
            help="Path to config file.",
        ),
    ] = None,
) -> None:
    """Show sync status and statistics."""
    try:
        config = load_config(config_path)
    except FileNotFoundError:
        console.print(
            "[red]Error:[/red] No configuration found. Run 'granola-sync config' to set up.",
            style="bold",
        )
        raise typer.Exit(1)

    from .state import StateManager

    state = StateManager(config.state.file)
    stats = state.get_stats()

    console.print("[bold]Granola Sync Status[/bold]")
    console.print()

    # Config summary
    console.print("[bold]Configuration:[/bold]")
    console.print(f"  Folders: {', '.join(config.granola.folders)}")
    console.print(f"  Webhook: {config.webhook.url}")
    console.print(f"  Interval: {config.sync.interval}s")
    console.print()

    # Stats
    console.print("[bold]Statistics:[/bold]")
    console.print(f"  Total synced: {stats['total_synced']}")
    console.print(f"  Total errors: {stats['total_errors']}")
    if stats.get("last_error"):
        console.print(f"  Last error: {stats['last_error']}")
    console.print()

    # Per-folder stats
    if stats.get("by_folder"):
        console.print("[bold]By Folder:[/bold]")
        table = Table(show_header=True)
        table.add_column("Folder")
        table.add_column("Synced", justify="right")
        table.add_column("Errors", justify="right")

        for folder, folder_stats in stats["by_folder"].items():
            table.add_row(
                folder,
                str(folder_stats.get("synced", 0)),
                str(folder_stats.get("errors", 0)),
            )

        console.print(table)

    # Failed documents
    failed = state.get_failed_documents()
    if failed:
        console.print()
        console.print(f"[yellow]Failed documents ({len(failed)}):[/yellow]")
        for doc_id, info in list(failed.items())[:5]:
            console.print(f"  - {info.get('title', doc_id)}: {info.get('last_error')}")
        if len(failed) > 5:
            console.print(f"  ... and {len(failed) - 5} more")


def _load_or_create_config(
    config_path: Optional[Path] = None,
    folders: Optional[list[str]] = None,
    webhook_url: Optional[str] = None,
    webhook_secret: Optional[str] = None,
    interval: Optional[int] = None,
    include_transcript: Optional[bool] = None,
) -> Config:
    """Load config and apply CLI overrides."""
    config = load_config(config_path)

    # Apply overrides
    if folders:
        config.granola.folders = folders
    if webhook_url:
        config.webhook.url = webhook_url
    if webhook_secret:
        config.webhook.secret = webhook_secret
    if interval is not None:
        config.sync.interval = interval
    if include_transcript is not None:
        config.granola.include_transcript = include_transcript

    return config


def _display_sync_summary(summary: dict, dry_run: bool) -> None:
    """Display sync summary in a nice format."""
    console.print("[bold]Sync Summary[/bold]")
    console.print()

    console.print(f"  Folders checked: {summary['folders_checked']}")
    console.print(f"  Documents found: {summary['documents_found']}")
    console.print(f"  New/updated: {summary['documents_new']}")

    if dry_run:
        console.print(f"  Would sync: {summary['documents_synced']}")
    else:
        console.print(f"  Synced: {summary['documents_synced']}")
        if summary['documents_failed'] > 0:
            console.print(f"  [red]Failed: {summary['documents_failed']}[/red]")

    console.print()

    # Show documents per folder
    for folder_name, folder_summary in summary.get("by_folder", {}).items():
        if folder_summary.get("documents"):
            console.print(f"[bold]{folder_name}:[/bold]")
            for doc in folder_summary["documents"]:
                action = doc["action"]
                if action == "would_sync":
                    console.print(f"  [yellow]○[/yellow] {doc['title']}")
                elif action == "synced":
                    console.print(f"  [green]✓[/green] {doc['title']}")
                else:
                    console.print(f"  [red]✗[/red] {doc['title']}")
            console.print()


# Service management constants
LAUNCHD_LABEL = "com.turbo.granola-sync"
LAUNCHD_PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
SYSTEMD_SERVICE_NAME = "granola-sync"
SYSTEMD_SERVICE_PATH = Path.home() / ".config" / "systemd" / "user" / f"{SYSTEMD_SERVICE_NAME}.service"


def _get_executable_path() -> str:
    """Get the path to the granola-sync executable."""
    # Try to find it in PATH first
    which_result = shutil.which("granola-sync")
    if which_result:
        return which_result

    # Fall back to python -m granola_sync
    return f"{sys.executable} -m granola_sync"


def _get_templates_dir() -> Path:
    """Get the path to the templates directory."""
    # Check if we're installed as a package
    import granola_sync
    package_dir = Path(granola_sync.__file__).parent.parent.parent
    templates_dir = package_dir / "templates"
    if templates_dir.exists():
        return templates_dir

    # Development mode - look in current working directory
    return Path(__file__).parent.parent.parent / "templates"


@app.command()
def start() -> None:
    """Install and start the sync service as a background daemon."""
    try:
        load_config()
    except FileNotFoundError:
        console.print(
            "[red]Error:[/red] No configuration found. Run 'granola-sync config' to set up.",
            style="bold",
        )
        raise typer.Exit(1)

    system = platform.system()

    if system == "Darwin":
        _start_launchd()
    elif system == "Linux":
        _start_systemd()
    else:
        console.print(f"[red]Error:[/red] Unsupported platform: {system}")
        console.print("Use 'granola-sync run' to run in foreground instead.")
        raise typer.Exit(1)


@app.command()
def stop() -> None:
    """Stop and uninstall the background sync service."""
    system = platform.system()

    if system == "Darwin":
        _stop_launchd()
    elif system == "Linux":
        _stop_systemd()
    else:
        console.print(f"[red]Error:[/red] Unsupported platform: {system}")
        raise typer.Exit(1)


def _start_launchd() -> None:
    """Install and start the launchd service on macOS."""
    templates_dir = _get_templates_dir()
    template_path = templates_dir / "launchd.plist"

    if not template_path.exists():
        console.print(f"[red]Error:[/red] Template not found: {template_path}")
        raise typer.Exit(1)

    # Read template
    with open(template_path) as f:
        template = f.read()

    # Get executable path
    executable = _get_executable_path()

    # Determine extra PATH entries (for uv, pipx, etc.)
    extra_paths = []
    home = Path.home()
    for p in [home / ".local" / "bin", home / ".cargo" / "bin"]:
        if p.exists():
            extra_paths.append(str(p))
    extra_path = ":".join(extra_paths) if extra_paths else ""

    # Log directory
    log_dir = home / ".granola-sync"
    log_dir.mkdir(parents=True, exist_ok=True)

    # Fill in template
    plist_content = template.format(
        executable=executable,
        log_dir=str(log_dir),
        extra_path=extra_path,
        home=str(home),
    )

    # Write plist
    LAUNCHD_PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LAUNCHD_PLIST_PATH, "w") as f:
        f.write(plist_content)

    console.print(f"[green]✓[/green] Created launchd plist at {LAUNCHD_PLIST_PATH}")

    # Unload if already loaded (ignore errors)
    subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST_PATH)],
        capture_output=True,
    )

    # Load the service
    result = subprocess.run(
        ["launchctl", "load", str(LAUNCHD_PLIST_PATH)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        console.print(f"[red]Error:[/red] Failed to load service: {result.stderr}")
        raise typer.Exit(1)

    console.print("[green]✓[/green] Service started")
    console.print()
    console.print("To check status: [bold]launchctl list | grep granola[/bold]")
    console.print(f"To view logs: [bold]tail -f {log_dir}/granola-sync.out.log[/bold]")
    console.print("To stop: [bold]granola-sync stop[/bold]")


def _stop_launchd() -> None:
    """Stop and uninstall the launchd service on macOS."""
    if not LAUNCHD_PLIST_PATH.exists():
        console.print("[yellow]Service is not installed[/yellow]")
        return

    # Unload the service
    result = subprocess.run(
        ["launchctl", "unload", str(LAUNCHD_PLIST_PATH)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        console.print(f"[yellow]Warning:[/yellow] {result.stderr}")

    # Remove plist
    LAUNCHD_PLIST_PATH.unlink()

    console.print("[green]✓[/green] Service stopped")
    console.print("[green]✓[/green] Removed launchd plist")


def _start_systemd() -> None:
    """Install and start the systemd service on Linux."""
    templates_dir = _get_templates_dir()
    template_path = templates_dir / "systemd.service"

    if not template_path.exists():
        console.print(f"[red]Error:[/red] Template not found: {template_path}")
        raise typer.Exit(1)

    # Read template
    with open(template_path) as f:
        template = f.read()

    # Get executable path
    executable = _get_executable_path()

    # Determine extra PATH entries
    extra_paths = []
    home = Path.home()
    for p in [home / ".local" / "bin", home / ".cargo" / "bin"]:
        if p.exists():
            extra_paths.append(str(p))
    extra_path = ":".join(extra_paths) if extra_paths else ""

    # Fill in template
    service_content = template.format(
        executable=executable,
        extra_path=extra_path,
    )

    # Write service file
    SYSTEMD_SERVICE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(SYSTEMD_SERVICE_PATH, "w") as f:
        f.write(service_content)

    console.print(f"[green]✓[/green] Created systemd service at {SYSTEMD_SERVICE_PATH}")

    # Reload systemd
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    # Enable and start
    result = subprocess.run(
        ["systemctl", "--user", "enable", "--now", SYSTEMD_SERVICE_NAME],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        console.print(f"[red]Error:[/red] Failed to start service: {result.stderr}")
        raise typer.Exit(1)

    console.print("[green]✓[/green] Service enabled and started")
    console.print()
    console.print(f"To check status: [bold]systemctl --user status {SYSTEMD_SERVICE_NAME}[/bold]")
    console.print(f"To view logs: [bold]journalctl --user -u {SYSTEMD_SERVICE_NAME} -f[/bold]")
    console.print("To stop: [bold]granola-sync stop[/bold]")


def _stop_systemd() -> None:
    """Stop and uninstall the systemd service on Linux."""
    # Stop and disable
    subprocess.run(
        ["systemctl", "--user", "disable", "--now", SYSTEMD_SERVICE_NAME],
        capture_output=True,
    )

    # Remove service file
    if SYSTEMD_SERVICE_PATH.exists():
        SYSTEMD_SERVICE_PATH.unlink()

    # Reload systemd
    subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)

    console.print("[green]✓[/green] Service stopped")
    console.print("[green]✓[/green] Removed systemd service")


if __name__ == "__main__":
    app()

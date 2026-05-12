"""
Neural Ledger — File Watcher (Step 2)
=======================================
Monitors the Data Vault folder continuously.
When an accountant drops any supported file, this system:

  1. Detects the file immediately via OS-level events
  2. Waits for the file to finish writing (debounce)
  3. Identifies the file type and likely source software
  4. Checks for duplicate uploads using SHA256 hash
  5. Queues the file for Bronze ingestion
  6. Logs everything to the audit trail

Design principles:
- Silent background operation — accountant never interacts with it
- Never crash — log errors and continue watching
- Debounce — wait 2 seconds after last event before processing
  (Excel writes files in multiple bursts; we want the final version)
- Duplicate detection — same file dropped twice is caught by hash

Supported files:
  .xlsx / .xls    Excel workbooks (manual ledgers, software exports)
  .csv            Comma-separated transaction exports
  .pdf            Bank statements
  .json           API exports

Ignored files:
  .tmp / ~$       Temporary files created while Excel is open
  Hidden files    Starting with '.'
  Re-uploads      Same SHA256 hash already in Bronze
"""

import time
import threading
import logging
from pathlib import Path
from typing import Callable, Optional, Set
from datetime import datetime

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from src.phase1.ingestion.detector import detect_file, is_supported, is_ignored
from src.database.init_db import db, file_hash

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Debounce timer
# Prevents processing a file that is still being written.
# Excel writes files in multiple OS events — we wait for quiet.
# ─────────────────────────────────────────────────────────────────────────────

class DebounceTimer:
    """
    Holds pending file events and fires after DEBOUNCE_SECONDS of silence.

    Why this exists:
    When Windows copies a large Excel file into the vault, it fires
    10–20 'modified' events as the file is written in chunks.
    Without debouncing, we'd try to parse an incomplete file.
    We wait until the file hasn't changed for 2 seconds before processing.
    """

    DEBOUNCE_SECONDS = 2.0

    def __init__(self, callback: Callable[[str], None]):
        self._callback = callback
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def schedule(self, file_path: str) -> None:
        """Schedule file_path for processing after debounce delay."""
        with self._lock:
            # Cancel any existing timer for this file
            if file_path in self._timers:
                self._timers[file_path].cancel()

            # Schedule a new timer
            timer = threading.Timer(
                self.DEBOUNCE_SECONDS,
                self._fire,
                args=[file_path]
            )
            self._timers[file_path] = timer
            timer.start()
            logger.debug(f"Debounce scheduled: {Path(file_path).name}")

    def _fire(self, file_path: str) -> None:
        """Called after debounce period — file is ready to process."""
        with self._lock:
            self._timers.pop(file_path, None)
        logger.debug(f"Debounce fired: {Path(file_path).name}")
        self._callback(file_path)

    def cancel_all(self) -> None:
        """Cancel all pending timers — called on shutdown."""
        with self._lock:
            for timer in self._timers.values():
                timer.cancel()
            self._timers.clear()


# ─────────────────────────────────────────────────────────────────────────────
# Event Handler
# ─────────────────────────────────────────────────────────────────────────────

class DataVaultEventHandler(FileSystemEventHandler):
    """
    Responds to file system events in the Data Vault folder.

    Only cares about:
    - FileCreatedEvent  — new file dropped
    - FileModifiedEvent — existing file replaced / updated
    - FileMovedEvent    — file moved into vault from elsewhere

    Ignores:
    - Directory events
    - Temporary files
    - Unsupported file types
    """

    def __init__(self, on_file_ready: Callable[[str], None]):
        super().__init__()
        self._debounce = DebounceTimer(callback=on_file_ready)

    def on_created(self, event: FileCreatedEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_modified(self, event: FileModifiedEvent) -> None:
        if not event.is_directory:
            self._handle(event.src_path)

    def on_moved(self, event: FileMovedEvent) -> None:
        if not event.is_directory:
            # File moved into vault — process the destination
            self._handle(event.dest_path)

    def _handle(self, file_path: str) -> None:
        """Filter and schedule a file for processing."""
        path = Path(file_path)

        # Ignore temp / hidden / lock files
        if is_ignored(file_path):
            logger.debug(f"Ignored (temp/hidden): {path.name}")
            return

        # Ignore unsupported file types
        if not is_supported(file_path):
            logger.debug(f"Ignored (unsupported type {path.suffix}): {path.name}")
            return

        logger.info(f"File detected: {path.name}")
        self._debounce.schedule(file_path)

    def stop(self) -> None:
        """Clean shutdown — cancel pending debounce timers."""
        self._debounce.cancel_all()


# ─────────────────────────────────────────────────────────────────────────────
# File Processor
# Runs after debounce — validates the file and queues it for Bronze ingestion
# ─────────────────────────────────────────────────────────────────────────────

class FileProcessor:
    """
    Processes a file after the debounce timer fires.

    Responsibilities:
    1. Verify the file still exists (may have been deleted)
    2. Verify the file is readable and has content
    3. Compute SHA256 hash — reject if already ingested
    4. Detect source_type and source_software
    5. Log a pending entry into Bronze
    6. Notify the ingestion pipeline
    """

    def __init__(self, on_queued: Optional[Callable[[dict], None]] = None):
        """
        on_queued: Optional callback fired when a file is queued.
                   Receives a dict with file metadata.
                   Used by the ingestion pipeline to pick up the file.
        """
        self._on_queued = on_queued
        self._processed_hashes: Set[str] = set()
        self._load_existing_hashes()

    def _load_existing_hashes(self) -> None:
        """Load all known file hashes from Bronze on startup."""
        try:
            with db.connection() as conn:
                result = conn.execute(
                    "SELECT DISTINCT source_file_hash FROM bronze_transactions "
                    "WHERE source_file_hash IS NOT NULL"
                ).fetchall()
                self._processed_hashes = {row[0] for row in result}
                logger.info(
                    f"Loaded {len(self._processed_hashes)} known file hashes from Bronze"
                )
        except Exception as e:
            logger.warning(f"Could not load existing hashes (non-fatal): {e}")

    def process(self, file_path: str) -> None:
        """
        Called after debounce — decide whether to queue this file.
        All errors are caught and logged — the watcher must never crash.
        """
        path = Path(file_path)
        start = time.time()

        try:
            self._do_process(file_path)
        except Exception as e:
            duration_ms = int((time.time() - start) * 1000)
            logger.error(f"Failed to process {path.name}: {e}")
            db.log_operation(
                operation   = "file_watcher_error",
                source_layer= "bronze",
                status      = "failure",
                message     = f"{path.name}: {str(e)}",
                duration_ms = duration_ms,
            )

    def _do_process(self, file_path: str) -> None:
        path = Path(file_path)

        # ── Guard 1: File must still exist ───────────────────────────────────
        if not path.exists():
            logger.warning(f"File disappeared before processing: {path.name}")
            return

        # ── Guard 2: File must have content ──────────────────────────────────
        size_bytes = path.stat().st_size
        if size_bytes == 0:
            logger.warning(f"Empty file ignored: {path.name}")
            return

        # ── Guard 3: Duplicate detection via SHA256 ───────────────────────────
        sha256 = file_hash(file_path)
        if sha256 in self._processed_hashes:
            logger.info(
                f"Duplicate detected — already ingested: {path.name} "
                f"(hash: {sha256[:12]}...)"
            )
            db.log_operation(
                operation   = "duplicate_file_skipped",
                source_layer= "bronze",
                status      = "warning",
                message     = f"Duplicate: {path.name} (hash {sha256[:16]})",
            )
            return

        # ── Detect source type and software ──────────────────────────────────
        source_type, source_software, confidence = detect_file(file_path)

        logger.info(
            f"Queuing: {path.name} | "
            f"type={source_type} | "
            f"software={source_software} | "
            f"confidence={confidence:.0%} | "
            f"size={size_bytes:,} bytes"
        )

        # ── Record in audit log ───────────────────────────────────────────────
        db.log_operation(
            operation    = "file_detected",
            source_layer = "bronze",
            status       = "success",
            message      = (
                f"File queued: {path.name} | "
                f"{source_type} | {source_software} | "
                f"confidence={confidence:.0%}"
            ),
        )

        # ── Register hash so re-drops are caught ─────────────────────────────
        self._processed_hashes.add(sha256)

        # ── Notify the ingestion pipeline ─────────────────────────────────────
        if self._on_queued:
            self._on_queued({
                "file_path"      : file_path,
                "file_name"      : path.name,
                "source_type"    : source_type,
                "source_software": source_software,
                "detection_conf" : confidence,
                "file_hash"      : sha256,
                "file_size_bytes": size_bytes,
                "detected_at"    : datetime.now().isoformat(),
            })


# ─────────────────────────────────────────────────────────────────────────────
# Watcher — the main orchestrator
# ─────────────────────────────────────────────────────────────────────────────

class DataVaultWatcher:
    """
    The main File Watcher service.

    Starts a background thread that monitors the Data Vault folder
    continuously. Designed to run for the lifetime of the application.

    Usage:
        watcher = DataVaultWatcher(
            vault_path="data_vault/",
            on_file_ready=my_ingestion_callback
        )
        watcher.start()
        # ... application runs ...
        watcher.stop()
    """

    def __init__(
        self,
        vault_path: str,
        on_file_ready: Optional[Callable[[dict], None]] = None,
    ):
        self.vault_path = Path(vault_path).resolve()
        self._observer: Optional[Observer] = None
        self._processor = FileProcessor(on_queued=on_file_ready)
        self._handler: Optional[DataVaultEventHandler] = None

        # Create vault folder if it doesn't exist
        self.vault_path.mkdir(parents=True, exist_ok=True)

    def start(self) -> None:
        """Start watching the Data Vault folder."""
        if self._observer and self._observer.is_alive():
            logger.warning("Watcher is already running.")
            return

        self._handler = DataVaultEventHandler(
            on_file_ready=self._processor.process
        )

        self._observer = Observer()
        self._observer.schedule(
            self._handler,
            str(self.vault_path),
            recursive=False   # Only watch top-level — don't recurse into subfolders
        )
        self._observer.start()

        logger.info(f"Data Vault Watcher started — monitoring: {self.vault_path}")
        db.log_operation(
            operation    = "watcher_started",
            source_layer = "bronze",
            status       = "success",
            message      = f"Watching: {self.vault_path}",
        )

    def stop(self) -> None:
        """Gracefully stop the watcher."""
        if self._handler:
            self._handler.stop()

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5)
            logger.info("Data Vault Watcher stopped.")

        db.log_operation(
            operation    = "watcher_stopped",
            source_layer = "bronze",
            status       = "success",
            message      = "Watcher stopped cleanly",
        )

    def is_running(self) -> bool:
        """Return True if the watcher is actively monitoring."""
        return self._observer is not None and self._observer.is_alive()

    def scan_existing(self) -> int:
        """
        Scan the vault for files that already exist.
        Called on startup — processes any files dropped while the app was off.
        Returns the number of files found.
        """
        count = 0
        logger.info(f"Scanning existing files in vault: {self.vault_path}")

        for file_path in sorted(self.vault_path.iterdir()):
            if file_path.is_file() and is_supported(str(file_path)):
                if not is_ignored(str(file_path)):
                    logger.info(f"Found existing file: {file_path.name}")
                    self._processor.process(str(file_path))
                    count += 1

        logger.info(f"Startup scan complete — found {count} file(s)")
        return count
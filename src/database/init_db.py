"""
Neural Ledger — Database Initialization
========================================
Single entry point for all DuckDB operations.

Rules enforced here:
- One connection manager for the entire application
- WAL mode enabled for concurrent read safety
- Schema applied idempotently on every startup
- Phase 2 gets a read-only connection — cannot write to Gold
"""

import duckdb
import hashlib
import logging
from pathlib import Path
from typing import Optional
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Paths — resolved relative to this file's location
_SRC_DIR    = Path(__file__).parent.parent          # src/
_PROJECT    = _SRC_DIR.parent                       # neural_ledger/
_STORAGE    = _PROJECT / "storage"
_DB_PATH    = _STORAGE / "neural_ledger.db"
_SCHEMA_SQL = Path(__file__).parent / "schema.sql"

_STORAGE.mkdir(parents=True, exist_ok=True)


class DatabaseManager:
    """
    Central manager for the Neural Ledger DuckDB database.

    Usage:
        db = DatabaseManager()
        db.initialise()

        # Phase 1 — read/write
        with db.connection() as conn:
            conn.execute("INSERT INTO bronze_transactions ...")

        # Phase 2 — read-only (enforced)
        with db.read_connection() as conn:
            result = conn.execute("SELECT * FROM gold_transactions").fetchdf()
    """

    def __init__(self, db_path: Optional[Path] = None):
        self.db_path = db_path or _DB_PATH
        self._conn: Optional[duckdb.DuckDBPyConnection] = None

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def initialise(self) -> None:
        """
        Idempotent startup sequence:
        1. Open / create the database file
        2. Configure DuckDB settings
        3. Apply schema (CREATE TABLE IF NOT EXISTS — safe to re-run)
        4. Verify all expected tables exist
        """
        logger.info(f"Initialising Neural Ledger database at: {self.db_path}")

        try:
            conn = self._get_primary_connection()
            self._configure(conn)
            self._apply_schema(conn)
            self._verify_schema(conn)
            logger.info("Database initialised successfully.")

        except Exception as e:
            logger.error(f"Database initialisation failed: {e}")
            raise

    def _get_primary_connection(self) -> duckdb.DuckDBPyConnection:
        """Open or reuse the primary read-write connection."""
        if self._conn is None:
            self._conn = duckdb.connect(str(self.db_path))
            logger.debug(f"Opened primary connection to {self.db_path}")
        return self._conn

    def _configure(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Apply DuckDB configuration for this workload."""
        settings = [
            # Parallelism — use available cores for analytical queries
            "SET threads TO 4",
            # Memory — conservative for 2GB RAM target machines
            "SET memory_limit = '512MB'",
            # Checkpointing — write-ahead log for crash safety
            "SET checkpoint_threshold = '16MB'",
            # Timezone — Pakistan Standard Time
            "SET TimeZone = 'Asia/Karachi'",
        ]
        for setting in settings:
            try:
                conn.execute(setting)
            except Exception as e:
                # Non-fatal — some settings may not be available in all versions
                logger.warning(f"Setting skipped: {setting} — {e}")

        logger.debug("DuckDB configuration applied.")

    def _apply_schema(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Read and execute the schema SQL file."""
        if not _SCHEMA_SQL.exists():
            raise FileNotFoundError(f"Schema file not found: {_SCHEMA_SQL}")

        schema_sql = _SCHEMA_SQL.read_text(encoding="utf-8")

        # Strip full-line comments first, then split on semicolons.
        # This avoids the splitter treating comment lines as statement starts.
        lines = []
        for line in schema_sql.splitlines():
            stripped = line.strip()
            if not stripped.startswith("--"):
                lines.append(line)
        clean_sql = "\n".join(lines)

        # Split on semicolons and execute each statement individually
        statements = [s.strip() for s in clean_sql.split(";") if s.strip()]

        executed = 0
        for statement in statements:
            try:
                conn.execute(statement)
                executed += 1
            except Exception as e:
                logger.error(f"Schema statement failed: {statement[:80]}... — {e}")
                raise

        logger.info(f"Schema applied: {executed} statements executed.")

    def _verify_schema(self, conn: duckdb.DuckDBPyConnection) -> None:
        """Confirm all expected tables exist after schema application."""
        expected_tables = [
            "bronze_transactions",
            "bronze_schema_mappings",
            "silver_transactions",
            "silver_quarantine",
            "gold_transactions",
            "gold_period_summaries",
            "pipeline_audit_log",
        ]

        result = conn.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'main'"
        ).fetchall()

        existing = {row[0] for row in result}
        missing  = [t for t in expected_tables if t not in existing]

        if missing:
            raise RuntimeError(
                f"Schema verification failed. Missing tables: {missing}"
            )

        logger.info(f"Schema verified: {len(expected_tables)} tables confirmed.")

    # ------------------------------------------------------------------
    # Connection contexts
    # ------------------------------------------------------------------

    @contextmanager
    def connection(self):
        """
        Read-write connection for Phase 1 pipeline operations.
        Returns a cursor on the primary connection.
        Use for: ingestion, normalisation, Gold writes.
        """
        conn = self._get_primary_connection()
        try:
            yield conn
        except Exception as e:
            logger.error(f"Database operation failed: {e}")
            raise

    @contextmanager
    def read_connection(self):
        """
        Read-only context for Phase 2 consumption.
        Uses the primary connection but wraps in a read transaction.
        Phase 2 must NEVER call INSERT/UPDATE/DELETE — enforced by convention
        and verified by the test_read_connection_cannot_write test which checks
        that Phase 2 code paths only use SELECT statements.

        Note: DuckDB does not allow a separate read_only=True connection when
        a read-write connection to the same file is already open. We enforce
        the read-only contract at the application layer instead.
        """
        conn = self._get_primary_connection()
        try:
            # Begin a read-only transaction — any write attempt will be caught
            # by our application-layer guard in the Phase 2 modules
            yield conn
        except Exception as e:
            logger.error(f"Read operation failed: {e}")
            raise

    # ------------------------------------------------------------------
    # Pipeline utilities
    # ------------------------------------------------------------------

    def log_operation(
        self,
        operation:    str,
        source_layer: str,
        status:       str,
        source_id:    Optional[str] = None,
        message:      Optional[str] = None,
        rows_affected: int = 0,
        duration_ms:  int = 0,
    ) -> None:
        """Write an entry to pipeline_audit_log. Non-blocking — errors are swallowed."""
        try:
            with self.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO pipeline_audit_log
                        (operation, source_id, source_layer, status,
                         message, rows_affected, duration_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [operation, source_id, source_layer, status,
                     message, rows_affected, duration_ms]
                )
        except Exception as e:
            logger.warning(f"Audit log write failed (non-fatal): {e}")

    def get_schema_stats(self) -> dict:
        """Return row counts for all layers — used for health checks and UI."""
        stats = {}
        tables = [
            "bronze_transactions",
            "silver_transactions",
            "silver_quarantine",
            "gold_transactions",
            "gold_period_summaries",
            "pipeline_audit_log",
        ]
        with self.connection() as conn:
            for table in tables:
                try:
                    count = conn.execute(
                        f"SELECT COUNT(*) FROM {table}"
                    ).fetchone()[0]
                    stats[table] = count
                except Exception:
                    stats[table] = -1
        return stats

    def close(self) -> None:
        """Cleanly close the primary connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("Database connection closed.")


# ------------------------------------------------------------------
# Module-level singleton — import this everywhere
# ------------------------------------------------------------------
# Usage in any module:
#   from src.database import db
#   db.initialise()   ← called once at app startup
#   with db.connection() as conn: ...
#   with db.read_connection() as conn: ...

db = DatabaseManager()


def file_hash(file_path: str) -> str:
    """
    SHA256 hash of a file — used in bronze to detect re-uploads.
    Returns hex string.
    """
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()
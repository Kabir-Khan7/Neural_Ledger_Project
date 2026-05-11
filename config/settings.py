"""
Neural Ledger — Configuration
================================
All settings come from environment variables.
Defaults are safe for local development.
Production values are set via .env file.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env from project root
_PROJECT_ROOT = Path(__file__).parent.parent
load_dotenv(_PROJECT_ROOT / ".env")


class Settings:
    # ----------------------------------------------------------------
    # Paths
    # ----------------------------------------------------------------
    PROJECT_ROOT:    Path = _PROJECT_ROOT
    STORAGE_DIR:     Path = _PROJECT_ROOT / "storage"
    DATA_VAULT_DIR:  Path = _PROJECT_ROOT / "data_vault"
    DB_PATH:         Path = _PROJECT_ROOT / "storage" / "neural_ledger.db"
    QDRANT_PATH:     Path = _PROJECT_ROOT / "storage" / "qdrant_store"
    LOG_DIR:         Path = _PROJECT_ROOT / "logs"

    # ----------------------------------------------------------------
    # Database
    # ----------------------------------------------------------------
    DB_MEMORY_LIMIT:     str = os.getenv("DB_MEMORY_LIMIT", "512MB")
    DB_THREADS:          int = int(os.getenv("DB_THREADS", "4"))
    QUALITY_GATE_MIN:    float = float(os.getenv("QUALITY_GATE_MIN", "0.7"))

    # ----------------------------------------------------------------
    # LLM — Groq
    # ----------------------------------------------------------------
    GROQ_API_KEY:        str = os.getenv("GROQ_API_KEY", "")
    GROQ_MODEL:          str = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
    GROQ_MAX_TOKENS:     int = int(os.getenv("GROQ_MAX_TOKENS", "2048"))
    GROQ_TEMPERATURE:    float = float(os.getenv("GROQ_TEMPERATURE", "0.1"))

    # ----------------------------------------------------------------
    # Embeddings — FastEmbed
    # ----------------------------------------------------------------
    EMBED_MODEL:         str = os.getenv("EMBED_MODEL", "BAAI/bge-small-en-v1.5")
    EMBED_BATCH_SIZE:    int = int(os.getenv("EMBED_BATCH_SIZE", "64"))

    # ----------------------------------------------------------------
    # Qdrant
    # ----------------------------------------------------------------
    QDRANT_HOST:         str = os.getenv("QDRANT_HOST", "")      # empty = local mode
    QDRANT_PORT:         int = int(os.getenv("QDRANT_PORT", "6333"))
    QDRANT_API_KEY:      str = os.getenv("QDRANT_API_KEY", "")

    # ----------------------------------------------------------------
    # API
    # ----------------------------------------------------------------
    API_HOST:            str = os.getenv("API_HOST", "127.0.0.1")
    API_PORT:            int = int(os.getenv("API_PORT", "8000"))
    API_RELOAD:          bool = os.getenv("API_RELOAD", "true").lower() == "true"

    # ----------------------------------------------------------------
    # Localisation
    # ----------------------------------------------------------------
    DEFAULT_CURRENCY:    str = os.getenv("DEFAULT_CURRENCY", "PKR")
    DEFAULT_LANGUAGE:    str = os.getenv("DEFAULT_LANGUAGE", "en")
    TIMEZONE:            str = os.getenv("TIMEZONE", "Asia/Karachi")
    FISCAL_YEAR_START:   str = os.getenv("FISCAL_YEAR_START", "07-01")  # July 1

    # ----------------------------------------------------------------
    # Pipeline behaviour
    # ----------------------------------------------------------------
    WATCHER_DEBOUNCE_MS: int = int(os.getenv("WATCHER_DEBOUNCE_MS", "2000"))
    MAX_FILE_SIZE_MB:    int = int(os.getenv("MAX_FILE_SIZE_MB", "50"))

    def validate(self) -> list[str]:
        """Return list of missing required settings. Empty = all good."""
        warnings = []
        if not self.GROQ_API_KEY:
            warnings.append("GROQ_API_KEY not set — LLM queries will fail")
        return warnings


settings = Settings()
"""Configuration - loads environment variables from .env files."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Try loading .env from multiple locations
_base = Path(__file__).resolve().parent.parent
for env_path in [
    _base / ".env",
    _base.parent / "sourcing-logic" / ".env",
    _base.parent / "spec-matching-v2.5" / ".env",
]:
    if env_path.exists():
        load_dotenv(env_path, override=False)
        break

# Required
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_GENERATIVE_AI_API_KEY") or ""
TM_API_KEY: str = os.getenv("TM_API_KEY", "")
TMAPI_BASE_URL: str = os.getenv("API_1688_BASE_URL", "http://api.tmapi.top")

# Tuning
MAX_CONCURRENT: int = int(os.getenv("MAX_CONCURRENT", "15"))
MAX_TOOL_TURNS: int = int(os.getenv("MAX_TOOL_TURNS", "8"))
USD_TO_CNY_RATE: float = float(os.getenv("USD_TO_CNY_RATE", "7.2"))
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")

# Logging
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO")

def validate():
    """Check required config is present."""
    errors = []
    if not GEMINI_API_KEY:
        errors.append("GEMINI_API_KEY (or GOOGLE_GENERATIVE_AI_API_KEY) not set")
    if not TM_API_KEY:
        errors.append("TM_API_KEY not set")
    if errors:
        raise EnvironmentError(
            "Missing required environment variables:\n  - " + "\n  - ".join(errors)
            + "\n\nSet them in .env or export them."
        )

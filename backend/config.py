"""Runtime configuration loaded from environment variables / .env."""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"

load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    hindsight_api_key: str = os.getenv("HINDSIGHT_API_KEY", "").strip()
    hindsight_base_url: str = os.getenv("HINDSIGHT_BASE_URL", "https://api.hindsight.vectorize.io").strip()
    hindsight_bank_id: str = os.getenv("HINDSIGHT_BANK_ID", "incident-memory").strip()

    groq_api_key: str = os.getenv("GROQ_API_KEY", "").strip()
    groq_model: str = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
    groq_fallback_model: str = os.getenv("GROQ_FALLBACK_MODEL", "qwen/qwen3.8-27b,openai/gpt-oss-20b").strip()

    memory_backend: str = os.getenv("MEMORY_BACKEND", "auto").strip().lower()

    @property
    def use_hindsight(self) -> bool:
        if self.memory_backend == "hindsight":
            return True
        if self.memory_backend == "local":
            return False
        return bool(self.hindsight_api_key)


settings = Settings()

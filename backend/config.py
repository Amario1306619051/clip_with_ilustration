"""Config loader. Reads from environment, with an optional `.env` in the
project root (illustrator/.env). LLM config reuses the SAME var names as the
sibling email_categorizer project so you can drop in the same vLLM endpoint.
"""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Minimal .env loader (no python-dotenv dependency needed). Lines like
    KEY=VALUE; ignores blanks and # comments. Does not override real env vars."""
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()

# ===== vLLM (OpenAI-compatible) — same names as email_categorizer =====
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "https://internal-rnd.balitower.co.id/model")
VLLM_MODEL = os.getenv("VLLM_MODEL", "gb10-qwen35-122b-a10b-fp8-4node-100k")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "dummy")

# ===== Pexels (stock photo search) =====
# Get a free key at https://www.pexels.com/api/
PEXELS_API_KEY = os.getenv("PEXELS_API_KEY", "").strip()

# Default illustration segment length (seconds). One picked image per segment.
DEFAULT_SEGMENT_SECONDS = float(os.getenv("ILLUSTRATOR_SEGMENT_SECONDS", "5"))
# How many candidate images to offer per segment in the UI.
CANDIDATES_PER_SEGMENT = int(os.getenv("ILLUSTRATOR_CANDIDATES", "4"))

"""Whisper STT wrapper — returns flat word-level timestamps.

Env knobs (read from the project-root .env OR the process env):
  WHISPER_MODEL     tiny|base|small|medium|large-v3
                    (default: medium on GPU, base on CPU)
  WHISPER_LANGUAGE  language code e.g. "id"; empty = auto-detect

Runs on CUDA when torch sees a usable GPU. NOTE: the installed torch build must
match the NVIDIA driver's CUDA version or torch silently falls back to CPU (then
a bigger model is painfully slow) — see CLAUDE.md "Whisper / GPU".

On CUDA the model is loaded per call and its VRAM is RELEASED afterwards, so the
render step (NVENC, same GPU) gets the full GPU back; a CUDA out-of-memory error
transparently retries on CPU instead of failing the request.
"""
import gc
import os
from pathlib import Path
from typing import Optional

import torch
import whisper


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the project-root .env into os.environ (no
    override). Self-contained so this module honors WHISPER_* regardless of
    import order or whether the project has a config.py."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


_load_dotenv()

DEVICE = "cuda" if torch.cuda.is_available() else "cpu"
# A bigger model only makes sense when it runs on the GPU; on CPU keep `base`.
DEFAULT_MODEL = os.getenv("WHISPER_MODEL") or ("medium" if DEVICE == "cuda" else "base")
# Empty / unset → auto-detect. Owner content is Indonesian → set WHISPER_LANGUAGE=id.
LANGUAGE = os.getenv("WHISPER_LANGUAGE", "").strip() or None


def _transcribe_on(device: str, name: str, video_path: Path,
                   initial_prompt: Optional[str]) -> dict:
    """Load `name` on `device`, transcribe, then free the model (releasing VRAM
    on CUDA so the render step gets the whole GPU back)."""
    model = whisper.load_model(name, device=device)
    try:
        return model.transcribe(
            str(video_path),
            word_timestamps=True,
            verbose=False,
            language=LANGUAGE,
            initial_prompt=initial_prompt or None,
            condition_on_previous_text=False,  # stop repeat/hallucination loops
            fp16=(device == "cuda"),
        )
    finally:
        del model
        if device == "cuda":
            gc.collect()
            torch.cuda.empty_cache()


def transcribe(video_path: Path, model_name: Optional[str] = None,
               initial_prompt: Optional[str] = None) -> list[dict]:
    """Returns flat list of {word, start, end} dicts.

    `initial_prompt` biases the decoder toward expected vocabulary (proper nouns,
    topic terms) to cut mis-spellings.
    """
    name = model_name or DEFAULT_MODEL
    try:
        result = _transcribe_on(DEVICE, name, video_path, initial_prompt)
    except RuntimeError as e:
        # CUDA OOM (e.g. a render is holding the GPU) → fall back to CPU so the
        # request still succeeds instead of surfacing an opaque 500.
        if DEVICE == "cuda" and "out of memory" in str(e).lower():
            gc.collect()
            torch.cuda.empty_cache()
            result = _transcribe_on("cpu", name, video_path, initial_prompt)
        else:
            raise

    words: list[dict] = []
    for seg in result.get("segments", []):
        for w in seg.get("words", []) or []:
            token = (w.get("word") or "").strip()
            if not token:
                continue
            words.append({
                "word": token,
                "start": float(w.get("start", 0.0)),
                "end": float(w.get("end", 0.0)),
            })
    return words

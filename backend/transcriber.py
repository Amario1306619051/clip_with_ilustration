from functools import lru_cache
from pathlib import Path

import whisper


@lru_cache(maxsize=1)
def get_model(name: str = "base"):
    return whisper.load_model(name)


def transcribe(video_path: Path, model_name: str = "base") -> list[dict]:
    """Returns flat list of {word, start, end} dicts."""
    model = get_model(model_name)
    result = model.transcribe(str(video_path), word_timestamps=True, verbose=False)

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

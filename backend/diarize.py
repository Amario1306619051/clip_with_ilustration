"""Optional speaker diarization (WHO is talking when) via pyannote.audio.

Feeds the windowed shot-director (autobox.run_director) a per-window dominant-
speaker label so it can pick which person is box1 when several people alternate.
FULLY OPTIONAL and LAZY: pyannote is imported only inside diarize_turns(), so a
missing/broken install never touches app boot or the per-frame boxing path; ANY
failure degrades to an empty turn list (the director then decides box1 visually).

Audio is extracted to a mono 16 kHz wav via ffmpeg and read with `soundfile`,
then passed to pyannote as an in-memory waveform — this bypasses torchaudio's
load/info APIs, which the bleeding-edge torchaudio 2.11 build moved to torchcodec.

The model VRAM is freed after every call (like transcriber.py) so the render /
Whisper steps get the GPU back — diarization is serialized in the boxing stage,
never concurrent with an NVENC render.

Setup (owner runs once):
  1. pip install 'pyannote.audio>=4' torchcodec   (already matched to torch 2.11)
  2. Accept the gated-model conditions on HuggingFace (BOTH, the 3.1 pipeline
     loads 3.0 internally):
       https://hf.co/pyannote/speaker-diarization-3.1
       https://hf.co/pyannote/segmentation-3.0
  3. Create a READ token: https://hf.co/settings/tokens
  4. Put HF_TOKEN=hf_xxx in the project-root .env  (empty = diarization off)
"""
import gc
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """Load KEY=VALUE lines from the project-root .env into os.environ (no
    override). Self-contained — clipper has no config.py."""
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()

HF_TOKEN = (os.getenv("HF_TOKEN") or os.getenv("HUGGINGFACE_TOKEN")
            or os.getenv("HUGGINGFACE_HUB_TOKEN") or "").strip()
MODEL = os.getenv("DIARIZE_MODEL", "pyannote/speaker-diarization-3.1").strip()
_DISABLED = os.getenv("DIARIZE_ENABLED", "true").strip().lower() in (
    "0", "false", "no", "off")

_import_state: Optional[bool] = None
_preloaded = False


def _preload_cuda_libs() -> None:
    """pyannote 4.x decodes via torchcodec, whose prebuilt core .so links CUDA
    libs (nvrtc, NPP, cudart) that torch bundles under site-packages/nvidia/*/lib
    but does NOT put on the loader path — so torchcodec fails with
    'libnppicc.so.12 / libnvrtc.so.12: cannot open shared object file'. Preload
    those libs RTLD_GLOBAL here (before any torchcodec load) so the core .so finds
    them in-process, no LD_LIBRARY_PATH needed. Best-effort + idempotent."""
    global _preloaded
    if _preloaded:
        return
    _preloaded = True
    try:
        import ctypes
        import glob
        import nvidia
        base = os.path.dirname(nvidia.__file__)
        for pat in ("*/lib/libnvrtc.so.12", "*/lib/libnpp*.so.12",
                    "*/lib/libcudart.so.12", "*/lib/libcupti.so.12"):
            for so in glob.glob(os.path.join(base, pat)):
                try:
                    ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass
    except Exception as e:  # noqa: BLE001 — best-effort; torchcodec may still find libs
        log.debug("CUDA lib preload skipped: %s", e)


def _import_ok() -> bool:
    """Lazily probe that pyannote.audio imports. Cached. NEVER called at module
    import time, so a broken pyannote/torchaudio can't break app boot or the
    per-frame boxing path."""
    global _import_state
    if _import_state is None:
        try:
            _preload_cuda_libs()
            import pyannote.audio  # noqa: F401
            _import_state = True
        except Exception as e:  # noqa: BLE001
            log.warning("pyannote.audio import failed — diarization off: %s", e)
            _import_state = False
    return _import_state


def enabled() -> bool:
    """True only when a HF token is set, pyannote imports, and DIARIZE_ENABLED."""
    return bool(HF_TOKEN) and not _DISABLED and _import_ok()


def diarize_turns(video_path, num_speakers: Optional[int] = None) -> list:
    """Return [{speaker, start, end}, ...] over the whole clip, or [] on ANY
    failure (graceful). The model is loaded per call and its VRAM freed after, so
    the render/Whisper steps get the GPU back."""
    if not enabled():
        return []
    tmp = None
    pipe = None
    torch = None
    try:
        from pyannote.audio import Pipeline
        import soundfile as sf
        import torch as _torch
        torch = _torch

        fd, tmp = tempfile.mkstemp(suffix=".wav", prefix="diar_")
        os.close(fd)
        subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(video_path),
             "-ac", "1", "-ar", "16000", tmp],
            check=True, capture_output=True)
        data, sr = sf.read(tmp, dtype="float32", always_2d=True)   # (time, ch)
        wav = torch.from_numpy(data.T)                             # (ch, time)

        pipe = Pipeline.from_pretrained(MODEL, token=HF_TOKEN)
        if pipe is None:
            raise RuntimeError(
                f"could not load diarization pipeline '{MODEL}' — accept the gated "
                "model conditions on HuggingFace and check HF_TOKEN")
        if torch.cuda.is_available():
            try:
                pipe.to(torch.device("cuda"))
            except Exception as e:  # noqa: BLE001 — CPU still works
                log.warning("diarization .to(cuda) failed, using CPU: %s", e)

        kw = {"num_speakers": int(num_speakers)} if num_speakers else {}
        try:
            diar = pipe({"waveform": wav, "sample_rate": sr}, **kw)
        except RuntimeError as e:
            if torch.cuda.is_available() and "out of memory" in str(e).lower():
                gc.collect()
                torch.cuda.empty_cache()
                pipe.to(torch.device("cpu"))
                diar = pipe({"waveform": wav, "sample_rate": sr}, **kw)
            else:
                raise

        # pyannote 4.x returns a DiarizeOutput wrapper whose Annotation is on
        # .speaker_diarization; getattr falls through to a legacy plain Annotation.
        ann = getattr(diar, "speaker_diarization", diar)
        return [{"speaker": str(spk), "start": float(seg.start), "end": float(seg.end)}
                for seg, _, spk in ann.itertracks(yield_label=True)]
    except Exception as e:  # noqa: BLE001 — diarization is best-effort
        log.warning("diarize_turns failed: %s", e)
        return []
    finally:
        try:
            del pipe
            gc.collect()
            if torch is not None and torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001
            pass
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def dominant_speaker(turns: list, t0: float, t1: float) -> Optional[str]:
    """The speaker label with the most overlap in [t0, t1], or None. Pure-python
    (no pyannote import) — autobox.run_director calls this per window."""
    if not turns:
        return None
    totals: dict = {}
    for tn in turns:
        try:
            s, e, spk = float(tn["start"]), float(tn["end"]), str(tn["speaker"])
        except (KeyError, TypeError, ValueError):
            continue
        ov = min(t1, e) - max(t0, s)
        if ov > 0:
            totals[spk] = totals.get(spk, 0.0) + ov
    if not totals:
        return None
    return max(totals, key=totals.get)

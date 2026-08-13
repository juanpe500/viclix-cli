"""viclix listen — hands-free voice dictation with spoken confirmation.

The reply half of `viclix say`. Flow:

  1. beep (through the default output device)
  2. listen on the mic; WebRTC-VAD cuts speech into utterances, faster-whisper
     transcribes each (CUDA float16 if available, else CPU int8)
  3. keep accumulating until you say a STOP word ("send" / "puto" by default),
     which ends capture and is stripped from the text
  4. a voice reads it back: "Did you say: <text>?"
  5. you answer yes / no:
       • yes            → confirmed; the text is printed to stdout AND copied to
                          the clipboard
       • no / silence   → it says "text copied to clipboard", copies it, and
                          finishes so you can edit it manually later

No window, no UI — everything happens in the background.

The STT stack (faster-whisper, webrtcvad, sounddevice, numpy, pyperclip) is an
OPTIONAL extra:  pip install "viclix[voice]"
GPU acceleration additionally needs:  pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
"""
from __future__ import annotations

import os
import sys
import time
import queue
import unicodedata

from ..console import logger

# WebRTC-VAD constraints (mirrors the tts-hotkey voice listener).
VAD_RATE = 16000       # 8/16/32/48 kHz only
FRAME_MS = 30          # 10/20/30 ms frames
FRAME_LEN = VAD_RATE * FRAME_MS // 1000
SILENCE_MS = 600       # trailing silence that ends one utterance
MIN_UTTER_MS = 250     # ignore blips shorter than this

DEFAULT_STOP_WORDS = ("send", "puto")
DEFAULT_MODEL = "small"

# Confirmation vocabulary (accent/case-insensitive, matched whole-word).
_YES = {"yes", "yeah", "yep", "yup", "sure", "ok", "okay", "okey", "correct",
        "si", "sip", "claro", "correcto", "exacto", "dale", "perfecto", "perfect"}
_NO = {"no", "nope", "nah", "negative", "negativo", "incorrecto", "mal", "cancel", "cancela"}

_MISSING_HINT = (
    "'viclix listen' needs the voice extra. Install it with:\n"
    "    pip install \"viclix[voice]\"\n"
    "  (faster-whisper, webrtcvad, sounddevice, numpy, pyperclip)\n"
    "  For GPU also: pip install nvidia-cublas-cu12 nvidia-cudnn-cu12"
)


def _import_stt():
    """Lazily import the STT stack; explain how to get it if absent."""
    try:
        import numpy as np
        import sounddevice as sd
        import webrtcvad
        return np, sd, webrtcvad
    except ImportError as e:
        logger.error(_MISSING_HINT)
        logger.debug(f"listen import error: {e}")
        sys.exit(1)


# ── CUDA DLLs + model load (ported from tts-hotkey voice.py) ────────────────

def _register_cuda_dlls() -> None:
    """Put the pip-installed NVIDIA CUDA runtime DLLs (cuBLAS/cuDNN) on the DLL
    search path so ctranslate2 can load them for GPU inference."""
    from pathlib import Path
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for bindir in nvidia.glob("*/bin"):
            try:
                os.add_dll_directory(str(bindir))
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = str(bindir) + os.pathsep + os.environ.get("PATH", "")


def _load_model(size: str, np):
    """Load faster-whisper, preferring CUDA (float16), falling back to CPU (int8)."""
    _register_cuda_dlls()
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error(_MISSING_HINT)
        sys.exit(1)
    warmup = np.zeros(VAD_RATE, dtype=np.float32)
    try:
        m = WhisperModel(size, device="cuda", compute_type="float16")
        list(m.transcribe(warmup, beam_size=1)[0])  # force CUDA kernels to compile
        logger.info(f"🧠 whisper '{size}' on CUDA (float16)")
        return m
    except Exception as e:  # noqa: BLE001
        logger.warning(f"CUDA path failed ({e}) — falling back to CPU")
        m = WhisperModel(size, device="cpu", compute_type="int8")
        list(m.transcribe(warmup, beam_size=1)[0])
        logger.info(f"🧠 whisper '{size}' on CPU (int8)")
        return m


# ── text helpers ────────────────────────────────────────────────────────────

def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").lower())
    text = "".join(c for c in text if unicodedata.category(c) != "Mn")  # drop accents
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def _split_on_stopword(text: str, stop_words):
    """Return (text_before_first_stopword, hit?). The stop word (and anything
    after it) is dropped."""
    out = []
    for w in text.split():
        if _norm(w) in stop_words:
            return " ".join(out), True
        out.append(w)
    return " ".join(out), False


def _is_yes(text: str) -> bool:
    return any(w in _YES for w in _norm(text).split())


def _is_no(text: str) -> bool:
    return any(w in _NO for w in _norm(text).split())


def _copy_clipboard(text: str) -> bool:
    """Copy to the OS clipboard. pyperclip if present, else Windows `clip`."""
    try:
        import pyperclip
        pyperclip.copy(text)
        return True
    except Exception:  # noqa: BLE001
        try:
            import subprocess
            p = subprocess.Popen(["clip"], stdin=subprocess.PIPE, shell=True)
            p.communicate(input=text.encode("utf-16-le"))
            return p.returncode == 0
        except Exception:  # noqa: BLE001
            return False


# ── audio in ────────────────────────────────────────────────────────────────

def _beep(sd, np, freq: int = 880, ms: int = 160, sr: int = 24000, vol: float = 0.3) -> None:
    """A short sine ping through the default output device (fade in/out = no click)."""
    n = int(sr * ms / 1000)
    t = np.arange(n) / sr
    tone = (vol * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    fade = max(1, int(sr * 0.01))
    env = np.ones(n, dtype=np.float32)
    env[:fade] = np.linspace(0, 1, fade)
    env[-fade:] = np.linspace(1, 0, fade)
    tone = (tone * env).reshape(-1, 1)
    try:
        sd.play(tone, sr, device=None)
        sd.wait()
    except Exception as e:  # noqa: BLE001
        logger.debug(f"beep failed: {e}")


def _transcribe(model, np, frames, language):
    pcm = np.frombuffer(b"".join(frames), dtype=np.int16).astype(np.float32) / 32768.0
    try:
        segments, _ = model.transcribe(
            pcm, language=language, beam_size=1, vad_filter=False, no_speech_threshold=0.5,
        )
        return " ".join(s.text for s in segments).strip()
    except Exception as e:  # noqa: BLE001
        logger.error(f"whisper transcribe failed: {e}")
        return ""


def _capture(model, np, sd, webrtcvad, *, language, stop_words=None,
             max_seconds=45.0, idle_seconds=6.0, single=False):
    """Open the mic and VAD-cut utterances, transcribing each.

    If ``single`` — return the first utterance's text (for yes/no).
    Else accumulate until a stop word is heard (or idle/max timeout), returning
    the joined text with the stop word stripped."""
    vad = webrtcvad.Vad(2)
    q: "queue.Queue[bytes]" = queue.Queue()

    def cb(indata, frames, time_info, status):  # noqa: ANN001
        q.put(bytes(indata))

    parts = []
    voiced = []
    silence_ms = 0
    triggered = False
    started = time.time()
    last_activity = time.time()

    try:
        stream = sd.RawInputStream(samplerate=VAD_RATE, blocksize=FRAME_LEN, dtype="int16",
                                   channels=1, device=None, callback=cb)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Could not open the microphone: {e}")
        return "" if single else ""

    with stream:
        while True:
            now = time.time()
            if now - started > max_seconds:
                break
            # Give up after a stretch of quiet once we already have something.
            if not triggered and (parts or single) and now - last_activity > idle_seconds:
                break
            try:
                frame = q.get(timeout=0.2)
            except queue.Empty:
                continue
            if len(frame) != FRAME_LEN * 2:
                continue
            try:
                speech = vad.is_speech(frame, VAD_RATE)
            except Exception:  # noqa: BLE001
                speech = False

            if speech:
                if not triggered:
                    triggered = True
                    voiced = []
                voiced.append(frame)
                silence_ms = 0
                last_activity = now
            elif triggered:
                voiced.append(frame)
                silence_ms += FRAME_MS
                if silence_ms >= SILENCE_MS:
                    dur_ms = len(voiced) * FRAME_MS
                    text = _transcribe(model, np, voiced, language) if dur_ms >= MIN_UTTER_MS else ""
                    voiced, triggered, silence_ms = [], False, 0
                    last_activity = time.time()
                    if not text:
                        continue
                    logger.info(f"🎧 heard: {text!r}")
                    if single:
                        return text
                    cleaned, hit = _split_on_stopword(text, stop_words or ())
                    if cleaned:
                        parts.append(cleaned)
                    if hit:
                        break
    return " ".join(parts).strip()


# ── orchestration ───────────────────────────────────────────────────────────

def run_listen(*, stop_words=None, model_size=None, language=None,
               confirm=True, voice=None, rate=None) -> dict:
    """Beep, dictate until a stop word, confirm, then copy to clipboard + print.

    Returns {"text": str, "confirmed": bool}. Also PRINTS the final text to
    stdout (so a caller can capture your spoken reply) and copies it."""
    np, sd, webrtcvad = _import_stt()

    stops = tuple(_norm(w) for w in (
        stop_words.split(",") if isinstance(stop_words, str) else (stop_words or DEFAULT_STOP_WORDS)
    ) if _norm(w))
    size = (model_size or DEFAULT_MODEL).strip()

    logger.info(f"Loading whisper '{size}' … (stop words: {', '.join(stops)})")
    model = _load_model(size, np)

    from . import say  # spoken feedback reuses the say pipeline

    _beep(sd, np)
    logger.info("🎙  Listening — speak, then say a stop word to finish.")
    text = _capture(model, np, sd, webrtcvad, language=language, stop_words=stops)

    if not text:
        logger.info("Heard nothing.")
        try:
            say.speak("I didn't catch anything.", quiet=True)
        except Exception:  # noqa: BLE001
            pass
        print("", end="")
        return {"text": "", "confirmed": False}

    confirmed = False
    if confirm:
        try:
            say.speak(f"Did you say: {text}?", quiet=True)
        except Exception as e:  # noqa: BLE001
            logger.debug(f"confirm speak failed: {e}")
        _beep(sd, np, freq=660, ms=120)
        answer = _capture(model, np, sd, webrtcvad, language=language,
                          single=True, max_seconds=6.0, idle_seconds=3.0)
        # yes → confirmed; no / silence / anything-else → not confirmed (safe).
        confirmed = _is_yes(answer) and not _is_no(answer)

    _copy_clipboard(text)
    if confirmed:
        logger.info(f"✅ confirmed: {text!r}")
    else:
        try:
            say.speak("Text copied to clipboard.", quiet=True)
        except Exception:  # noqa: BLE001
            pass
        logger.info(f"📋 copied to clipboard for manual edit: {text!r}")

    # The transcript on stdout is the machine-readable result; status on stderr.
    print(text)
    print(f"# {'confirmed' if confirmed else 'copied-for-edit'}", file=sys.stderr)
    return {"text": text, "confirmed": confirmed}


def cmd_listen(args) -> None:
    """`viclix listen` — dictate a message by voice, confirm, copy to clipboard."""
    try:
        run_listen(
            stop_words=getattr(args, 'stop', None),
            model_size=getattr(args, 'model', None),
            language=(getattr(args, 'lang', None) or None),
            confirm=not getattr(args, 'no_confirm', False),
            voice=getattr(args, 'voice', None),
            rate=getattr(args, 'rate', None),
        )
    except KeyboardInterrupt:
        logger.info("interrupted")
        sys.exit(1)

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
import threading
import unicodedata

from ..console import logger

# WebRTC-VAD constraints (mirrors the tts-hotkey voice listener).
VAD_RATE = 16000       # 8/16/32/48 kHz only
FRAME_MS = 30          # 10/20/30 ms frames
FRAME_LEN = VAD_RATE * FRAME_MS // 1000
SILENCE_MS = 600       # trailing silence that ends one utterance
MIN_UTTER_MS = 250     # ignore blips shorter than this
PARTIAL_EVERY_FRAMES = 18   # ~0.5s of new speech between live partial updates

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


def _cudnn_present() -> bool:
    """Is a cuDNN ops DLL that ctranslate2 needs actually on the DLL path?

    ctranslate2's CUDA backend HARD-ABORTS the process (not a catchable Python
    exception) when cuDNN is missing — as JP hit: "Could not locate
    cudnn_ops64_9.dll". So in auto mode we must not even attempt CUDA unless the
    DLL is present. `_register_cuda_dlls()` must run first so the pip-wheel bin
    dirs are searched."""
    import glob
    from pathlib import Path
    dirs = []
    for base in map(Path, sys.path):
        nv = base / "nvidia"
        if nv.is_dir():
            dirs += [str(p) for p in nv.glob("*/bin")]
    dirs += [d for d in os.environ.get("PATH", "").split(os.pathsep) if d]
    for d in dirs:
        # v9 merged naming (cudnn_ops64_9.dll) or the base runtime dll.
        if glob.glob(os.path.join(d, "cudnn_ops64_*.dll")) or glob.glob(os.path.join(d, "cudnn64_*.dll")):
            return True
    return False


def _load_model(size: str, np, device: str = "auto"):
    """Load faster-whisper. device: auto (CUDA if cuDNN present, else CPU) | cuda | cpu."""
    _register_cuda_dlls()
    # Quiet faster-whisper's per-call "Processing audio with duration…" INFO logs;
    # with live partials it would otherwise print on every update.
    import logging as _logging
    _logging.getLogger("faster_whisper").setLevel(_logging.WARNING)
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        logger.error(_MISSING_HINT)
        sys.exit(1)
    warmup = np.zeros(VAD_RATE, dtype=np.float32)

    want_cuda = device in ("auto", "cuda")
    if want_cuda and device == "auto" and not _cudnn_present():
        logger.warning("CUDA cuDNN not found — using CPU. For GPU speed install the wheels:")
        logger.warning('    pip install "nvidia-cudnn-cu12>=9" "nvidia-cublas-cu12>=12.4"')
        want_cuda = False

    if want_cuda:
        try:
            m = WhisperModel(size, device="cuda", compute_type="float16")
            list(m.transcribe(warmup, beam_size=1)[0])  # force CUDA kernels to compile
            logger.info(f"🧠 whisper '{size}' on CUDA (float16)")
            return m
        except Exception as e:  # noqa: BLE001
            if device == "cuda":
                logger.error(f"CUDA was requested but failed to init: {e}")
                logger.error('Install/repair the GPU wheels: pip install "nvidia-cudnn-cu12>=9" "nvidia-cublas-cu12>=12.4"')
                sys.exit(1)
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

def _beep(sd, np, freq: int = 1000, ms: int = 220, sr: int = 48000, vol: float = 0.45) -> None:
    """An audible ping through the default output device. 48 kHz (what most default
    devices actually run at) + a winsound fallback so it's never silent."""
    try:
        n = int(sr * ms / 1000)
        t = np.arange(n) / sr
        tone = (vol * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        fade = max(1, int(sr * 0.01))
        env = np.ones(n, dtype=np.float32)
        env[:fade] = np.linspace(0, 1, fade)
        env[-fade:] = np.linspace(1, 0, fade)
        sd.play((tone * env).reshape(-1, 1), sr, device=None)
        sd.wait()
        return
    except Exception as e:  # noqa: BLE001
        logger.debug(f"sounddevice beep failed ({e}) — trying winsound")
    try:
        import winsound
        winsound.Beep(int(freq), int(ms))
    except Exception as e:  # noqa: BLE001
        logger.debug(f"winsound beep failed: {e}")


class _LiveLine:
    """A single self-overwriting terminal line (on stderr) for live partials, so
    it never pollutes the final transcript printed to stdout."""

    def __init__(self, enabled=True):
        self.enabled = enabled
        self._prev = 0

    def show(self, msg: str) -> None:
        if not self.enabled:
            return
        pad = " " * max(0, self._prev - len(msg))
        sys.stderr.write("\r" + msg + pad)
        sys.stderr.flush()
        self._prev = len(msg)

    def clear(self) -> None:
        if self.enabled and self._prev:
            sys.stderr.write("\r" + " " * self._prev + "\r")
            sys.stderr.flush()
        self._prev = 0


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
             max_seconds=45.0, idle_seconds=6.0, single=False, live=True):
    """Open the mic and VAD-cut utterances, transcribing each.

    Shows a self-updating live line with the partial transcription WHILE you
    speak (throttled, off a background thread so capture never stalls), then the
    finalized utterance + its transcription latency (handy to compare CPU vs GPU).

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

    liner = _LiveLine(enabled=live)
    partial_busy = threading.Event()
    last_partial_len = 0

    def _partial(frames_snapshot):
        try:
            txt = _transcribe(model, np, frames_snapshot, language)
            if txt:
                liner.show(f"🎙 … {txt}")
        finally:
            partial_busy.clear()

    try:
        stream = sd.RawInputStream(samplerate=VAD_RATE, blocksize=FRAME_LEN, dtype="int16",
                                   channels=1, device=None, callback=cb)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Could not open the microphone: {e}")
        return ""

    with stream:
        liner.show("🎙 listening…")
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
                    last_partial_len = 0
                voiced.append(frame)
                silence_ms = 0
                last_activity = now
                # Live partial: re-transcribe the growing utterance, throttled,
                # one at a time (keeps the whisper model single-threaded).
                if (len(voiced) - last_partial_len) >= PARTIAL_EVERY_FRAMES and not partial_busy.is_set():
                    last_partial_len = len(voiced)
                    partial_busy.set()
                    threading.Thread(target=_partial, args=(list(voiced),), daemon=True).start()
            elif triggered:
                voiced.append(frame)
                silence_ms += FRAME_MS
                if silence_ms >= SILENCE_MS:
                    while partial_busy.is_set():   # don't run two transcribes at once
                        time.sleep(0.02)
                    liner.clear()
                    dur_ms = len(voiced) * FRAME_MS
                    t0 = time.time()
                    text = _transcribe(model, np, voiced, language) if dur_ms >= MIN_UTTER_MS else ""
                    lat = time.time() - t0
                    voiced, triggered, silence_ms = [], False, 0
                    last_activity = time.time()
                    if not text:
                        liner.show("🎙 listening…")
                        continue
                    logger.info(f"🎧 heard ({dur_ms/1000:.1f}s audio → {lat:.1f}s): {text!r}")
                    if single:
                        return text
                    cleaned, hit = _split_on_stopword(text, stop_words or ())
                    if cleaned:
                        parts.append(cleaned)
                    if parts:
                        sys.stderr.write(f"   ↳ so far: {' '.join(parts)}\n")
                        sys.stderr.flush()
                    if hit:
                        break
                    liner.show("🎙 listening…")
    liner.clear()
    return " ".join(parts).strip()


# ── orchestration ───────────────────────────────────────────────────────────

def run_listen(*, stop_words=None, model_size=None, language=None,
               confirm=True, voice=None, rate=None, device="auto") -> dict:
    """Beep, dictate until a stop word, confirm, then copy to clipboard + print.

    Returns {"text": str, "confirmed": bool}. Also PRINTS the final text to
    stdout (so a caller can capture your spoken reply) and copies it."""
    np, sd, webrtcvad = _import_stt()

    stops = tuple(_norm(w) for w in (
        stop_words.split(",") if isinstance(stop_words, str) else (stop_words or DEFAULT_STOP_WORDS)
    ) if _norm(w))
    size = (model_size or DEFAULT_MODEL).strip()

    logger.info(f"Loading whisper '{size}' … (stop words: {', '.join(stops)})")
    model = _load_model(size, np, (device or "auto").strip().lower())

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
            device=getattr(args, 'device', None) or 'auto',
        )
    except KeyboardInterrupt:
        logger.info("interrupted")
        sys.exit(1)

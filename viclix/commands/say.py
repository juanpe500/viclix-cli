"""viclix say — speak text aloud via Edge TTS, streamed sentence-by-sentence.

Ported from the tts-hotkey "lite" daemon (its run.bat), minus the hotkey,
clipboard, caption and seek machinery. Two deliberate changes from the source:

  * No device menu. It always plays through the CURRENT default output device
    (whatever the OS has selected) — ``sounddevice`` with ``device=None`` follows
    it. No fighting to pick one.
  * Sentence-aligned streaming. The text is split into chunks — a short first
    one so audio starts almost immediately, then larger batches — and each chunk
    plays while the next is still being synthesized. A long summary never blocks
    on one big generation.

The audio stack (edge-tts / sounddevice / miniaudio / numpy) is an OPTIONAL
extra so a normal ``pip install viclix`` stays lean. Enable it with:

    pip install "viclix[say]"

Usage:
    viclix say "Done — deployed dashboard and cp, all green."
    viclix say --lang en "Build finished."
    viclix say --voice es-ES-AlvaroNeural "Listo, todo desplegado."
    echo "piped text" | viclix say
"""
from __future__ import annotations

import os
import re
import sys
import time
import hashlib
import asyncio
import threading
import collections

from ..console import logger, config_home

# edge-tts default stream is 24 kHz mono mp3 — decode straight to that.
SAMPLE_RATE = 24000
CHANNELS = 1

# Gentle speed-up; the source daemon used +20%. Override with --rate (""=normal).
DEFAULT_RATE = "+10%"

# "mix" is a Multilingual voice: it reads Spanish and English in one utterance
# with the right accent for each, so a Spanglish summary sounds natural.
VOICES = {
    "es": "es-ES-AlvaroNeural",
    "en": "en-US-ChristopherNeural",
    "mix": "en-US-BrianMultilingualNeural",
}
DEFAULT_LANG = "mix"

# First chunk short so audio starts ASAP; later chunks batched (fewer round-trips).
FIRST_CHUNK_MAX = 120
REST_CHUNK_MAX = 280

_MISSING_HINT = (
    "'viclix say' needs the audio extra. Install it with:\n"
    "    pip install \"viclix[say]\"\n"
    "  (pulls edge-tts, sounddevice, miniaudio, numpy)"
)


def _import_audio():
    """Lazily import the heavy audio stack; explain how to get it if absent."""
    try:
        import edge_tts
        import miniaudio
        import numpy as np
        import sounddevice as sd
        return edge_tts, miniaudio, np, sd
    except ImportError as e:
        logger.error(_MISSING_HINT)
        logger.debug(f"say import error: {e}")
        sys.exit(1)


# ── text prep (trimmed from the source's clipboard cleaner) ─────────────────

_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002190-\U000021FF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F"
    "\U0001F000-\U0001F0FF\U0000200D\U000020E3]+",
    flags=re.UNICODE,
)
_PATH_RE = re.compile(r"\S*[\\/]\S*")
_SYMBOLS_TO_SPACE = str.maketrans({c: " " for c in "!#^*()_"})
_SYMBOL_WORDS = {
    "~": " around ", "%": " percent ", "$": " dollars ", "@": " at ",
    "&": " and ", "=": " equals ", "+": " plus ",
}
_DASH_OP_RE = re.compile(r"(?<=\s)-(?=\s)")
_FILE_EXTS = (
    "py|js|ts|tsx|jsx|json|md|txt|html?|css|scss|csv|tsv|pdf|png|jpe?g|gif|svg|webp|ico|"
    "sh|ps1|bat|cmd|ya?ml|toml|ini|cfg|conf|xml|sql|java|c|cpp|cc|h|hpp|go|rs|rb|php|"
    "exe|dll|zip|tar|gz|rar|7z|log|lock|mp3|mp4|wav|mov|avi|mkv|docx?|xlsx?|pptx?|"
    "env|db|sqlite|whl|so|bin|dat"
)
_FILE_RE = re.compile(rf"(\w[\w-]*)?\.({_FILE_EXTS})\b", re.IGNORECASE)


def _basename(token: str) -> str:
    parts = re.split(r"[\\/]+", token.rstrip("\\/"))
    return parts[-1] if parts and parts[-1] else token


def _spell_filename(m: "re.Match") -> str:
    stem = m.group(1)
    return f"{stem} file" if stem else "file"


def _clean(raw: str) -> str:
    """Make text read cleanly aloud: drop emoji, shorten paths, spell out the
    symbols that carry meaning, collapse the rest to spaces."""
    text = _EMOJI_RE.sub("", raw)
    text = _PATH_RE.sub(lambda m: _basename(m.group(0)), text)
    text = _FILE_RE.sub(_spell_filename, text)
    for sym, word in _SYMBOL_WORDS.items():
        text = text.replace(sym, word)
    text = _DASH_OP_RE.sub(" minus ", text)
    text = text.translate(_SYMBOLS_TO_SPACE)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def _segment(text: str) -> list:
    """Split into sentences (paragraph then sentence-terminator aware)."""
    out = []
    for pm in re.finditer(r"\S.*?(?=\n\s*\n|\Z)", text, re.S):
        for sm in re.finditer(r"\S.*?(?:[.!?…]+|\Z)", pm.group(0), re.S):
            s = sm.group(0).strip()
            if s:
                out.append(s)
    if not out and text.strip():
        out.append(text.strip())
    return out


def _chunk_text(text: str) -> list:
    """Sentence-aligned chunks: a short first one (fast start), then batches."""
    sents = _segment(text)
    if not sents:
        return []
    chunks, cur, limit = [], "", FIRST_CHUNK_MAX
    for s in sents:
        if cur and len(cur) + 1 + len(s) > limit:
            chunks.append(cur)
            cur, limit = s, REST_CHUNK_MAX
        else:
            cur = f"{cur} {s}".strip()
    if cur:
        chunks.append(cur)
    return chunks


# ── synthesis + tiny cache ──────────────────────────────────────────────────

def _cache_dir() -> str:
    d = os.path.join(config_home(), "say-cache")
    os.makedirs(d, exist_ok=True)
    return d


async def _synth(edge_tts, text: str, voice: str, rate: str) -> bytes:
    comm = edge_tts.Communicate(text, voice, **({"rate": rate} if rate else {}))
    audio = bytearray()
    async for ch in comm.stream():
        if ch["type"] == "audio":
            audio.extend(ch["data"])
    return bytes(audio)


def _get_mp3(edge_tts, text: str, voice: str, rate: str):
    """Return a path to an mp3 for text+voice+rate, generating + caching if new."""
    key = hashlib.md5(f"{voice}::{rate}::{text}".encode("utf-8")).hexdigest()
    path = os.path.join(_cache_dir(), key + ".mp3")
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    try:
        audio = asyncio.run(_synth(edge_tts, text, voice, rate))
    except Exception as e:  # noqa: BLE001
        logger.error(f"edge-tts failed: {e}")
        return None
    if not audio:
        return None
    with open(path, "wb") as f:
        f.write(audio)
    return path


def _decode(miniaudio, np, path: str):
    dec = miniaudio.decode(
        open(path, "rb").read(),
        nchannels=CHANNELS, sample_rate=SAMPLE_RATE,
        output_format=miniaudio.SampleFormat.FLOAT32,
    )
    return np.asarray(dec.samples, dtype=np.float32).reshape(-1, CHANNELS)


# ── streaming player (default output device) ────────────────────────────────

class _StreamingPlayer:
    """Gapless playback of PCM chunks fed in over time, on the default device."""

    def __init__(self, sd, np) -> None:
        self._sd = sd
        self._queue = collections.deque()
        self._lock = threading.Lock()
        self._cur = None
        self._pos = 0
        self._done = False
        self._stream = None

    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        filled = 0
        with self._lock:
            while filled < frames:
                if self._cur is None or self._pos >= len(self._cur):
                    if self._queue:
                        self._cur = self._queue.popleft()
                        self._pos = 0
                    else:
                        break
                take = min(frames - filled, len(self._cur) - self._pos)
                outdata[filled:filled + take] = self._cur[self._pos:self._pos + take]
                self._pos += take
                filled += take
        if filled < frames:
            outdata[filled:].fill(0)

    def _ensure_stream(self) -> None:
        if self._stream is None:
            # device=None → PortAudio's current default output (the selected one).
            self._stream = self._sd.OutputStream(
                samplerate=SAMPLE_RATE, channels=CHANNELS, dtype="float32",
                device=None, callback=self._callback, blocksize=1024,
            )
            self._stream.start()

    def submit(self, pcm) -> None:
        with self._lock:
            self._queue.append(pcm)
        self._ensure_stream()

    def mark_done(self) -> None:
        self._done = True

    def _idle(self) -> bool:
        with self._lock:
            return not self._queue and (self._cur is None or self._pos >= len(self._cur))

    def wait(self) -> None:
        """Block until every submitted chunk has finished playing."""
        while not (self._done and self._idle()):
            time.sleep(0.05)
        time.sleep(0.25)  # small tail so the last block flushes through PortAudio

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001
                pass
            self._stream = None


# ── command ─────────────────────────────────────────────────────────────────

def _resolve_text(args) -> str:
    parts = [p for p in (getattr(args, 'target', None), getattr(args, 'target_arg', None)) if p]
    if parts:
        return " ".join(parts).strip()
    # Piped input:  echo "..." | viclix say
    try:
        if not sys.stdin.isatty():
            return (sys.stdin.read() or "").strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def run_say_argv(argv) -> None:
    """Parse `viclix say` with a DEDICATED parser so flags may appear before or
    after the text. The shared CLI parser can't backfill a `nargs='?'` positional
    that follows an option, which would make `viclix say --lang en "hi"` fail."""
    import argparse
    ap = argparse.ArgumentParser(
        prog='viclix say',
        description='Speak text aloud via Edge TTS (streamed; default output device).',
    )
    ap.add_argument('--voice', help='exact Edge TTS voice id, e.g. es-ES-AlvaroNeural (overrides --lang)')
    ap.add_argument('--lang', help='es | en | mix  (default: mix — reads both languages)')
    ap.add_argument('--rate', help='speed like "+20%%" or "-10%%"  (""=normal; default +10%%)')
    ap.add_argument('text', nargs='*', help='the text to speak (quote it); omit to read stdin')
    ns = ap.parse_args(list(argv))

    class _Args:
        pass
    a = _Args()
    a.target = ' '.join(ns.text).strip() if ns.text else None
    a.target_arg = None
    a.voice = ns.voice
    a.lang = ns.lang
    a.rate = ns.rate
    cmd_say(a)


def cmd_say(args) -> None:
    """Speak the given text aloud, streamed sentence-by-sentence."""
    text = _resolve_text(args)
    if not text:
        logger.error('Nothing to say. Usage: viclix say "your text here"  (or pipe text in).')
        sys.exit(1)

    edge_tts, miniaudio, np, sd = _import_audio()

    voice = (getattr(args, 'voice', None) or '').strip()
    if not voice:
        lang = (getattr(args, 'lang', None) or DEFAULT_LANG).strip().lower()
        voice = VOICES.get(lang, VOICES[DEFAULT_LANG])
    rate = getattr(args, 'rate', None)
    rate = DEFAULT_RATE if rate is None else rate.strip()

    text = _clean(text)
    chunks = _chunk_text(text)
    if not chunks:
        logger.error("Nothing to say after cleaning the text.")
        sys.exit(1)

    player = _StreamingPlayer(sd, np)
    logger.info(f"🗣  Speaking ({voice}, {len(text)} chars, {len(chunks)} chunk(s))…")
    try:
        # Generate + submit in order; the first chunk starts playing while the
        # rest synthesize (playback runs on PortAudio's callback thread).
        for i, chunk in enumerate(chunks):
            mp3 = _get_mp3(edge_tts, chunk, voice, rate)
            if not mp3:
                logger.warning(f"chunk {i + 1}/{len(chunks)} produced no audio — skipping")
                continue
            player.submit(_decode(miniaudio, np, mp3))
        player.mark_done()
        player.wait()
    except KeyboardInterrupt:
        logger.info("interrupted")
    except Exception as e:  # noqa: BLE001 — PortAudio / device errors
        logger.error(f"Playback failed: {e}")
        logger.debug("Is an output device available? 'say' plays through the OS default.")
        sys.exit(1)
    finally:
        player.close()

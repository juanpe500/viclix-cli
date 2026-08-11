"""Console foundation for the Viclix CLI (colors, logging, prompts, paths).

LEAF module — imports nothing else from the `viclix` package, so it can never
create an import cycle. It also owns the per-user config *paths*
(`config_home` / `CONFIG_PATH` / `LOGS_DIR`) because the logger's FileHandler
needs `LOGS_DIR` at import time and `config.load_config` needs `logger` — keeping
the paths here breaks that would-be cycle.

MIGRATION (paste from cli.py, in this order — keep module-level blocks where the
executable statements sit, i.e. right after imports, before the functions):

  # COPY cli.py:33-46    console-encoding block (reconfigure stdout/stderr + win ANSI)  [module-level]
  # COPY cli.py:51       DEBUG
  # COPY cli.py:58-61    config_home()
  # COPY cli.py:64-65    CONFIG_PATH, LOGS_DIR
  # COPY cli.py:67       os.makedirs(LOGS_DIR, exist_ok=True)                            [module-level]
  # COPY cli.py:70-76    C_CYAN .. C_BOLD  (ANSI color constants)
  # COPY cli.py:79-91    _color_enabled()
  # COPY cli.py:96-97    color-disable block (if not _color_enabled(): C_* = '')         [module-level]
  # COPY cli.py:101-118  colorize_help()
  # COPY cli.py:121-125  class ColorHelpParser
  # COPY cli.py:128-132  _colorize_json()
  # COPY cli.py:135-137  print_json()
  # COPY cli.py:140-153  ASCII_ART
  # COPY cli.py:156-167  class ColorFormatter
  # COPY cli.py:171-184  logging setup (log_filename, log_path, file_handler,
  #                      stream_handler, basicConfig, logger, logger.setLevel)           [module-level]
  # COPY cli.py:451-459  _interactive()
  # COPY cli.py:462-470  _ask()
  # COPY cli.py:473-480  _confirm()
  # COPY cli.py:483-487  _mask_secret()
  # COPY cli.py:490-496  _mask_url_credentials()
  # COPY cli.py:499-504  _open_url()
  # COPY cli.py:2257-2271  _menu()

After pasting, `python -m py_compile console.py` must pass.
"""
import os
import re
import sys
import json
import logging
import argparse
import webbrowser
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────


# ── Console encoding ────────────────────────────────────────────────────────
# The banner and log glyphs are UTF-8; Windows consoles often default to cp1252
# and would crash on them. Force UTF-8 (and best-effort ANSI) so output is safe
# everywhere. errors='replace' means the worst case is a stray '?', never a crash.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
if os.name == 'nt':
    try:
        import ctypes
        # Enable virtual terminal processing so ANSI color codes render on Win10+.
        ctypes.windll.kernel32.SetConsoleMode(
            ctypes.windll.kernel32.GetStdHandle(-11), 7
        )
    except Exception:
        pass

# ── Debug mode ──────────────────────────────────────────────────────────────
# Flip to False before publishing to PyPI. When True: verbose debug logs (every
# logger.debug call) plus the connection strings injected into the app.
DEBUG = False


def config_home() -> str:
    return os.environ.get("VICLIX_HOME") or os.path.join(
        os.path.expanduser("~"), ".viclix"
    )


CONFIG_PATH = os.path.join(config_home(), "config.json")
LOGS_DIR = os.path.join(config_home(), "logs")

os.makedirs(LOGS_DIR, exist_ok=True)

# ── ANSI Colors ─────────────────────────────────────────────────────────────
C_CYAN = '\033[96m'
C_GREEN = '\033[92m'
C_YELLOW = '\033[93m'
C_RED = '\033[91m'
C_MAGENTA = '\033[95m'
C_RESET = '\033[0m'
C_BOLD = '\033[1m'


def _color_enabled() -> bool:
    """Honor NO_COLOR and only colorize a real terminal.

    Respects the NO_COLOR convention (https://no-color.org), a VICLIX_NO_COLOR
    escape hatch, and — crucially — disables ANSI when stdout is redirected to a
    file or pipe, so logs and greppable output stay clean.
    """
    if os.environ.get('NO_COLOR') or os.environ.get('VICLIX_NO_COLOR'):
        return False
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


# Blank every color code when color is off. Done here — before the log
# formatter, banner and help text are built — so a single switch covers them all.
if not _color_enabled():
    C_CYAN = C_GREEN = C_YELLOW = C_RED = C_MAGENTA = C_RESET = C_BOLD = ''


# ── Color helpers ───────────────────────────────────────────────────────────
def colorize_help(text: str) -> str:
    """Post-colorize argparse's already-formatted help.

    We color the finished string (not the argparse actions) so ANSI codes never
    throw off argparse's column-width math — alignment stays perfect.
    """
    out = []
    for line in text.split('\n'):
        if line.startswith('usage:'):
            line = line.replace('usage:', C_BOLD + C_CYAN + 'usage:' + C_RESET, 1)
        elif re.match(r'^[^\s].*:\s*$', line):  # section headings
            line = C_BOLD + C_CYAN + line + C_RESET
        out.append(line)
    text = '\n'.join(out)
    text = re.sub(r'(?<![\w-])(--?[A-Za-z][\w-]*)', lambda m: C_GREEN + m.group(1) + C_RESET, text)
    text = re.sub(r'(\{[a-z0-9,]+\})', lambda m: C_YELLOW + m.group(1) + C_RESET, text)
    text = re.sub(r'\b([A-Z][A-Z0-9_]{2,})\b', lambda m: C_YELLOW + m.group(1) + C_RESET, text)
    return text


class ColorHelpParser(argparse.ArgumentParser):
    """ArgumentParser whose --help / print_help output is colorized."""

    def format_help(self) -> str:
        return colorize_help(super().format_help())


def _colorize_json(s: str) -> str:
    s = re.sub(r'"([^"]*)":', lambda m: '"' + C_CYAN + m.group(1) + C_RESET + '":', s)
    s = re.sub(r':\s"([^"]*)"', lambda m: ': "' + C_GREEN + m.group(1) + C_RESET + '"', s)
    s = re.sub(r':\s(true|false|null|-?\d+(?:\.\d+)?)', lambda m: ': ' + C_YELLOW + m.group(1) + C_RESET, s)
    return s


def print_json(data) -> None:
    """Pretty-print a JSON-serializable value with light syntax coloring."""
    print(_colorize_json(json.dumps(data, indent=2)))


ASCII_ART = f"""
{C_CYAN}{C_BOLD}
 ██▒   █▓ ██▓ ▄████▄   ██▓    ██▓▒██   ██▒
▓██░   █▒▓██▒▒██▀ ▀█  ▓██▒   ▓██▒▒▒ █ █ ▒░
 ▓██  █▒░▒██▒▒▓█    ▄ ▒██░   ▒██▒░░  █   ░
  ▒██ █░░░██░▒▓▓▄ ▄██▒▒██░   ░██░ ░ █ █ ▒
   ▒▀█░  ░██░▒ ▓███▀ ░░██████░██░▒██▒ ▒██▒
   ░ ▐░  ░▓  ░ ░▒ ▒  ░░ ▒░▓  ░▓  ▒▒ ░ ░▓ ░
   ░ ░░   ▒ ░  ░  ▒   ░ ░ ▒  ░▒ ░░░   ░▒ ░
     ░░   ▒ ░░        ░ ░    ▒ ░ ░    ░
      ░   ░  ░ ░        ░  ░ ░   ░    ░
     ░       ░
"""



class ColorFormatter(logging.Formatter):
    FORMATS = {
        logging.DEBUG: f"{C_CYAN}❖{C_RESET} %(message)s",
        logging.INFO: f"{C_GREEN}✔{C_RESET} %(message)s",
        logging.WARNING: f"{C_YELLOW}⚠{C_RESET} %(message)s",
        logging.ERROR: f"{C_RED}✖{C_RESET} %(message)s",
        logging.CRITICAL: f"{C_RED}{C_BOLD}☢ %(message)s{C_RESET}"
    }

    def format(self, record):
        log_fmt = self.FORMATS.get(record.levelno, "%(message)s")
        return logging.Formatter(log_fmt).format(record)


# Logging configuration
log_filename = f"viclix_cli_{datetime.now().strftime('%Y%m%d')}.log"
log_path = os.path.join(LOGS_DIR, log_filename)

file_handler = logging.FileHandler(log_path, encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(ColorFormatter())

# Root stays at INFO so third-party libs (urllib3, etc.) don't spam debug;
# only our own logger goes verbose when DEBUG is on.
logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler])
logger = logging.getLogger('viclix_cli')
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# ── Interactive / UX helpers (wizard, clean errors, secret masking) ─────────
def _interactive() -> bool:
    """True when we can prompt — both stdin and stdout are a terminal.

    Every input() is gated on this so piped/CI runs fail with a helpful message
    instead of hanging or dying on EOF (B5)."""
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def _ask(prompt, default=None):
    """Prompt with a default; returns `default` on empty input. Raises
    SystemExit on a closed stdin so we never loop forever."""
    try:
        raw = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit(1)
    return raw if raw else default


def _confirm(prompt, default=True):
    """Yes/no prompt. Uses `default` on empty input and when non-interactive."""
    if not _interactive():
        return default
    ans = (_ask(prompt + (" [Y/n]: " if default else " [y/N]: ")) or '').lower()
    if not ans:
        return default
    return ans in ('y', 'yes')


def _mask_secret(s):
    """'ghp_abcd…wxyz' — show a token is present without leaking it."""
    if not s:
        return '••••'
    return '•' * len(s) if len(s) <= 8 else f"{s[:4]}…{s[-4:]}"


def _mask_url_credentials(url):
    """Render a repo URL as https://***@host/... so a stored PAT is visibly
    present rather than looking dropped (I1)."""
    if '://' not in url or '@' not in url:
        return url
    scheme, rest = url.split('://', 1)
    return f"{scheme}://***@{rest.split('@', 1)[1]}"


def _open_url(url):
    """Best-effort open a URL in the browser; never fatal."""
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False

def _menu(title, options):
    """Print a numbered menu; return the chosen 0-based index, or None if the
    user cancelled or typed something invalid."""
    print(f"\n{C_BOLD}{C_CYAN}{title}{C_RESET}")
    for i, opt in enumerate(options, 1):
        print(f"  {C_GREEN}{i}{C_RESET}. {opt}")
    try:
        raw = input(f"{C_CYAN}Choose [1-{len(options)}]: {C_RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if not raw.isdigit():
        return None
    idx = int(raw) - 1
    return idx if 0 <= idx < len(options) else None


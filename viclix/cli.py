#!/usr/bin/env python3
"""Viclix CLI — deploy and manage Viclix projects from your terminal.

Ported from the internal helper script. The only structural changes versus the
original are the three pieces that make it distributable to any user:

  1. Credentials live in a global config (``~/.viclix/config.json``) written by
     ``viclix login`` — nothing is hardcoded, so no secret ships in the wheel.
  2. A console entry point ``viclix`` (see pyproject ``[project.scripts]``).
  3. ``viclix init`` auto-detects an existing git remote and registers it
     without needing a GitHub PAT; it only falls back to creating a repo (which
     does need a PAT) when there is no remote yet.
"""
import os
import re
import sys
import json
import shutil
import socket
import hashlib
import tempfile
import requests
import argparse
import subprocess
import webbrowser
import logging
from datetime import datetime

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

# ── Paths / config ──────────────────────────────────────────────────────────
# Global, per-user config — overridable with VICLIX_HOME for tests/CI.
DEFAULT_BASE_URL = "https://dashboard.viclix.com/api/v1/"


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
CLI                                   {C_YELLOW}By Sizth, LLC{C_RESET}
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

DEFAULT_GITIGNORE = """
.env
.venv/
venv/
ENV/
.viclix
__pycache__/
*.py[cod]
*$py.class
.DS_Store
.vscode/
.idea/
"""


# ── Global config (was references/config.json) ──────────────────────────────
def load_config(required=True):
    """Load ~/.viclix/config.json. base_url always has a sane default.

    ``required`` gates the account token: commands that call the API need it,
    ``login`` itself does not.
    """
    cfg = {}
    try:
        with open(CONFIG_PATH, 'r') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        cfg = {}
    except Exception as e:
        logger.error(f"Failed to read config at {CONFIG_PATH}: {e}")
        sys.exit(1)

    base_url = cfg.get('base_url') or DEFAULT_BASE_URL
    if not base_url.endswith('/'):
        base_url += '/'
    cfg['base_url'] = base_url

    if required and not cfg.get('account_token'):
        logger.error("You're not logged in. Run 'viclix login' first.")
        sys.exit(1)
    return cfg


def save_config(cfg):
    os.makedirs(config_home(), exist_ok=True)
    with open(CONFIG_PATH, 'w') as f:
        json.dump(cfg, f, indent=2)
    # Best-effort: keep the token file private on POSIX. No-op on Windows.
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


# ── Per-project state (.viclix file in the repo) ────────────────────────────
def get_project_data():
    viclix_file = os.path.join(os.getcwd(), '.viclix')
    if os.path.exists(viclix_file):
        with open(viclix_file, 'r') as f:
            try:
                data = json.load(f)
                return data
            except json.JSONDecodeError:
                f.seek(0)
                return {"api_key": f.read().strip()}
    return None


def save_project_data(api_key=None, project_url=None, runtime=None, project_id=None):
    viclix_file = os.path.join(os.getcwd(), '.viclix')
    # Preserve anything already stored (e.g. runtime, local_env) across re-inits.
    data = get_project_data() or {}
    if api_key is not None:
        data["api_key"] = api_key
    if project_url:
        data["project_url"] = project_url
    if runtime:
        data["runtime"] = runtime
    if project_id:
        data["project_id"] = project_id
    logger.info(f"Saving project data to {viclix_file}")
    with open(viclix_file, 'w') as f:
        json.dump(data, f, indent=2)


# ── Git helpers ─────────────────────────────────────────────────────────────
def run_git(args, check=True, stream=False):
    logger.debug(f"Running git: {' '.join(args)}")
    if stream:
        print(f"\n{C_CYAN}▶ git {' '.join(args)}{C_RESET}")
        res = subprocess.run(['git'] + args)
        if res.returncode != 0 and check:
            logger.error(f"Git command failed: {' '.join(args)}")
            raise Exception(f"Git command failed with exit code {res.returncode}")
        return res
    else:
        res = subprocess.run(['git'] + args, capture_output=True, text=True)
        if res.returncode != 0 and check:
            logger.error(f"Git command failed: {' '.join(args)} | {res.stderr}")
            raise Exception(f"Git command failed: {res.stderr}")
        return res


def git_remote_url(remote='origin'):
    """Return the configured URL for a remote, or None if it isn't set."""
    if not os.path.exists('.git'):
        return None
    res = run_git(['remote', 'get-url', remote], check=False)
    url = (res.stdout or '').strip()
    return url or None


def git_current_branch():
    """Best-effort current branch name, or None."""
    if not os.path.exists('.git'):
        return None
    res = run_git(['rev-parse', '--abbrev-ref', 'HEAD'], check=False)
    branch = (res.stdout or '').strip()
    return branch if branch and branch != 'HEAD' else None


def normalize_to_https(url):
    """Convert an scp-style SSH URL (git@host:owner/repo.git) to https so
    Viclix can clone it over HTTP. https:// URLs pass through untouched."""
    url = (url or '').strip()
    if url.startswith('git@'):
        # git@github.com:owner/repo.git  ->  https://github.com/owner/repo.git
        try:
            host, path = url[len('git@'):].split(':', 1)
            return f"https://{host}/{path}"
        except ValueError:
            return url
    return url


def strip_credentials(url):
    """Drop any embedded user:token@ so we never print secrets."""
    if '://' not in url:
        return url
    scheme, rest = url.split('://', 1)
    if '@' in rest:
        rest = rest.split('@', 1)[1]
    return f"{scheme}://{rest}"


def embed_token(url, token):
    """Embed a token into an https URL so a private repo can be cloned."""
    url = normalize_to_https(strip_credentials(url))
    if token and url.startswith('https://'):
        return url.replace('https://', f'https://{token}@', 1)
    return url


def setup_git_repo(name, repo_url, github_token):
    """Initializes git, creates .gitignore/README if missing, and pushes code."""
    logger.info(f"Setting up Git repository for: {name}")

    # 1. Init git if needed
    if not os.path.exists('.git'):
        run_git(['init'])
        logger.debug("Initialized empty Git repository")

    # 2. Ensure .gitignore exists and has required entries
    gitignore_path = '.gitignore'
    if not os.path.exists(gitignore_path):
        with open(gitignore_path, 'w') as f:
            f.write(DEFAULT_GITIGNORE.strip() + "\n")
        logger.debug("Created default .gitignore")

    # 3. Ensure README.md exists
    readme_path = 'README.md'
    if not os.path.exists(readme_path):
        with open(readme_path, 'w') as f:
            f.write(f"# {name}\n\nProject initialized for Viclix.")
        logger.debug("Created default README.md")

    # 4. Set remote URL with token for auth
    auth_url = embed_token(repo_url, github_token)

    run_git(['remote', 'remove', 'origin'], check=False)
    run_git(['remote', 'add', 'origin', auth_url])

    # 5. Commit and Push
    run_git(['add', '.'])
    status = run_git(['status', '--porcelain'])
    if status.stdout.strip():
        run_git(['commit', '-m', 'Initial commit from Viclix CLI'])

    run_git(['branch', '-M', 'main'])
    current_branch = 'main'

    logger.info(f"Pushing code to {current_branch}...")
    run_git(['push', '-u', 'origin', current_branch, '--force'])
    logger.info("Code uploaded successfully.")


def push_existing(branch):
    """Commit any pending changes and push the existing remote so Viclix
    clones the latest code. Used by the 'register existing remote' path."""
    run_git(['add', '.'], stream=True)
    status = run_git(['status', '--porcelain'])
    if status.stdout.strip():
        run_git(['commit', '-m', f"Viclix init {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"], stream=True)
    else:
        logger.info("No local changes to commit.")
    run_git(['push', 'origin', branch], check=False, stream=True)


def get_github_repo(name, token):
    """Checks if repo exists, if not creates it. Returns clone URL."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json"
    }
    logger.info(f"Checking GitHub for repository: {name}")

    user_res = requests.get("https://api.github.com/user", headers=headers, timeout=15)
    if user_res.status_code != 200:
        logger.error(f"Error fetching GitHub user: {user_res.text}")
        sys.exit(1)
    username = user_res.json()['login']

    repo_url = f"https://api.github.com/repos/{username}/{name}"
    res = requests.get(repo_url, headers=headers, timeout=15)

    if res.status_code == 200:
        logger.info(f"Found existing repo: {name}")
        return res.json()['clone_url']
    elif res.status_code == 404:
        # 404 also means "exists but this token can't see it" — a fine-grained
        # token scoped to selected repos hides the rest. Creation below tells us
        # which it was (422 'name already exists').
        logger.info(f"Creating new private GitHub repo: {name}")
        create_res = requests.post("https://api.github.com/user/repos",
                                   headers=headers,
                                   json={"name": name, "private": True}, timeout=30)
        if create_res.status_code == 201:
            return create_res.json()['clone_url']
        if create_res.status_code == 403:
            need = _github_missing_permission_hint(create_res)
            logger.error(f"GitHub won't let this token create repos"
                         + (f" — it's missing {need}." if need else "."))
            _print_github_token_help(need_create=True)
            logger.error(f"Fix the token at {GH_TOKEN_SETTINGS_URL} "
                         "then re-run 'viclix init'. Or skip creation: create the repo on "
                         "GitHub yourself and re-run with --repo <URL>.")
            sys.exit(1)
        if create_res.status_code == 422 and 'already exists' in create_res.text:
            logger.error(f"A repo named '{name}' already exists on {username}, but this token "
                         "can't see it — its repository access probably doesn't include it. "
                         f"Widen the token to 'All repositories', or re-run with "
                         f"--repo https://github.com/{username}/{name}")
            sys.exit(1)
        logger.error(f"Error creating repo: {create_res.text}")
        sys.exit(1)
    elif res.status_code == 401:
        logger.error("GitHub rejected this token (401). It may be expired or revoked — "
                     "create a new one and save it with 'viclix login --github-token <PAT>'.")
        sys.exit(1)
    else:
        logger.error(f"GitHub API error: {res.status_code}")
        sys.exit(1)


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


def _explain_api_error(res):
    """Turn a Viclix API error response into a short, human sentence."""
    detail = _err_detail(res)
    low = (detail or '').lower()
    if res.status_code in (401, 403):
        if 'account token' in low and 'project_id' in low:
            return "This repo isn't linked to a project yet. Run 'viclix link' or 'viclix init'."
        return "Your token was rejected. Check it in Settings → Tokens on the dashboard."
    return detail or f"HTTP {res.status_code}"


# ── GitHub access checks (fail early instead of a build that can't clone) ────
# Where to send people to make a token, and what it must carry. Fine-grained
# PATs (github_pat_…) are the default GitHub offers now, and their per-repo
# "access" selector is NOT a permission grant — repo creation needs
# Administration:write on top of it, which is off by default. Missing it is the
# #1 cause of "Resource not accessible by personal access token" on init.
GH_CLASSIC_TOKEN_URL = "https://github.com/settings/tokens/new?scopes=repo&description=Viclix"
GH_FINEGRAINED_TOKEN_URL = "https://github.com/settings/personal-access-tokens/new"
GH_TOKEN_SETTINGS_URL = "https://github.com/settings/personal-access-tokens"


def _print_github_token_help(need_create=True):
    """Print exactly which token permissions Viclix needs, for both token types.

    need_create=True  → the token will also create repos (Administration:write).
    need_create=False → read/clone only, so the Administration line is dropped."""
    print(f"\n{C_BOLD}What the token needs{C_RESET}")
    print(f"  {C_BOLD}Classic token{C_RESET} (simplest) — tick the {C_CYAN}repo{C_RESET} scope. That's all.")
    print(f"  {C_BOLD}Fine-grained token{C_RESET} ({C_YELLOW}github_pat_…{C_RESET}) — set all of these:")
    print(f"     • Resource owner: {C_CYAN}your account{C_RESET} (or the org that will own the repo)")
    print(f"     • Repository access: {C_CYAN}All repositories{C_RESET}"
          + (f"  {C_YELLOW}← 'Only select' can't create repos{C_RESET}" if need_create else ""))
    print(f"     • Repository permissions → {C_CYAN}Contents: Read and write{C_RESET}")
    print(f"     • Repository permissions → {C_CYAN}Metadata: Read-only{C_RESET} (auto-selected)")
    if need_create:
        print(f"     • Repository permissions → {C_CYAN}Administration: Read and write{C_RESET}"
              f"  {C_YELLOW}← required to CREATE repos{C_RESET}")
    print(f"  {C_YELLOW}Note:{C_RESET} picking 'All repositories' only chooses which repos the token")
    print("  can touch — you still have to grant the permissions listed above.")


def _github_username(token):
    """Return the GitHub login for a token, or None if the token is invalid."""
    try:
        r = requests.get("https://api.github.com/user",
                         headers={"Authorization": f"token {token}",
                                  "Accept": "application/vnd.github+json"}, timeout=15)
    except requests.RequestException:
        return None
    return r.json().get('login') if r.status_code == 200 else None


def _github_repo_slug(url):
    """Extract 'owner/repo' from an https or ssh GitHub URL, else None."""
    u = normalize_to_https(strip_credentials(url or '')).strip()
    m = re.search(r'github\.com[/:]([^/]+/[^/]+?)(?:\.git)?/?$', u)
    return m.group(1) if m else None


def _github_repo_accessible(url, token):
    """True/False if `token` can read the repo behind `url`; None if not a
    checkable GitHub URL (so callers can skip the pre-check gracefully)."""
    slug = _github_repo_slug(url)
    if not slug:
        return None
    try:
        r = requests.get(f"https://api.github.com/repos/{slug}",
                         headers={"Authorization": f"token {token}",
                                  "Accept": "application/vnd.github+json"}, timeout=15)
    except requests.RequestException:
        return None
    return r.status_code == 200


def _github_can_create_repos(token):
    """True/False if `token` is allowed to create repos; None if we couldn't tell.

    Asks GitHub itself instead of guessing from the token's shape: POST with an
    empty body is permission-checked BEFORE the payload is validated, so a token
    that may create repos comes back 422 ('name must not be blank') and one that
    may not comes back 403 — and nothing is ever created either way."""
    try:
        r = requests.post("https://api.github.com/user/repos",
                          headers={"Authorization": f"Bearer {token}",
                                   "Accept": "application/vnd.github+json"},
                          json={}, timeout=15)
    except requests.RequestException:
        return None
    if r.status_code == 422:      # permission OK, payload rejected — as expected
        return True
    if r.status_code == 403:
        return False
    return None                   # 401/5xx/anything else — don't block on it


def _github_missing_permission_hint(res):
    """Turn a GitHub 403 into the concrete permission the token is missing.

    GitHub names it in x-accepted-github-permissions (e.g. 'administration=write'),
    which is the whole answer — surface it instead of the opaque 'Resource not
    accessible by personal access token'."""
    accepted = (res.headers.get('x-accepted-github-permissions') or '').strip()
    return accepted.split(',')[0].strip() if accepted else None


def _generate_deploy_key(comment):
    """Generate an ed25519 keypair with ssh-keygen. Returns (private, public)
    as strings, or (None, None) if ssh-keygen isn't installed or failed."""
    if not shutil.which('ssh-keygen'):
        return None, None
    tmp = tempfile.mkdtemp(prefix='viclix_key_')
    key_path = os.path.join(tmp, 'id_ed25519')
    try:
        res = subprocess.run(
            ['ssh-keygen', '-t', 'ed25519', '-N', '', '-C', comment, '-f', key_path],
            capture_output=True, text=True)
        if res.returncode != 0:
            return None, None
        with open(key_path, 'r', encoding='utf-8') as f:
            private = f.read()
        with open(key_path + '.pub', 'r', encoding='utf-8') as f:
            public = f.read().strip()
        return private, public
    except Exception:
        return None, None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── Auth resolution (account token vs project key) ──────────────────────────
def _tok_url(base_url, path, token, project_id=None):
    """Build an authenticated API URL. Adds project_id when present, which the
    server needs to pick the project for an ACCOUNT token (harmless — validated —
    for a project token)."""
    url = f"{base_url}{path}?token={token}"
    if project_id:
        url += f"&project_id={project_id}"
    return url


def _tok_params(token, project_id=None, **extra):
    """Same idea as _tok_url but for requests(..., params=...)."""
    p = {"token": token}
    if project_id:
        p["project_id"] = project_id
    p.update(extra)
    return p


def _wait_for_deploy(base_url, api_key, project_id, stream=False, timeout=600):
    """Poll projects/deploy/status until the build reaches a terminal state.

    A CLIENT-SIDE loop — never one long-held request — so a 5-minute full rebuild
    survives proxy idle timeouts. Two independent stops: an overall `timeout`, and
    a 90s stall guard (no phase change AND no new log bytes). Default output is
    compact phase/progress; `stream` tails the full live build log incrementally
    (only new bytes each poll via since_offset). Returns an exit code: 0 running,
    1 failed/stalled/timed-out.
    """
    import time
    poll, stall_limit = 3, 90
    started = last_change = time.time()
    offset = 0
    last_phase = last_progress = None
    url_base = _tok_url(base_url, 'projects/deploy/status', api_key, project_id)
    print(f"{C_CYAN}Waiting for deploy…{C_RESET}")
    while True:
        try:
            r = requests.get(f"{url_base}&since_offset={offset}", timeout=15)
        except Exception as e:
            if time.time() - started > timeout:
                logger.error("Timed out waiting for deploy. Check: viclix logs-build")
                return 1
            logger.warning(f"poll error: {e}")
            time.sleep(poll)
            continue
        if r.status_code != 200:
            logger.warning(f"deploy/status {r.status_code}: {r.text[:200]}")
            time.sleep(poll)
            continue

        d = r.json()
        state = d.get('state')
        phase = d.get('phase')
        progress = d.get('progress_pct')
        delta = d.get('log_delta') or ''
        new_offset = d.get('log_offset') or offset

        moved = False
        if stream and delta:
            sys.stdout.write(delta)
            sys.stdout.flush()
            moved = True
        elif (phase, progress) != (last_phase, last_progress):
            print(f"  {C_CYAN}▸{C_RESET} {phase} · {progress}%")
            moved = True
        if new_offset != offset:
            moved = True
        offset = new_offset
        last_phase, last_progress = phase, progress
        if moved:
            last_change = time.time()

        if d.get('terminal'):
            if d.get('ok') is True or state == 'running':
                print(f"\n{C_GREEN}✓ deploy running{C_RESET}")
                return 0
            if state == 'sleeping':
                print(f"\n{C_YELLOW}app is sleeping{C_RESET}")
                return 0
            print(f"\n{C_RED}✗ deploy failed{C_RESET} ({d.get('deploy_status')})")
            tail = (d.get('log_delta') or '').strip()
            if tail:
                print(tail[-2000:])
            print("See: viclix logs-build")
            return 1

        now = time.time()
        if now - started > timeout:
            logger.error(f"Timed out after {timeout}s (state: {state}). Check: viclix logs-build")
            return 1
        if now - last_change > stall_limit:
            logger.error(f"No progress for {stall_limit}s (state: {state}) — build looks "
                         f"stalled. Check: viclix logs-build")
            return 1
        time.sleep(poll)


def resolve_auth(args, proj=None):
    """Return (token, project_id) for a per-project API call.

    Precedence:
      1. --project-key / --api-key on the CLI      → that project token.
      2. api_key stored in .viclix                 → that project token (legacy /
                                                     a shared other-account key).
      3. account token (global config) + project_id from .viclix → account path.

    The account path is what lets one account token drive every project; the
    project_id (non-secret, stored in .viclix) tells the server which one.
    """
    if proj is None:
        proj = get_project_data() or {}
    project_id = proj.get('project_id')
    explicit = getattr(args, 'project_key', None) or getattr(args, 'api_key', None)
    if explicit:
        return explicit, project_id
    if proj.get('api_key'):
        return proj['api_key'], project_id
    cfg = load_config(required=False)
    if cfg.get('account_token') and project_id:
        return cfg['account_token'], project_id
    return None, project_id


def require_auth(args, proj=None):
    """resolve_auth, but exit with a helpful message when nothing is available."""
    token, project_id = resolve_auth(args, proj)
    if not token:
        logger.error("This repo isn't linked to a Viclix project. Run 'viclix init' "
                     "or 'viclix link' here, or pass --project-key.")
        sys.exit(1)
    return token, project_id


# ── Env + API helpers (parity with the manual wizard) ───────────────────────
def _api_post(url, **kwargs):
    """POST with a clean error instead of a traceback on network failure."""
    try:
        return requests.post(url, **kwargs)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix: {e}")
        sys.exit(1)


def parse_env_file(path):
    """Minimal KEY=VALUE parser, matching the dashboard's server-side one."""
    try:
        with open(path, 'r', encoding='utf-8') as f:
            text = f.read()
    except OSError as e:
        logger.error(f"Could not read env file '{path}': {e}")
        sys.exit(1)
    out = {}
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith('#') or '=' not in s:
            continue
        key, value = s.split('=', 1)
        key = key.strip()
        if key.startswith('export '):
            key = key[len('export '):].strip()
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def collect_env(args):
    """Combine --env-file and repeated --env KEY=VALUE into one dict."""
    env = {}
    if getattr(args, 'env_file', None):
        env.update(parse_env_file(args.env_file))
    for item in (getattr(args, 'env', None) or []):
        if '=' in item:
            key, value = item.split('=', 1)
            env[key.strip()] = value.strip()
    return env


def api_upload_sqlite(base_url, token, project_id, remote_path, local_file):
    url = _tok_url(base_url, 'projects/sqlite/upload', token, project_id)
    logger.info(f"Uploading SQLite seed '{os.path.basename(local_file)}' → {remote_path}")
    with open(local_file, 'rb') as f:
        res = _api_post(
            url,
            data={'path': remote_path},
            files={'data': (os.path.basename(local_file), f)},
        )
    if res.status_code == 200:
        logger.info("SQLite seed uploaded.")
    else:
        logger.error(f"SQLite upload failed: {res.text}")
        sys.exit(1)


def api_set_env(base_url, token, project_id, env, mode='merge'):
    url = _tok_url(base_url, 'projects/env', token, project_id)
    res = _api_post(url, json={'env_vars': env, 'mode': mode})
    if res.status_code == 200:
        logger.info(f"Environment updated ({len(env)} variable(s)).")
    else:
        logger.error(f"Failed to set env: {res.text}")
        sys.exit(1)


def api_set_static(base_url, token, project_id, prefix, cache):
    url = _tok_url(base_url, 'projects/static', token, project_id)
    res = _api_post(url, json={'path_prefix': prefix, 'cache_max_age': cache or 86400})
    if res.status_code == 200:
        logger.info(f"Static path set: {prefix}")
    else:
        logger.error(f"Failed to set static path: {res.text}")
        sys.exit(1)


def api_rebuild(base_url, token, project_id, full=True):
    url = _tok_url(base_url, 'projects/rebuild', token, project_id)
    if full:
        url += "&full=true"
    res = _api_post(url)
    if res.status_code != 200:
        logger.warning(f"Could not trigger the first deploy automatically: {res.text}")


# ── Commands ────────────────────────────────────────────────────────────────
def cmd_login(args):
    """Save the user's Viclix account token to ~/.viclix/config.json."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}

    # B4: `viclix login --github-token X` while already signed in updates just
    # that credential — don't re-prompt for the account token.
    if args.github_token and not args.token and cfg.get('account_token'):
        cfg['github_token'] = args.github_token
        if args.base_url:
            cfg['base_url'] = args.base_url if args.base_url.endswith('/') else args.base_url + '/'
        save_config(cfg)
        logger.info(f"GitHub token updated ({_mask_secret(args.github_token)}). Account sign-in unchanged.")
        return

    token = args.token
    if not token and not _interactive():
        logger.error("Not signed in and no token given. Pass --token <ACCOUNT_TOKEN> "
                     "(create one at Settings → Tokens on the dashboard).")
        sys.exit(1)
    if not token:
        try:
            token = input(f"{C_CYAN}Paste your Viclix account API token: {C_RESET}").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(1)
    if not token:
        logger.error("No token provided.")
        sys.exit(1)

    base_url = args.base_url or cfg.get('base_url') or DEFAULT_BASE_URL
    if not base_url.endswith('/'):
        base_url += '/'

    cfg['account_token'] = token
    cfg['base_url'] = base_url
    if args.github_token:
        cfg['github_token'] = args.github_token

    save_config(cfg)
    logger.info(f"Credentials saved to {CONFIG_PATH}")

    # Light verification — save regardless, but tell the user if it looks wrong.
    # Verify against the ACCOUNT endpoint: this is an account token, not a
    # project token, so /projects (project-scoped) would reject it by design.
    try:
        res = requests.get(f"{base_url}account", params={"token": token}, timeout=15)
        if res.status_code == 200:
            who = ''
            try:
                email = res.json().get('email')
                who = f" as {email}" if email else ''
            except Exception:
                pass
            logger.info(f"Token verified — signed in{who}. Run 'viclix init' inside a repo.")
        elif res.status_code in (401, 403):
            logger.warning("Saved, but the server rejected this token. Double-check it in Settings → Tokens.")
        elif res.status_code == 404:
            # Older server without /account — can't pre-verify account tokens;
            # the token is saved and still works for init/deploy.
            logger.info("Credentials saved. (This server can't pre-verify account tokens — that's fine.)")
        else:
            logger.warning(f"Saved, but couldn't verify the token (HTTP {res.status_code}).")
    except requests.RequestException:
        logger.warning("Saved, but couldn't reach the server to verify the token.")


def cmd_logout(args):
    """Sign out: drop the stored account token. Keeps any saved GitHub token so
    signing into another account doesn't need it re-entered. Idempotent, and it
    never touches project .viclix files (your project_id stays put)."""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    if cfg.get('account_token'):
        cfg.pop('account_token', None)
        save_config(cfg)
        logger.info("Signed out — account token removed. "
                    "(GitHub token kept; run 'viclix disconnect' to clear everything.)")
    else:
        logger.info("Already signed out — no account token stored.")


def cmd_update(args):
    """Upgrade the Viclix CLI to the latest release from PyPI."""
    logger.info("Updating the Viclix CLI to the latest version…")
    cmd = [sys.executable, '-m', 'pip', 'install', '--upgrade', '--no-cache-dir', 'viclix']
    logger.debug(f"Running: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd)
    except Exception as e:
        logger.error(f"Could not run pip: {e}")
        sys.exit(1)
    if res.returncode == 0:
        logger.info("Viclix CLI is up to date.")
    else:
        logger.error("Update failed. Try running it yourself:\n"
                     "    pip install --upgrade --no-cache-dir viclix")
        sys.exit(res.returncode)


def cmd_disconnect(args):
    """Full reset of the CLI's own config (~/.viclix/config.json): account token,
    GitHub credentials, base URL. Idempotent, and it never touches project
    .viclix files — your project_id and links stay intact."""
    if os.path.exists(CONFIG_PATH):
        try:
            os.remove(CONFIG_PATH)
        except OSError as e:
            logger.error(f"Could not remove {CONFIG_PATH}: {e}")
            sys.exit(1)
        logger.info(f"Disconnected — cleared all stored credentials ({CONFIG_PATH}).")
        logger.info("Run 'viclix setup' (or 'viclix login') to set things up again.")
    else:
        logger.info("Nothing to clear — no stored credentials.")


def cmd_whoami(args):
    cfg = load_config(required=True)
    try:
        res = requests.get(f"{cfg['base_url']}account", params={"token": cfg['account_token']}, timeout=15)
        if res.status_code == 200:
            logger.info(f"Logged in against {cfg['base_url']}")
            print_json(res.json())
        else:
            logger.error(f"Token check failed (HTTP {res.status_code}).")
            sys.exit(1)
    except requests.RequestException as e:
        logger.error(f"Could not reach the server: {e}")
        sys.exit(1)


def cmd_open(args):
    """Open this project's dashboard page (/project/<id>) in the browser.

    Local-only: reads project_id from .viclix and the dashboard origin from the
    saved config (falling back to the default). No account token required — the
    dashboard prompts for its own login. An explicit id may be passed as an
    argument (``viclix open <project_id>``) to open any project."""
    project_id = (getattr(args, 'target', None) or '').strip()
    if not project_id:
        proj = get_project_data() or {}
        project_id = proj.get('project_id')
    if not project_id:
        logger.error("This repo isn't linked to a Viclix project. Run 'viclix init' "
                     "or 'viclix link' here, or pass a project id: viclix open <id>.")
        sys.exit(1)

    cfg = load_config(required=False)
    dash = _dashboard_base(cfg.get('base_url'))
    url = f"{dash}/project/{project_id}"
    logger.info(f"Opening {url}")
    if not _open_url(url):
        logger.warning("Couldn't launch a browser automatically — open the URL above manually.")


def cmd_init(args, cfg):
    base_url = cfg['base_url']
    account_token = cfg['account_token']
    # B3: a --github-token passed to init must apply to EVERY path (existing
    # remote included), not only when we create the repo. CLI flag beats the
    # stored one.
    github_token = (args.github_token or '').strip() or cfg.get('github_token')
    # SSH deploy key (private key contents) — from the wizard, or --ssh-key-file.
    ssh_key = getattr(args, 'ssh_key', None)
    ssh_key_file = getattr(args, 'ssh_key_file', None)
    if not ssh_key and ssh_key_file:
        try:
            with open(ssh_key_file, 'r', encoding='utf-8') as f:
                ssh_key = f.read()
        except OSError as e:
            logger.error(f"Could not read --ssh-key-file '{ssh_key_file}': {e}")
            sys.exit(1)

    name = args.name or os.path.basename(os.getcwd())

    # Decide where the code lives.
    #   --repo given          → trust it
    #   origin remote exists  → register it as-is
    #   otherwise             → create a GitHub repo + push (needs a PAT)
    origin = git_remote_url('origin')

    if args.repo:
        repo_url = args.repo
        branch = args.branch or git_current_branch() or 'main'
        shown = _mask_url_credentials(repo_url) if '@' in repo_url else strip_credentials(repo_url)
        logger.info(f"Using provided repo: {shown}")
    elif origin:
        repo_url = origin
        branch = args.branch or git_current_branch() or 'main'
        logger.info(f"Detected existing remote '{strip_credentials(repo_url)}' on branch '{branch}'.")
        # Make sure Viclix clones the latest state.
        try:
            push_existing(branch)
        except Exception as e:
            logger.warning(f"Could not push to the existing remote automatically: {e}")
    else:
        logger.info("No git remote found — creating a GitHub repository for you.")
        if not github_token:
            if not _interactive():
                logger.error("No git remote and no GitHub token. Pass --github-token <PAT> to "
                             "create a repo, or --repo <URL> to register an existing one.")
                logger.error("The token needs the 'repo' scope (classic) or, if fine-grained: "
                             "Repository access 'All repositories' + Administration: Read and "
                             "write + Contents: Read and write.")
                sys.exit(1)
            _print_github_token_help(need_create=True)
            github_token = _ask(
                f"{C_YELLOW}GitHub token (needed to create & push a new repo): {C_RESET}")
        if not github_token:
            logger.error("A GitHub token is required to create a repo. "
                         "Or add a remote yourself and re-run 'viclix init'.")
            sys.exit(1)
        repo_url = get_github_repo(name, github_token)
        setup_git_repo(name, repo_url, github_token)
        branch = 'main'

    # Decide how Viclix will clone the repo.
    slug = _github_repo_slug(repo_url)
    if ssh_key:
        # Deploy-key path: Viclix clones over SSH server-side. Send the plain
        # https URL (the server converts it to SSH) and never embed a token.
        clone_url = normalize_to_https(strip_credentials(repo_url))
        logger.info("Using an SSH deploy key to clone this repo.")
    else:
        # B3: for an existing private repo, confirm read access up front when we
        # hold a token — otherwise a failed clone only surfaces later as
        # "could not read Username for github.com".
        if slug and github_token and '@' not in repo_url:
            if _github_repo_accessible(repo_url, github_token) is False:
                logger.error(f"That GitHub token can't read {slug}. Use a classic token with the "
                             "'repo' scope, or a fine-grained one whose Repository access includes "
                             f"{slug} with Contents: Read.")
                sys.exit(1)
        elif slug and not github_token and '@' not in repo_url:
            logger.warning(f"No GitHub token stored for {slug}. If it's private, Viclix can't clone "
                           "it — pass --github-token <PAT> or run 'viclix setup'.")
        # For private repos Viclix needs credentials to clone. If we hold a GitHub
        # token, embed it; otherwise send the plain URL (fine for public repos).
        if github_token and 'github.com' in repo_url:
            clone_url = embed_token(repo_url, github_token)
        else:
            clone_url = normalize_to_https(repo_url)

    # Optional setup mirrored from the manual wizard (db / env / static).
    env_vars = collect_env(args)
    static_path = (args.static or '').strip()
    db_mode = args.db or 'none'
    sqlite_path = (args.sqlite_path or '').strip()
    # A local file to upload implies 'upload' mode.
    if args.sqlite_file:
        if not os.path.exists(args.sqlite_file):
            logger.error(f"SQLite file not found: {args.sqlite_file}")
            sys.exit(1)
        db_mode = 'upload'
        if not sqlite_path:
            sqlite_path = './data/' + os.path.basename(args.sqlite_file)
    if db_mode in ('sqlite', 'upload') and not sqlite_path:
        sqlite_path = './data/app.db'

    # Static-site build config (only meaningful when runtime == static).
    static_build = (args.build or '').strip()
    static_output = (args.output or '').strip()

    payload = {
        "name": name,
        "repo_url": clone_url,
        "repo_branch": branch,
        "runtime": args.runtime,
        "static_build_command": static_build or None,
        "static_output_dir": static_output or None,
        "db_mode": db_mode,
        "sqlite_path": sqlite_path or None,
        "env_vars": env_vars,
        "static_path": static_path or None,
        "cache_max_age": args.cache_max_age,
        "repo_ssh_key": ssh_key or None,
        "auth_required": bool(ssh_key),
    }
    url = f"{base_url}projects/new?token={account_token}"
    logger.debug(f"Creating Viclix project '{name}' on branch '{branch}' (db={db_mode})")
    try:
        response = requests.post(url, json=payload)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix at {base_url}: {e}")
        sys.exit(1)

    if response.status_code != 200:
        logger.error(f"Viclix Error: {response.text}")
        sys.exit(1)

    data = response.json()
    api_key = data.get('api_key')
    project_id = data.get('project_id')

    # New model: .viclix stores the non-secret project_id and the account token
    # (global) authenticates. We only persist the generated project key when
    # there's no account token to fall back on — keeping the repo file
    # secret-free by default. project_id is always saved.
    persist_key = None if account_token else api_key
    save_project_data(api_key=persist_key, project_url=data.get('project_url'),
                      runtime=args.runtime, project_id=project_id)
    logger.info(f"SUCCESS: Project created on Viclix. ID: {project_id}")
    for warn in (data.get('warnings') or []):
        logger.warning(warn)

    # Post-init calls authenticate with the account token + project_id (or the
    # project key if that's all we have).
    tok = account_token or api_key
    # Push the SQLite seed (if any), then release the deploy that was held for it.
    if args.sqlite_file:
        api_upload_sqlite(base_url, tok, project_id, sqlite_path, args.sqlite_file)
    if data.get('awaiting_upload'):
        logger.info("Triggering first deploy...")
        api_rebuild(base_url, tok, project_id, full=True)

    if data.get('project_url'):
        logger.info(f"Project URL: {data['project_url']}")


# ── Guided setup (viclix setup / first run) ─────────────────────────────────
def _dashboard_base(base_url):
    """Turn the API base (…/api/v1/) into the dashboard origin for browser links."""
    root = (base_url or DEFAULT_BASE_URL).split('/api/')[0].rstrip('/')
    return root or 'https://dashboard.viclix.com'


def _verify_account(base_url, token, timeout=15):
    """(ok, ' as email') for an account token — used to greet the user by name.
    ok is True (valid), False (rejected), or None (couldn't verify / offline)."""
    try:
        r = requests.get(f"{base_url}account", params={"token": token}, timeout=timeout)
    except requests.RequestException:
        return None, ''  # couldn't reach — unknown, not a rejection
    if r.status_code == 200:
        try:
            email = r.json().get('email')
        except Exception:
            email = None
        return True, (f" as {email}" if email else '')
    if r.status_code in (401, 403):
        return False, ''
    return None, ''  # older server / transient — treat as unverified-but-ok


def _print_status_header(cfg):
    """Two-line status banner shown at the top of a bare `viclix`: whether an
    account is signed in (verified by a short call) and whether a GitHub
    credential is stored."""
    token = cfg.get('account_token')
    if not token:
        acct = f"{C_RED}no — run 'viclix setup'{C_RESET}"
    else:
        ok, who = _verify_account(cfg['base_url'], token, timeout=6)
        if ok:
            email = who[4:] if who.startswith(' as ') else ''
            acct = f"{C_GREEN}{email or 'yes'}{C_RESET}"
        elif ok is False:
            acct = f"{C_RED}token rejected — run 'viclix login'{C_RESET}"
        else:
            acct = f"{C_YELLOW}signed in (offline — couldn't verify){C_RESET}"
    gh = f"{C_GREEN}yes{C_RESET}" if cfg.get('github_token') else f"{C_YELLOW}no{C_RESET}"
    print(f"{C_BOLD}Logged in:{C_RESET} {acct}")
    print(f"{C_BOLD}Github connected:{C_RESET} {gh}")
    print()


def _blank_init_args(**overrides):
    """A namespace carrying every attribute cmd_init / collect_env read, so the
    wizard can drive init exactly like the flags would."""
    ns = argparse.Namespace(
        name=None, repo=None, branch=None, github_token=None, ssh_key=None,
        runtime='auto', build=None, output=None, db=None,
        sqlite_path=None, sqlite_file=None, env_file=None, env=None,
        static=None, cache_max_age=None,
    )
    for key, value in overrides.items():
        setattr(ns, key, value)
    return ns


def _wizard_login(cfg, pre_token=None):
    """Step 1: obtain + verify an account token, save it. Returns updated cfg."""
    print(f"\n{C_BOLD}Step 1 · Sign in{C_RESET}")
    base_url = cfg.get('base_url') or DEFAULT_BASE_URL
    if not base_url.endswith('/'):
        base_url += '/'
    dash = _dashboard_base(base_url)

    token = (pre_token or '').strip()
    if not token:
        print("To talk to your Viclix account, the CLI uses an 'account token' — a long")
        print(f"secret string that proves it's you (like a password, but just for the CLI).")
        print(f"You create one on the dashboard under {C_CYAN}Settings → Tokens{C_RESET}.")
        idx = _menu("How do you want to sign in?", [
            "I have a token — paste it",
            "Open the dashboard to get one",
            "I don't have an account yet — sign up",
        ])
        if idx == 1:
            _open_url(dash)
            print(f"Opened {dash} — create a token under Settings → Tokens, then paste it.")
        elif idx == 2:
            signup = "https://www.viclix.com/signup"
            _open_url(signup)
            print(f"Opened {signup} — sign up, then come back and create a token "
                  f"under Settings → Tokens on {dash}.")
        token = _ask(f"{C_CYAN}Paste your Viclix account token: {C_RESET}")
        while not token:
            token = _ask(f"{C_CYAN}Token (or press Ctrl-C to cancel): {C_RESET}")

    cfg['account_token'] = token
    cfg['base_url'] = base_url
    ok, who = _verify_account(base_url, token)
    if ok is False:
        if not _confirm("That token was rejected by the server. Save and continue anyway?", default=False):
            logger.error("Setup cancelled — get a fresh token under Settings → Tokens.")
            sys.exit(1)
    save_config(cfg)
    logger.info(f"Signed in{who}. Credentials saved to {CONFIG_PATH}")
    return cfg


def _wizard_connect_github(cfg):
    """Optionally connect a global GitHub token during setup, so Viclix can work
    across all your repos without per-repo setup. Skippable; explains the trade."""
    if cfg.get('github_token'):
        logger.info("GitHub already connected.")
        return
    print(f"\n{C_BOLD}Connect GitHub (optional, recommended){C_RESET}")
    print("Viclix can store one GitHub token to use for all your projects. What it does:")
    print(f"  • {C_GREEN}Create{C_RESET} new repos for you and push your code.")
    print(f"  • {C_GREEN}Clone{C_RESET} your private repos — no setup needed per repo.")
    print("It's a broad credential (it can reach your repos), and creating repos needs")
    print(f"{C_CYAN}Administration: Read and write{C_RESET} on top of that. Prefer to keep access")
    print(f"narrow? Skip this, and set up a read-only {C_GREEN}deploy key{C_RESET} per repo instead")
    print("(you'll be offered that when you connect a private repo). You can also give")
    print("a specific project its own token later — this global one is just the default.")
    if _confirm("Connect a GitHub token now?", default=True):
        _wizard_github_token(cfg, None, save_global=True)
    else:
        logger.info("Skipped — you can connect one later or set up access per repo.")


def _wizard_github_token(cfg, slug, save_global=True, need_create=None):
    """Obtain a GitHub token that can reach `slug` (or create repos).

    save_global=True  → store it as your global token (reused for every project).
    save_global=False → return it for THIS project only, without touching the
                        global one (so one repo can use a different token).
    need_create       → will this token have to create repos? Defaults to "yes
                        when there's no specific repo yet", which is exactly the
                        case that needs Administration:write."""
    if need_create is None:
        need_create = slug is None
    print(f"\n{C_BOLD}GitHub token{C_RESET}")
    print("Viclix clones over HTTPS, so it needs a personal access token"
          + (" that can also create repos." if need_create else " that can read your repos."))
    _print_github_token_help(need_create=need_create)
    if _confirm("Open GitHub to create the token now?", default=True):
        idx = _menu("Which kind?", [
            "Classic token — one 'repo' checkbox, fastest",
            "Fine-grained token — narrower, set the permissions listed above",
        ])
        _open_url(GH_CLASSIC_TOKEN_URL if idx in (0, None) else GH_FINEGRAINED_TOKEN_URL)
        print("Create it, copy it, and paste it below. (It's never printed back in full.)")
    token = _ask("Paste your GitHub token: ")
    while token:
        user = _github_username(token)
        if not user:
            print(f"{C_RED}That token didn't authenticate with GitHub.{C_RESET}")
            token = _ask("Paste a valid token (or Ctrl-C to cancel): ")
            continue
        # Check the permission BEFORE saving it — otherwise the gap only shows up
        # much later, as a 403 in the middle of 'viclix init'.
        if need_create and _github_can_create_repos(token) is False:
            print(f"{C_RED}Authenticated as {user}, but this token can't create repos.{C_RESET}")
            print(f"{C_YELLOW}Missing: Administration: Read and write "
                  f"(and Repository access must be 'All repositories').{C_RESET}")
            print(f"You can edit the token in place at {C_CYAN}{GH_TOKEN_SETTINGS_URL}{C_RESET} — "
                  "no need to make a new one.")
            if not _confirm("Use it anyway? (fine if you'll always bring your own repo)", default=False):
                token = _ask("Paste a token that can create repos: ")
                continue
        if slug and _github_repo_accessible(f"https://github.com/{slug}", token) is False:
            print(f"{C_RED}Authenticated as {user}, but this token can't read {slug}.{C_RESET}")
            print(f"{C_YELLOW}A fine-grained token must list {slug} under 'Repository access' "
                  f"and grant Contents: Read.{C_RESET}")
            if not _confirm("Use it anyway?", default=False):
                token = _ask("Paste a token that has access: ")
                continue
        if save_global:
            cfg['github_token'] = token
            save_config(cfg)
            logger.info(f"GitHub connected ({_mask_secret(token)}) — saved for all your projects.")
        else:
            logger.info(f"GitHub token accepted ({_mask_secret(token)}) — used for this project only.")
        return token
    return None


def _wizard_ssh_deploy_key(slug):
    """Generate a deploy key, walk the user through adding the public half to
    their repo, and return the PRIVATE half for Viclix. None on cancel/failure.

    Explains every step in plain language — someone who's never seen an SSH key
    should be able to follow along."""
    print(f"\n{C_BOLD}Setting up an SSH deploy key{C_RESET}")
    print("A deploy key is a matching pair of keys:")
    print(f"  • a {C_GREEN}PUBLIC{C_RESET} half you paste into your GitHub repo (safe to share), and")
    print(f"  • a {C_GREEN}PRIVATE{C_RESET} half Viclix keeps to clone the repo (never shown to anyone else).")
    print("Together they let Viclix read THIS one repo — nothing else on your account.")
    print("I'll generate the pair now; you just paste the public half into GitHub.")
    private, public = _generate_deploy_key(f"viclix-{slug.replace('/', '-')}")
    if not private:
        print(f"{C_YELLOW}Couldn't run ssh-keygen (it may not be installed).{C_RESET}")
        return None
    print(f"\n{C_BOLD}1.{C_RESET} Copy this PUBLIC key — the whole line:\n")
    print(f"{C_CYAN}{public}{C_RESET}\n")
    url = f"https://github.com/{slug}/settings/keys/new"
    if _confirm("Open your repo's 'Add deploy key' page now?", default=True):
        _open_url(url)
    print(f"\n{C_BOLD}2.{C_RESET} On that page ({C_YELLOW}{url}{C_RESET}):")
    print("     • Title: anything, e.g. 'Viclix'")
    print("     • Key: paste the public key printed above")
    print(f"     • {C_YELLOW}Leave 'Allow write access' UNCHECKED{C_RESET} — Viclix only needs to read")
    print("     • Click 'Add key'")
    ans = _ask("\nPress Enter once you've added it (or type 'skip' to use a token instead): ")
    if (ans or '').strip().lower() == 'skip':
        return None
    logger.info("Deploy key ready — Viclix will clone this repo over SSH.")
    return private


def _wizard_repo_access(cfg, slug):
    """Let the user choose how Viclix reads a private repo. Returns
    (github_token, ssh_key) with exactly one set — or (None, None) if cancelled."""
    print(f"\n{C_BOLD}Giving Viclix access to {slug}{C_RESET}")
    print(f"{slug} is private, so Viclix needs permission to clone it. What each option is for:")
    have_global = bool(cfg.get('github_token'))
    options, actions = [], []
    if have_global:
        print(f"  • {C_GREEN}Your connected token{C_RESET}: reuse the GitHub token you already set up.")
        print("    Simplest — works for every repo that token can reach.")
        options.append("Use my connected GitHub token")
        actions.append('global')
    print(f"  • {C_GREEN}Deploy key (SSH){C_RESET}: a key that works for THIS repo only, read-only.")
    print("    Safest — if it ever leaked, only this one repo is exposed; revoke it in one click.")
    print("    Limitation: read-only and this repo only (can't create repos).")
    options.append("Deploy key (SSH) — scoped to this repo only, most secure")
    actions.append('ssh')
    print(f"  • {C_GREEN}A token just for this repo{C_RESET}: a GitHub token used only here, without")
    print("    changing your global one. Good when this repo is under a different account/org.")
    options.append("A GitHub token just for this repo (doesn't change your global one)")
    actions.append('token')

    idx = _menu("How should Viclix access this repo?", options)
    if idx is None:
        return None, None
    choice = actions[idx]
    if choice == 'global':
        tok = cfg.get('github_token')
        if _github_repo_accessible(f"https://github.com/{slug}", tok) is False:
            print(f"{C_YELLOW}Your connected token can't read {slug} — let's set up access for it.{C_RESET}")
            return _wizard_github_token(cfg, slug, save_global=False), None
        return tok, None
    if choice == 'ssh':
        key = _wizard_ssh_deploy_key(slug)
        if key:
            return None, key
        print("No problem — let's use a token instead.")
    return _wizard_github_token(cfg, slug, save_global=False), None


def _wizard_project(cfg, pre_ghtoken=None):
    """Steps 2–3: pick the code source, sort out access, gather config. Returns a
    namespace for cmd_init, or None if the user cancelled."""
    print(f"\n{C_BOLD}Step 2 · Your project's code{C_RESET}")
    print("Viclix deploys straight from a Git repo: your code lives on GitHub and")
    print("Viclix pulls it to build and run. Let's point it at the right repo.")
    cwd = os.getcwd()
    name = os.path.basename(cwd)
    origin = git_remote_url('origin')
    github_token = (pre_ghtoken or '').strip() or cfg.get('github_token')
    ssh_key = None
    repo = None                 # None → let cmd_init use origin or create one
    check_url = None            # the URL we validate access against

    if origin:
        print(f"\nFound a git remote here: {C_YELLOW}{strip_credentials(origin)}{C_RESET}")
        print("(that's the GitHub repo this folder already pushes to)")
        idx = _menu("What are we deploying?", [
            f"This repo ({strip_credentials(origin)}) — deploy what's here",
            "A different repo — I'll paste another URL",
        ])
        if idx is None:
            return None
        if idx == 0:
            check_url = origin
        else:
            repo = _ask("Paste the repo URL (e.g. https://github.com/owner/repo): ")
            if not repo:
                return None
            check_url = repo
    else:
        print("\nThis folder has no git remote yet — Viclix has nowhere to pull from.")
        idx = _menu("What are we deploying?", [
            "Create a new private GitHub repo and push this folder — I'll set it up",
            "An existing repo — I'll paste its URL",
        ])
        if idx is None:
            return None
        if idx == 1:
            repo = _ask("Paste the repo URL (e.g. https://github.com/owner/repo): ")
            if not repo:
                return None
            check_url = repo
        # idx == 0 → check_url stays None (cmd_init will create the repo)

    # Access. Creating a repo needs a token (only a token can make + push a new
    # repo). An existing private repo can use a deploy key OR a token.
    slug = _github_repo_slug(check_url) if check_url else None
    if check_url is None:
        # Create a new repo. This always needs a token (a deploy key can't create
        # repos). Reuse the connected one if there is one; otherwise ask.
        if github_token:
            print(f"\nI'll create a new private repo for '{name}' with your connected GitHub "
                  "token and push this folder to it.")
            # Catch a token that can read but not create here, while we can still
            # ask for a better one — not halfway through init.
            if _github_can_create_repos(github_token) is False:
                print(f"{C_RED}That token can't create repos "
                      f"(missing Administration: Read and write).{C_RESET}")
                github_token = _wizard_github_token(cfg, None, save_global=False, need_create=True)
                # Don't silently swap the credential every other repo relies on.
                if github_token and _confirm("Make this your global token too (replaces the "
                                             "current one)?", default=True):
                    cfg['github_token'] = github_token
                    save_config(cfg)
        else:
            print("\nCreating a new repo needs a GitHub token — only a token can make and push "
                  "one for you (a deploy key can't create repos).")
            github_token = _wizard_github_token(cfg, None)
        if not github_token:
            logger.info("Cancelled — no GitHub token provided.")
            return None
    elif slug:
        already = bool(github_token and _github_repo_accessible(check_url, github_token))
        if not already and _confirm(f"Is {slug} private?", default=True):
            github_token, ssh_key = _wizard_repo_access(cfg, slug)
            if not github_token and not ssh_key:
                logger.info("Cancelled — no repo access was set up.")
                return None
    # else: a non-GitHub URL — assume it clones without credentials (public/other host).

    # Step 3 · configuration (all optional — press Enter to accept the default).
    print(f"\n{C_BOLD}Step 3 · Configuration{C_RESET}")
    print("A few optional settings. Press Enter to accept each default.")
    print(f"{C_CYAN}Runtime{C_RESET}: the stack Viclix builds. 'auto' inspects your repo and picks")
    print("  FastAPI / Node / static / etc. Leave it on auto unless you want to force one.")
    runtime = _ask("  Runtime [auto]: ", default='auto')
    env_file = None
    if os.path.exists(os.path.join(cwd, '.env')):
        print(f"\n{C_CYAN}Environment{C_RESET}: found a .env here. Uploading it makes those variables")
        print("  available to your app on Viclix (same KEY=VALUE lines, kept private).")
        if _confirm("  Upload this .env as the project's environment?", default=True):
            env_file = '.env'
    print(f"\n{C_CYAN}Database{C_RESET}: does your app need one?")
    print("  • none — no database  • sqlite — a file in your repo")
    print("  • shared/dedicated postgres — a managed Postgres Viclix provisions")
    db_idx = _menu("Database?", ["none", "sqlite (file in the repo)",
                                  "shared postgres", "dedicated postgres"])
    db = {0: 'none', 1: 'sqlite', 2: 'shared', 3: 'dedicated'}.get(db_idx, 'none')

    return _blank_init_args(name=name, repo=repo, github_token=github_token,
                            ssh_key=ssh_key, runtime=(runtime or 'auto'),
                            env_file=env_file, db=db)


def _print_cheatsheet():
    g = C_GREEN
    print(f"\n{C_BOLD}You're set. Handy commands:{C_RESET}")
    print(f"  {g}viclix logs-build{C_RESET}      watch the build")
    print(f"  {g}viclix logs-app{C_RESET}        app logs")
    print(f"  {g}viclix deploy{C_RESET}          push + fast redeploy")
    print(f"  {g}viclix deploy --full{C_RESET}   full rebuild (needed when env / PORT / DB change)")
    print(f"  {g}viclix run{C_RESET}             run the app locally")
    print(f"  {g}viclix status{C_RESET}          health & URL")
    print(f"  {g}viclix delete{C_RESET}          remove this project")


def cmd_setup(args):
    """Sign in and (optionally) connect GitHub. This finishes at sign-in — it
    does NOT create a project. To create + deploy one, run 'viclix deploy' in
    your project folder. Safe to re-run."""
    print(f"\n{C_BOLD}{C_CYAN}Welcome to Viclix.{C_RESET} Let's get you signed in.")
    if not _interactive():
        logger.error("Signing in here is interactive. Instead run:\n"
                     "    viclix login --token <ACCOUNT_TOKEN>")
        sys.exit(1)

    cfg = load_config(required=False)

    # Step 1 · sign in (skip if already signed in).
    if not cfg.get('account_token'):
        cfg = _wizard_login(cfg, pre_token=getattr(args, 'token', None))
    else:
        _ok, who = _verify_account(cfg['base_url'], cfg['account_token'])
        logger.info(f"Already signed in{who}." if who else "Already signed in.")

    # Offer to connect GitHub globally (makes creating/cloning repos frictionless).
    _wizard_connect_github(cfg)

    # Done — sign-in only. Point them to deploy for the project part.
    print(f"\n{C_BOLD}You're all set.{C_RESET} In your project folder, run "
          f"{C_GREEN}viclix deploy{C_RESET} to create it on Viclix and ship it.")


def _deploy_first_time(args, cfg):
    """First `viclix deploy` in a folder that isn't a Viclix project yet: guide
    the user through connecting a repo, then create + deploy it (via cmd_init)."""
    if not _interactive():
        logger.error("This folder isn't a Viclix project yet. Run 'viclix deploy' in a "
                     "terminal to set it up, or create it non-interactively with\n"
                     "    viclix init --repo <URL> [--github-token <PAT> | --ssh-key-file <KEY>]")
        sys.exit(1)
    print(f"\n{C_BOLD}{C_CYAN}This folder isn't on Viclix yet.{C_RESET} Let's set it up and deploy it.")
    ns = _wizard_project(cfg, pre_ghtoken=getattr(args, 'github_token', None))
    if ns is None:
        return
    # Let flags passed to `viclix deploy` win over the wizard's collected config.
    for attr in ('name', 'branch', 'db', 'env_file', 'env', 'static', 'cache_max_age',
                 'sqlite_file', 'sqlite_path', 'build', 'output', 'github_token'):
        val = getattr(args, attr, None)
        if val is not None:
            setattr(ns, attr, val)
    if getattr(args, 'runtime', 'auto') not in (None, 'auto'):
        ns.runtime = args.runtime
    cmd_init(ns, cfg)
    _print_cheatsheet()


# ── Delete a project (viclix delete) ────────────────────────────────────────
def cmd_delete(args):
    """Permanently delete this repo's Viclix project (token-authed)."""
    cfg = load_config(required=True)
    proj = get_project_data() or {}
    token, project_id = resolve_auth(args, proj)
    if not token or not project_id:
        logger.error("No linked project here. Run this inside a repo set up with "
                     "'viclix init' / 'viclix link', or pass --project-key.")
        sys.exit(1)

    label = strip_credentials(proj.get('project_url') or '') or project_id
    if not getattr(args, 'yes', False):
        print(f"{C_RED}{C_BOLD}This permanently deletes {label} and all its data. "
              f"It cannot be undone.{C_RESET}")
        confirm = _ask(f"Type the project id ({C_YELLOW}{project_id}{C_RESET}) to confirm: ")
        if confirm != project_id:
            logger.info("Cancelled — nothing was deleted.")
            return

    url = _tok_url(cfg['base_url'], 'projects/delete', token, project_id) + "&confirm=true"
    res = _api_post(url)
    if res.status_code != 200:
        logger.error(f"Delete failed: {_explain_api_error(res)}")
        sys.exit(1)
    logger.info(f"Deleted {label}.")
    # Drop the now-dangling local link so the folder isn't half-connected.
    vf = os.path.join(os.getcwd(), '.viclix')
    try:
        if os.path.exists(vf):
            os.remove(vf)
            logger.info("Removed local .viclix.")
    except Exception:
        pass


# ── Link an existing project (viclix link) ──────────────────────────────────
def _norm_repo(url):
    """Normalize a repo URL for comparison: https, no creds, no trailing / or
    .git, lowercased. Avoids str.removesuffix for older Pythons."""
    u = normalize_to_https(strip_credentials(url or '')).strip().rstrip('/').lower()
    if u.endswith('.git'):
        u = u[:-4]
    return u


def cmd_link(args):
    """Link the current repo to an EXISTING Viclix project (writes project_id).

    Two ways:
      • viclix link                  → interactive: pick from your account's
        projects (the one matching this repo's git remote is pre-selected).
      • viclix link --project-key K  → link via a project key someone shared
        (works across accounts; stores the key in .viclix).
    """
    cfg = load_config(required=False)
    base_url = cfg['base_url']
    key = getattr(args, 'project_key', None) or getattr(args, 'api_key', None)

    # ── Shared project key: validate it and adopt the project it points to. ──
    if key:
        try:
            res = requests.get(f"{base_url}projects", params={'token': key}, timeout=15)
        except requests.RequestException as e:
            logger.error(f"Could not reach Viclix: {e}")
            sys.exit(1)
        if res.status_code != 200:
            logger.error(f"That project key was rejected: {_err_detail(res)}")
            sys.exit(1)
        p = res.json()
        save_project_data(api_key=key, project_url=p.get('project_url'),
                          runtime=p.get('runtime'), project_id=p.get('id'))
        logger.info(f"Linked to '{p.get('name')}' — using the shared project key.")
        return

    # ── Interactive: list the account's projects and pick one. ──
    account = cfg.get('account_token')
    if not account:
        logger.error("Run 'viclix login' first, or pass --project-key to link with a shared key.")
        sys.exit(1)
    try:
        res = requests.get(f"{base_url}projects/list", params={'token': account}, timeout=15)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix: {e}")
        sys.exit(1)
    if res.status_code != 200:
        logger.error(f"Could not list your projects: {_err_detail(res)}")
        sys.exit(1)
    projects = res.json().get('projects') or []
    if not projects:
        logger.error("Your account has no projects yet. Run 'viclix init' to create one.")
        sys.exit(1)

    remote = _norm_repo(git_remote_url('origin'))

    def _matches(p):
        return bool(remote) and _norm_repo(p.get('repo_url')) == remote

    # Pre-select when exactly one project matches this repo's remote.
    matches = [p for p in projects if _matches(p)]
    chosen = None
    if len(matches) == 1:
        cand = matches[0]
        try:
            ans = input(f"{C_CYAN}This repo matches '{cand['name']}'. Link to it? [Y/n]: {C_RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print(); sys.exit(1)
        if ans not in ('n', 'no'):
            chosen = cand

    if chosen is None:
        opts = []
        for p in projects:
            tag = f"  {C_GREEN}← matches this repo{C_RESET}" if _matches(p) else ""
            opts.append(f"{p['name']}  {C_YELLOW}({strip_credentials(p.get('repo_url') or '—')}){C_RESET}{tag}")
        idx = _menu("Which project do you want to link?", opts)
        if idx is None:
            logger.info("Cancelled.")
            return
        chosen = projects[idx]
        # Guard: if the repo doesn't match, make the user confirm — this is how
        # "si el repo hace match procede, si no no" is enforced without blocking
        # a deliberate cross-repo link.
        if remote and not _matches(chosen):
            try:
                ans = input(f"{C_YELLOW}'{chosen['name']}' points at a different repo "
                            f"({strip_credentials(chosen.get('repo_url') or '—')}). Link anyway? [y/N]: {C_RESET}").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print(); sys.exit(1)
            if ans not in ('y', 'yes'):
                logger.info("Cancelled.")
                return

    save_project_data(project_url=chosen.get('project_url'),
                      runtime=chosen.get('runtime'), project_id=chosen['id'])
    logger.info(f"Linked to '{chosen['name']}' (id {chosen['id']}). Auth uses your account token.")


# ── Local run (viclix run / viclix local) ───────────────────────────────────
def _venv_paths(root):
    """Return (python_exe, venv_dir) for an existing venv, or (None, None)."""
    sub = 'Scripts' if os.name == 'nt' else 'bin'
    exe = 'python.exe' if os.name == 'nt' else 'python'
    for name in ('.venv', 'venv', 'env'):
        py = os.path.join(root, name, sub, exe)
        if os.path.exists(py):
            return py, os.path.join(root, name)
    return None, None


def _create_venv(root):
    venv_dir = os.path.join(root, '.venv')
    logger.info("No virtualenv found — creating .venv ...")
    res = subprocess.run([sys.executable, '-m', 'venv', venv_dir])
    if res.returncode != 0:
        logger.error("Failed to create the virtualenv.")
        sys.exit(1)
    sub = 'Scripts' if os.name == 'nt' else 'bin'
    exe = 'python.exe' if os.name == 'nt' else 'python'
    return os.path.join(venv_dir, sub, exe), venv_dir


def _file_md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            h.update(chunk)
    return h.hexdigest()


def _ensure_requirements(py, venv_dir, fresh):
    """Install requirements.txt only when it changed (md5 cached in the venv)."""
    req = 'requirements.txt'
    if not os.path.exists(req):
        if fresh:
            logger.warning("No requirements.txt found — nothing to install.")
        return
    digest = _file_md5(req)
    stamp = os.path.join(venv_dir, '.viclix_reqs_md5')
    previous = None
    if os.path.exists(stamp):
        try:
            with open(stamp) as f:
                previous = f.read().strip()
        except Exception:
            previous = None
    if previous == digest and not fresh:
        logger.info("Requirements unchanged — skipping install.")
        return
    logger.info("Installing requirements ...")
    res = subprocess.run([py, '-m', 'pip', 'install', '-r', req])
    if res.returncode != 0:
        logger.error("pip install failed.")
        sys.exit(1)
    try:
        with open(stamp, 'w') as f:
            f.write(digest)
    except Exception:
        pass


def _detect_app(root, override):
    if override:
        return override
    for rel, imp in (('main.py', 'main:app'),
                     (os.path.join('app', 'main.py'), 'app.main:app'),
                     ('app.py', 'app:app')):
        if os.path.exists(os.path.join(root, rel)):
            return imp
    return 'main:app'


def _has_module(py, module):
    return subprocess.run([py, '-c', f'import {module}'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def _free_port(start, host='127.0.0.1'):
    """First free port at or after `start` — lets several apps run at once.

    No SO_REUSEADDR on purpose: on Windows it lets you bind a port that's
    already in use, which would defeat the whole check.
    """
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind((host, port))
                return port
            except OSError:
                continue
    return start


def _authorize_viclix_db(base_url, token, project_id=None):
    """Whitelist this machine's IP on the project's Viclix DB and return its URL."""
    if not token:
        logger.error("--viclix-db needs a Viclix project — run inside a repo set up with 'viclix init'.")
        return None
    # Detect our public IP so the server whitelists the right one (it also
    # falls back to the request IP if we can't).
    ip = None
    try:
        ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
    except requests.RequestException:
        ip = None
    try:
        res = requests.post(f"{base_url}projects/db/authorize",
                            params=_tok_params(token, project_id), json={"ip": ip}, timeout=20)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix to authorize the database: {e}")
        return None
    if res.status_code != 200:
        try:
            detail = res.json().get("detail") or res.text
        except Exception:
            detail = res.text
        logger.error(f"Viclix DB authorization failed: {detail}")
        return None
    data = res.json()
    logger.info(f"Authorized {data.get('ip')} on Viclix DB '{data.get('db_name')}' — DATABASE_URL injected.")
    logger.debug(f"DB authorize response: {data}")
    return data.get("database_url")


# On Windows, asyncio defaults to the ProactorEventLoop, which async DB drivers
# (psycopg/psycopg3, asyncpg) refuse to run on. Linux (where Viclix runs) uses
# the Selector loop, so this never bites in production. We launch uvicorn through
# this tiny module and force the Selector loop:
#   - uvicorn >=0.36 ignores the event-loop policy and uses a loop factory, so we
#     hand it the SelectorEventLoop factory via the `loop` import string;
#   - older uvicorn respects the policy, so we set that instead.
# Both paths also cover --reload workers (they inherit the loop config / re-import).
_WIN_UVICORN_LAUNCHER = '''\
import os, sys, json, asyncio, uvicorn
sys.path.insert(0, os.getcwd())
if __name__ == "__main__":
    opts = json.loads(sys.argv[1])
    if sys.platform == "win32":
        try:
            from uvicorn.config import LOOP_FACTORIES  # noqa: F401  (uvicorn >=0.36)
            opts["loop"] = "asyncio:SelectorEventLoop"
        except Exception:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    uvicorn.run(opts.pop("app"), **opts)
'''


def _uvicorn_command(py, app_str, host, port, args):
    """Build the uvicorn launch command.

    On Windows we go through a launcher that forces the Selector event loop
    (async Postgres drivers can't use the default Proactor loop). Elsewhere it's
    a plain `python -m uvicorn`.
    """
    if os.name == 'nt':
        os.makedirs(config_home(), exist_ok=True)
        launcher = os.path.join(config_home(), '_uvicorn_win.py')
        with open(launcher, 'w', encoding='utf-8') as f:
            f.write(_WIN_UVICORN_LAUNCHER)
        opts = {"app": app_str, "host": host, "port": port}
        if args.reload:
            opts["reload"] = True
        if args.workers:
            opts["workers"] = args.workers
        if args.log_level:
            opts["log_level"] = args.log_level
        return [py, launcher, json.dumps(opts)]

    cmd = [py, '-m', 'uvicorn', app_str, '--host', host, '--port', str(port)]
    if args.reload:
        cmd.append('--reload')
    if args.workers:
        cmd += ['--workers', str(args.workers)]
    if args.log_level:
        cmd += ['--log-level', args.log_level]
    return cmd


# ── Local env overrides (cookie fix + per-project local_env) ────────────────
# Prod ships values that break on localhost. The classic one: a session cookie
# scoped to Domain=.viclix.com is *rejected* by the browser at localhost:9100,
# so you can't log in locally. We fix that for `viclix run`/`local` without ever
# touching the .env: os.environ overrides the app's dotenv, so setting these in
# the child's environment wins. Two layers:
#   1. auto — blank the well-known cookie-domain vars (host-only cookie) and turn
#      the Secure flag off, but only for vars the project actually defines.
#   2. manual — a per-project `local_env` map (edit via `viclix config run`) for
#      anything with a non-standard name or extra tweaks. Always wins over auto.
COOKIE_DOMAIN_VARS = (
    "COOKIE_DOMAIN", "SESSION_COOKIE_DOMAIN", "CSRF_COOKIE_DOMAIN",
    "AUTH_COOKIE_DOMAIN", "JWT_COOKIE_DOMAIN",
)
COOKIE_SECURE_VARS = (
    "COOKIE_SECURE", "SESSION_COOKIE_SECURE", "CSRF_COOKIE_SECURE", "SECURE_COOKIES",
)


def _local_env_keys():
    """KEYs defined in os.environ + the local .env, for cookie-fix detection.

    Reading (not injecting) the .env is fine — we only override a handful of
    specific cookie vars, never the whole file, so the app still parses its own
    .env exactly as it does on Viclix.
    """
    keys = set(os.environ.keys())
    path = os.path.join(os.getcwd(), '.env')
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    s = line.strip()
                    if not s or s.startswith('#') or '=' not in s:
                        continue
                    k = s.split('=', 1)[0].strip()
                    if k.startswith('export '):
                        k = k[len('export '):].strip()
                    if k:
                        keys.add(k)
        except OSError:
            pass
    return keys


def _apply_local_env(env, proj, args):
    """Mutate `env` with the cookie auto-fix and the project's local_env map."""
    cookie_fix = proj.get('cookie_fix', True) and not getattr(args, 'no_cookie_fix', False)
    if cookie_fix:
        defined = _local_env_keys()
        for var in COOKIE_DOMAIN_VARS:
            if var in defined:
                logger.debug(f"cookie-fix: blanking {var} (host-only cookie for localhost)")
                env[var] = ''
        for var in COOKIE_SECURE_VARS:
            if var in defined:
                logger.debug(f"cookie-fix: {var} -> false (cookies over http://localhost)")
                env[var] = 'false'
    for key, value in (proj.get('local_env') or {}).items():
        env[str(key)] = str(value)
        logger.debug(f"local_env: {key} = {value!r}")


def cmd_run(args):
    """Run the project's FastAPI app locally: venv + deps + uvicorn."""
    root = os.getcwd()
    cfg = load_config(required=False)
    proj = get_project_data() or {}

    # Gate on runtime. Newer projects store it in .viclix at init. For older
    # ones, ask the API once (using the project api_key) and cache it. If we
    # truly can't tell (offline / no project), assume fastapi and proceed.
    # "auto" means the server hasn't resolved the stack yet (it does so at the
    # first deploy and writes it back). Treat auto/empty as unresolved and ask
    # the API for the concrete runtime, caching it in .viclix.
    runtime = proj.get('runtime')
    if runtime in (None, '', 'auto') and proj.get('api_key'):
        try:
            res = requests.get(f"{cfg['base_url']}projects",
                               params={'token': proj['api_key']}, timeout=10)
            if res.status_code == 200:
                fetched = res.json().get('runtime')
                if fetched and fetched != 'auto':
                    runtime = fetched
                    save_project_data(proj['api_key'], proj.get('project_url'), runtime=runtime)
                    logger.info(f"Detected runtime '{runtime}' from Viclix (cached in .viclix).")
        except requests.RequestException:
            pass
    # Unknown/auto → assume FastAPI and proceed; a resolved non-fastapi stack
    # can't be run locally by this command.
    if runtime and runtime not in ('fastapi', 'auto'):
        logger.error(f"'viclix run' currently supports FastAPI projects only (this one is '{runtime}').")
        sys.exit(1)

    # 1. venv — reuse if present (fast: just check the interpreter), else create.
    py, venv_dir = _venv_paths(root)
    fresh = False
    if py:
        logger.info(f"Using virtualenv: {os.path.relpath(venv_dir, root)}")
    else:
        py, venv_dir = _create_venv(root)
        fresh = True

    # 2. requirements — install only when changed.
    _ensure_requirements(py, venv_dir, fresh)

    # 3. env — the app loads its OWN .env from disk, exactly like on Viclix
    #    (pydantic/python-dotenv handle quoting, inline comments, etc.). We do
    #    NOT re-parse or inject the whole file. We only override a few specific
    #    vars that break on localhost: the cookie-domain fix + any per-project
    #    local_env overrides (see _apply_local_env), plus the remote database URL
    #    when the user asks for --viclix-db.
    env = os.environ.copy()
    _apply_local_env(env, proj, args)
    if args.viclix_db:
        db_token, db_pid = resolve_auth(args, proj)
        db_url = _authorize_viclix_db(cfg['base_url'], db_token, db_pid)
        if db_url:
            env['DATABASE_URL'] = db_url
            env['DATABASE_WRITE_URL'] = db_url
            logger.debug(f"Injected DATABASE_URL       = {db_url}")
            logger.debug(f"Injected DATABASE_WRITE_URL = {db_url}")

    # 4. server + entrypoint.
    app_str = _detect_app(root, args.app)
    if _has_module(py, 'uvicorn'):
        server = 'uvicorn'
    elif _has_module(py, 'gunicorn'):
        server = 'gunicorn'
    else:
        logger.info("No ASGI server installed — adding uvicorn ...")
        subprocess.run([py, '-m', 'pip', 'install', 'uvicorn'])
        server = 'uvicorn'

    # 5. port — default 9100, auto-increment unless the user pinned one.
    host = args.host or '127.0.0.1'
    port = args.port if args.port else _free_port(9100, host)

    display_host = 'localhost' if host in ('127.0.0.1', '0.0.0.0') else host
    url = f"http://{display_host}:{port}"

    if server == 'uvicorn':
        cmd = _uvicorn_command(py, app_str, host, port, args)
    else:
        cmd = [py, '-m', 'gunicorn', app_str, '-b', f'{host}:{port}',
               '-k', 'uvicorn.workers.UvicornWorker']
        if args.workers:
            cmd += ['-w', str(args.workers)]
        if args.reload:
            cmd.append('--reload')

    logger.info(f"Starting {server} → {C_CYAN}{url}{C_RESET}  (app: {app_str})")

    # Open the browser BEFORE the server starts, as requested.
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    logger.debug(f"launch: {' '.join(str(c) for c in cmd)}")
    try:
        subprocess.run(cmd, env=env)
    except KeyboardInterrupt:
        print()


# ── Download (viclix download) ──────────────────────────────────────────────
def _err_detail(res):
    try:
        return res.json().get('detail') or res.text
    except Exception:
        return res.text


def _confirm_overwrite(dest, assume_yes):
    """Return True if it's OK to write `dest`. Prompts before clobbering."""
    if not os.path.exists(dest):
        return True
    if assume_yes:
        return True
    try:
        ans = input(f"{C_YELLOW}{dest} already exists. Overwrite? [y/N]: {C_RESET}").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans in ('y', 'yes')


def _download_env(base_url, token, project_id, out, assume_yes):
    try:
        res = requests.get(f"{base_url}projects/env", params=_tok_params(token, project_id), timeout=20)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix: {e}")
        return
    if res.status_code != 200:
        logger.error(f"Failed to fetch .env: {_err_detail(res)}")
        return
    data = res.json()
    if not data.get('exists'):
        logger.warning("This project has no stored .env.")
        return
    dest = out or '.env'
    if not _confirm_overwrite(dest, assume_yes):
        logger.info(f"Skipped {dest}.")
        return
    with open(dest, 'w', encoding='utf-8', newline='\n') as f:
        f.write(data.get('content', ''))
    logger.info(f"Saved {dest}.")


def _download_sqlite(base_url, token, project_id, remote, out, assume_yes):
    dest = out or os.path.basename(remote.rstrip('/')) or 'app.db'
    if not _confirm_overwrite(dest, assume_yes):
        logger.info(f"Skipped {dest}.")
        return
    try:
        res = requests.get(f"{base_url}projects/sqlite/download",
                           params=_tok_params(token, project_id, path=remote), timeout=120)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix: {e}")
        return
    if res.status_code != 200:
        logger.error(f"Failed to download SQLite: {_err_detail(res)}")
        return
    with open(dest, 'wb') as f:
        f.write(res.content)
    logger.info(f"Saved {dest} ({len(res.content)} bytes).")


def _download_file(base_url, token, project_id, remote, out, assume_yes):
    dest = out or os.path.basename(remote.rstrip('/')) or 'download'
    if not _confirm_overwrite(dest, assume_yes):
        logger.info(f"Skipped {dest}.")
        return
    try:
        res = requests.get(f"{base_url}projects/file",
                           params=_tok_params(token, project_id, path=remote), timeout=120)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix: {e}")
        return
    if res.status_code != 200:
        logger.error(f"Failed to download file: {_err_detail(res)}")
        return
    with open(dest, 'wb') as f:
        f.write(res.content)
    logger.info(f"Saved {dest} ({len(res.content)} bytes).")


def _print_download_options():
    print(
        f"{C_BOLD}{C_CYAN}viclix download{C_RESET} — pull files from your Viclix project\n\n"
        f"  {C_GREEN}viclix download env{C_RESET}              the stored .env   (also: {C_GREEN}viclix env{C_RESET})\n"
        f"  {C_GREEN}viclix download sqlite{C_RESET}           a cached SQLite file (path via {C_GREEN}--sqlite-path{C_RESET})\n"
        f"  {C_GREEN}viclix download file{C_RESET} {C_YELLOW}PATH{C_RESET}         a specific file from the project\n\n"
        f"  {C_GREEN}--out{C_RESET} {C_YELLOW}PATH{C_RESET}       local destination (single target)\n"
        f"  {C_GREEN}-y{C_RESET}, {C_GREEN}--yes{C_RESET}         overwrite existing files without asking\n\n"
        f"examples:\n"
        f"  viclix download env\n"
        f"  viclix download sqlite --sqlite-path ./data/app.db\n"
        f"  viclix download file requirements.txt --out ./reqs.txt\n"
    )


def cmd_env(args):
    """Shortcut for 'viclix download env'."""
    cfg = load_config(required=False)
    token, project_id = require_auth(args)
    _download_env(cfg['base_url'], token, project_id, args.out, args.yes)


def cmd_download(args):
    """Download a SQLite file, a specific file, or the .env from the project."""
    # Accept both a positional sub-target (download env / sqlite / file PATH)
    # and the --dotenv/--sqlite/--file flags.
    target = (getattr(args, 'target', None) or '').strip().lower().lstrip('.')

    want_env = args.dotenv or target in ('env', 'dotenv')
    want_sqlite = args.sqlite or target == 'sqlite'
    file_path = args.file
    if target == 'file' and not file_path:
        file_path = getattr(args, 'target_arg', None)
    want_file = bool(file_path)

    if target and target not in ('env', 'dotenv', 'sqlite', 'file'):
        logger.error(f"Unknown download target '{target}'.")
        _print_download_options()
        return
    if target == 'file' and not file_path:
        logger.error("Specify a path: viclix download file <path>")
        return

    # Nothing requested → just show what can be downloaded (don't touch anything).
    if not (want_env or want_sqlite or want_file):
        _print_download_options()
        return

    cfg = load_config(required=False)
    base_url = cfg['base_url']
    token, project_id = require_auth(args)

    # --out only makes sense for a single target.
    single = (bool(want_env) + bool(want_sqlite) + bool(want_file)) == 1
    if args.out and not single:
        logger.warning("--out is ignored when downloading multiple things.")
    out = args.out if single else None

    if want_file:
        _download_file(base_url, token, project_id, file_path, out, args.yes)
    if want_sqlite:
        _download_sqlite(base_url, token, project_id, (args.sqlite_path or './data/app.db'), out, args.yes)
    if want_env:
        _download_env(base_url, token, project_id, out, args.yes)


# ── Config (viclix config …) ────────────────────────────────────────────────
def _save_project_data_full(data):
    """Overwrite the whole .viclix file (used by the interactive config menus)."""
    viclix_file = os.path.join(os.getcwd(), '.viclix')
    with open(viclix_file, 'w') as f:
        json.dump(data, f, indent=2)


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


def _config_add_override(proj):
    print(f"\n{C_CYAN}Enter an override as {C_BOLD}KEY=VALUE{C_RESET}{C_CYAN}. "
          f"Leave the value empty to blank the var (e.g. {C_GREEN}COOKIE_DOMAIN={C_RESET}{C_CYAN}).{C_RESET}")
    try:
        raw = input(f"{C_CYAN}KEY=VALUE: {C_RESET}").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if '=' not in raw:
        logger.warning("Expected KEY=VALUE.")
        return
    key, value = raw.split('=', 1)
    key = key.strip()
    if not key:
        logger.warning("Empty key — nothing added.")
        return
    proj.setdefault('local_env', {})[key] = value.strip()
    logger.info(f"Set {key}={value.strip()!r} for local runs.")


def _config_remove_override(proj):
    local_env = proj.get('local_env') or {}
    if not local_env:
        logger.info("No overrides to remove.")
        return
    keys = list(local_env.keys())
    idx = _menu("Remove which override?", [f"{k}={v}" for k, v in local_env.items()])
    if idx is None:
        return
    removed = keys[idx]
    del local_env[removed]
    proj['local_env'] = local_env
    logger.info(f"Removed {removed}.")


def cmd_config_run(args):
    """Interactive setup for local runs (`viclix run` / `viclix local`).

    Configures the cookie auto-fix and per-project env overrides, all stored in
    the project's .viclix file. No arguments to memorize — just pick from menus.
    """
    proj = get_project_data()
    if proj is None:
        logger.error("No .viclix file found here. Run 'viclix init' first.")
        sys.exit(1)

    while True:
        cookie_fix = proj.get('cookie_fix', True)
        local_env = proj.get('local_env') or {}

        print(f"\n{C_BOLD}{C_CYAN}Local run configuration{C_RESET}  "
              f"(stored in {C_YELLOW}.viclix{C_RESET})")
        state = f"{C_GREEN}ON{C_RESET}" if cookie_fix else f"{C_RED}OFF{C_RESET}"
        print(f"  Cookie auto-fix : {state}  "
              f"{C_YELLOW}(blanks COOKIE_DOMAIN etc. so login works on localhost){C_RESET}")
        if local_env:
            print("  Env overrides   :")
            for k, v in local_env.items():
                print(f"      {C_CYAN}{k}{C_RESET}={C_GREEN}{v}{C_RESET}")
        else:
            print(f"  Env overrides   : {C_YELLOW}(none){C_RESET}")

        choice = _menu("What do you want to do?", [
            f"Turn cookie auto-fix {'OFF' if cookie_fix else 'ON'}",
            "Add / edit an env override",
            "Remove an env override",
            "Clear all env overrides",
            "Save & exit",
        ])
        if choice is None or choice == 4:
            break
        if choice == 0:
            proj['cookie_fix'] = not cookie_fix
        elif choice == 1:
            _config_add_override(proj)
        elif choice == 2:
            _config_remove_override(proj)
        elif choice == 3:
            proj['local_env'] = {}
            logger.info("Cleared all env overrides.")
        _save_project_data_full(proj)  # persist after every change

    _save_project_data_full(proj)
    logger.info("Saved local-run settings to .viclix.")


def _print_config_options():
    print(
        f"{C_BOLD}{C_CYAN}viclix config{C_RESET} — configure per-project settings "
        f"(stored in {C_YELLOW}.viclix{C_RESET})\n\n"
        f"  {C_GREEN}viclix config run{C_RESET}     interactive setup for local runs — "
        f"cookie fix + env overrides used by {C_GREEN}viclix run{C_RESET} / {C_GREEN}local{C_RESET}\n"
    )


COMMANDS = [
    'setup', 'update',
    'login', 'logout', 'whoami',
    'init', 'link', 'open', 'disconnect', 'deploy', 'hotfix', 'run', 'local', 'download', 'env', '.env',
    'config', 'delete',
    'status', 'info', 'rebuild', 'restart', 'sleep', 'start',
    'logs-build', 'logs-app', 'exec', 'pip-install',
    'deploy-status', 'diagnostics', 'health', 'metrics',
    'db', 'db-schema', 'env-keys', 'fs', 'read',
    'deploys', 'rollback', 'requests', 'events',
    'describe', 'env-set', 'env-unset', 'packages',
    'probe', 'domains', 'scaling',
    'db-snapshot', 'db-snapshots', 'db-restore', 'db-exec',
    'agent-run', 'agent-status', 'agents',
]

EPILOG = """\
commands:
  setup / login              sign in to your Viclix account (and connect GitHub)
  update                     upgrade the Viclix CLI to the latest version
  logout                     sign out (remove the stored account token)
  disconnect                 clear all stored CLI credentials (account + GitHub); keeps .viclix
  whoami                     show who you're signed in as
  init                       register the current repo as a project and deploy it
  link                       link this repo to an existing project (interactive; --project-key to share)
  open                       open this project's dashboard page in your browser (viclix open [id])
  delete                     permanently delete this project (asks to confirm)
  run / local                run the FastAPI app locally (venv + deps + uvicorn on :9100)
  config run                 interactive local-run setup (cookie fix + env overrides)
  env / .env                 download the project's stored .env
  download                   show download options (--dotenv / --sqlite / --file)
  deploy                     first run: set up + create the project; after: push + rebuild (--full)
  hotfix                     fast git sync into the running container (-i reinstalls deps)
  status / info              inspect the project
  logs-build / logs-app      tail the build / app logs
  restart / sleep / start    lifecycle controls
  exec --cmd "..."           run a command inside the container
  pip-install                install packages in the container

examples:
  viclix setup
  viclix login
  viclix link
  viclix open
  viclix init --db sqlite --env-file .env --static /static/
  viclix init --runtime static --build "npm run build" --output dist
  viclix config run
  viclix run --reload
  viclix download --sqlite
  viclix deploy --full
  viclix hotfix -i

Flags are grouped above by the command that uses them.
docs: https://dashboard.viclix.com/projects/new
"""


def build_parser():
    parser = ColorHelpParser(
        prog='viclix',
        description='Viclix CLI — deploy and manage Viclix projects from your terminal.',
        usage='viclix <command> [options]',
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument('command', nargs='?', choices=COMMANDS, metavar='command',
                        help='the command to run (see the list below)')
    # Optional sub-target + arg, used by 'download' (e.g. download file PATH).
    parser.add_argument('target', nargs='?', metavar='target', help=argparse.SUPPRESS)
    parser.add_argument('target_arg', nargs='?', metavar='arg', help=argparse.SUPPRESS)
    parser.add_argument('--api-key', help='project API key (per-project override)')
    parser.add_argument('--project-key', help='link/use a shared project key (viclix link / init)')

    g_auth = parser.add_argument_group('auth (login)')
    g_auth.add_argument('--token', help='Viclix account API token')
    g_auth.add_argument('--base-url', help='override the Viclix API base URL')

    g_proj = parser.add_argument_group('project setup (init / deploy)')
    g_proj.add_argument('--name', help='project name (defaults to the repo folder)')
    g_proj.add_argument('--repo', help='repo URL (defaults to the git remote)')
    g_proj.add_argument('--github-token',
                        help="GitHub token to clone/create a private repo (init). Classic: 'repo' "
                             "scope. Fine-grained: Repository access 'All repositories' + "
                             "Administration RW (to create) + Contents RW")
    g_proj.add_argument('--ssh-key-file', help='path to a private SSH deploy key to clone a private repo (init)')
    g_proj.add_argument('--branch', help='branch (defaults to the current branch)')
    g_proj.add_argument('--runtime', default='auto',
                        help='runtime: auto|fastapi|static|node|nextjs|go|django|docker (default: auto — detects the stack from your repo)')
    g_proj.add_argument('--build', help='override the build step (static/node/go), e.g. "npm run build"')
    g_proj.add_argument('--output', help='static publish dir, e.g. dist (static runtime; default: dist with a build, else repo root)')
    g_proj.add_argument('--db', choices=['none', 'sqlite', 'upload', 'shared', 'dedicated'],
                        help='database mode for init')
    g_proj.add_argument('--sqlite-path', help='SQLite path in the repo, e.g. ./data/app.db')
    g_proj.add_argument('--sqlite-file', help='local .db to upload (implies --db upload)')
    g_proj.add_argument('--env-file', help='path to a .env file to load')
    g_proj.add_argument('--env', action='append', metavar='KEY=VALUE',
                        help='set an env var (repeatable)')
    g_proj.add_argument('--static', help='static path prefix, e.g. /static/')
    g_proj.add_argument('--cache-max-age', type=int, help='static cache max-age (seconds)')

    g_ship = parser.add_argument_group('deploy / hotfix')
    g_ship.add_argument('--full', action='store_true', help='force a full rebuild deploy')
    g_ship.add_argument('-nr', '--no-restart', '--no-reload', action='store_true',
                        help="don't restart after a hotfix")
    g_ship.add_argument('-i', '--install', action='store_true',
                        help='reinstall requirements during a hotfix')
    g_ship.add_argument('-git', '--install-git', action='store_true',
                        help='reinstall only git requirements during a hotfix')
    g_ship.add_argument('-w', '--wait', action='store_true',
                        help='deploy: poll deploy/status until the build finishes '
                             '(running/failed), then exit non-zero on failure')
    g_ship.add_argument('--wait-timeout', type=int, default=600,
                        help='deploy --wait: overall cap in seconds (default: 600, '
                             'generous for a full rebuild)')
    g_ship.add_argument('--stream', action='store_true',
                        help='deploy --wait: stream the full live build log instead '
                             'of compact phase/progress lines')

    g_logs = parser.add_argument_group('logs / inspect')
    g_logs.add_argument('--tail', type=int, default=0,
                        help='logs: return only the last N lines')
    g_logs.add_argument('--grep', help='logs: keep only lines matching this regex')
    g_logs.add_argument('--level', help='logs: keep only lines at/above this level '
                                        '(error/warning/info)')
    g_logs.add_argument('--limit', type=int, default=0,
                        help='deploys/requests/events: max rows to return')
    g_logs.add_argument('--method', help='probe: HTTP method (default GET)')
    g_logs.add_argument('--data', help='probe: request body')
    g_logs.add_argument('--mode', help='agent-run: plan(read-only, default)|manual|auto_edit|full')

    g_run = parser.add_argument_group('local run (run / local)')
    g_run.add_argument('--port', type=int,
                       help='port to serve on (default: 9100, auto-increments if busy)')
    g_run.add_argument('--host', help='host to bind (default: 127.0.0.1)')
    g_run.add_argument('--reload', action='store_true', help='restart on file changes')
    g_run.add_argument('--workers', type=int, help='number of worker processes')
    g_run.add_argument('--log-level', help='server log level (e.g. info, debug)')
    g_run.add_argument('--app', help='ASGI app import string (default: auto-detect, e.g. main:app)')
    g_run.add_argument('--viclix-db', action='store_true',
                       help="whitelist your IP and connect to the project's Viclix database")
    g_run.add_argument('--no-browser', action='store_true', help="don't open the browser")
    g_run.add_argument('--no-cookie-fix', action='store_true',
                       help="don't blank COOKIE_DOMAIN etc. for this local run")

    g_dl = parser.add_argument_group('download')
    g_dl.add_argument('--dotenv', action='store_true', help="download the project's stored .env")
    g_dl.add_argument('--sqlite', action='store_true', help='download a cached SQLite file (--sqlite-path)')
    g_dl.add_argument('--file', help='download a specific file from the project by path')
    g_dl.add_argument('--out', help='local destination path (single-target downloads)')
    g_dl.add_argument('-y', '--yes', action='store_true',
                      help='overwrite existing files without asking')

    g_exec = parser.add_argument_group('exec / pip-install')
    g_exec.add_argument('--cmd', help='command to run in the container (exec)')
    g_exec.add_argument('--timeout', type=int, default=10, help='exec timeout 1-30 (default: 10)')
    g_exec.add_argument('--workdir', default='/app', help='working dir for exec (default: /app)')
    g_exec.add_argument('--packages', help='space-separated packages (pip-install)')

    return parser


def main():
    print(ASCII_ART)
    parser = build_parser()
    args = parser.parse_args()

    # No command → guided setup on the very first run (not signed in); once
    # signed in, a status banner followed by the help.
    if not args.command:
        cfg = load_config(required=False)
        if not cfg.get('account_token'):
            cmd_setup(args)
        else:
            _print_status_header(cfg)
            parser.print_help()
        return

    logger.info(f"Executing command: {args.command}")

    # Self-update — no account or config needed.
    if args.command == 'update':
        cmd_update(args)
        return

    # Guided setup manages its own sign-in, so it runs before load_config.
    if args.command == 'setup':
        cmd_setup(args)
        return

    # ── Auth commands don't need an existing session ──
    # `viclix login` opens the guided setup; with --token / --github-token it
    # stays the scriptable credential-saver (and the github-only update).
    if args.command == 'login':
        if getattr(args, 'token', None) or getattr(args, 'github_token', None):
            cmd_login(args)
        else:
            cmd_setup(args)
        return
    if args.command == 'logout':
        cmd_logout(args)
        return
    if args.command == 'disconnect':
        cmd_disconnect(args)
        return

    # Local dev — no account token needed.
    if args.command in ('run', 'local'):
        cmd_run(args)
        return

    # Link an existing project (interactive needs an account token, but the
    # --project-key path works standalone — so handle it before load_config).
    if args.command == 'link':
        cmd_link(args)
        return
    # `viclix init --project-key K` means "adopt this shared key", not "create".
    if args.command == 'init' and getattr(args, 'project_key', None):
        cmd_link(args)
        return

    # Open the project's dashboard page — local only (project_id from .viclix).
    if args.command == 'open':
        cmd_open(args)
        return

    # Project-scoped, uses the .viclix api_key (no account token needed).
    if args.command == 'download':
        cmd_download(args)
        return
    if args.command in ('env', '.env'):
        cmd_env(args)
        return

    # Per-project config menus (local, no account token).
    if args.command == 'config':
        sub = (args.target or '').strip().lower()
        if sub in ('run', 'local'):
            cmd_config_run(args)
        else:
            _print_config_options()
        return

    # Delete self-loads config (account token required inside).
    if args.command == 'delete':
        cmd_delete(args)
        return

    cfg = load_config(required=True)
    base_url = cfg['base_url']
    account_token = cfg['account_token']

    if args.command == 'whoami':
        cmd_whoami(args)
        return

    if args.command == 'init':
        cmd_init(args, cfg)
        return

    # ── DEPLOY: Hotfix by default, full rebuild with --full ──
    if args.command == 'deploy':
        proj_data = get_project_data()
        # Not a Viclix project yet (no project_id/key in .viclix) → the first
        # deploy sets it up: connect a repo, create it, and deploy. Subsequent
        # deploys fall through to the normal push + rebuild below.
        if not resolve_auth(args, proj_data or {})[0]:
            _deploy_first_time(args, cfg)
            return
        api_key, project_id = require_auth(args, proj_data or {})

        # Static sites are served from the built (nginx) image, not a live /app
        # bind mount — a fast redeploy can't change what's served, so always do
        # a full rebuild.
        runtime = (proj_data.get('runtime') if proj_data else None) or 'fastapi'
        if runtime == 'static' and not args.full:
            logger.info("Static site — using a full rebuild (fast deploy doesn't apply).")
            args.full = True

        # Apply manual-wizard-style setup to the existing project first, so the
        # rebuild triggered below picks it up.
        deploy_env = collect_env(args)
        if deploy_env:
            api_set_env(base_url, api_key, project_id, deploy_env)
        if args.static:
            api_set_static(base_url, api_key, project_id, args.static.strip(), args.cache_max_age)
        if args.sqlite_file:
            if not os.path.exists(args.sqlite_file):
                logger.error(f"SQLite file not found: {args.sqlite_file}")
                sys.exit(1)
            remote = (args.sqlite_path or '').strip() or ('./data/' + os.path.basename(args.sqlite_file))
            api_upload_sqlite(base_url, api_key, project_id, remote, args.sqlite_file)

        logger.info("Deploying: pull, commit, push, rebuild...")

        # 1. Pull first to avoid conflicts
        try:
            run_git(['pull', 'origin', 'main'], check=False, stream=True)
        except Exception as e:
            logger.warning(f"Git pull had issues, proceeding anyway: {e}")

        # 2. Add and check status
        run_git(['add', '.'], stream=True)
        run_git(['status'], stream=True)

        # 3. Commit if changes exist
        porcelain_status = run_git(['status', '--porcelain'], stream=False)
        if porcelain_status.stdout.strip():
            deploy_type = "Full Deploy" if args.full else "Fast Deploy"
            run_git(['commit', '-m', f"{deploy_type} update {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"], stream=True)
        else:
            logger.info("No changes to commit. Proceeding to push & rebuild.")

        # 4. Push
        run_git(['branch', '-M', 'main'], stream=False)
        run_git(['push', 'origin', 'main'], stream=True)
        print()

        url = _tok_url(base_url, 'projects/rebuild', api_key, project_id)
        if args.full:
            url += "&full=true"

        res = requests.post(url)
        if res.status_code == 200:
            deploy_msg = "full rebuild" if args.full else "fast rebuild"
            logger.info(f"Successfully triggered {deploy_msg} on Viclix.")
            res_json = res.json()
            if proj_data and proj_data.get('project_url') and 'project_url' not in res_json:
                res_json['project_url'] = proj_data['project_url']
            print()
            print_json(res_json)
            print()
            if getattr(args, 'wait', False):
                code = _wait_for_deploy(
                    base_url, api_key, project_id,
                    stream=getattr(args, 'stream', False),
                    timeout=getattr(args, 'wait_timeout', 600))
                sys.exit(code)
        else:
            logger.error(f"Failed to trigger rebuild: {res.text}")
            sys.exit(1)
        return

    # ── HOTFIX: Sync git repo directly ──
    if args.command == 'hotfix':
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})

        # A hotfix git-resets inside the container's /app — static sites are
        # served from the built nginx image (no /app, no git), so it can't work.
        runtime = (proj_data.get('runtime') if proj_data else None) or 'fastapi'
        if runtime == 'static':
            logger.error("hotfix isn't available for static sites — they're served from the built image, not a live /app. Use 'viclix deploy' instead.")
            sys.exit(1)

        logger.info("Executing Git hotfix: local pull, commit, push, then container fetch & reset...")

        # 1. Pull, add, commit, push locally
        try:
            run_git(['pull', 'origin', 'main'], check=False, stream=True)
        except Exception as e:
            logger.warning(f"Git pull had issues, proceeding anyway: {e}")

        run_git(['add', '.'], stream=True)

        porcelain_status = run_git(['status', '--porcelain'], stream=False)
        if porcelain_status.stdout.strip():
            run_git(['commit', '-m', f"Hotfix Git Sync {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"], stream=True)
        else:
            logger.info("No local changes to commit.")

        run_git(['branch', '-M', 'main'], stream=False)
        run_git(['push', 'origin', 'main'], stream=True)
        print()

        # 2. Reset inside container via Exec endpoint
        exec_url = _tok_url(base_url, 'projects/exec', api_key, project_id)
        logger.info("Resetting code inside container...")
        git_reset_cmd = "git fetch origin && git reset --hard origin/main"
        res = requests.post(exec_url, data={
            'command': git_reset_cmd,
            'timeout': '15',
            'workdir': '/app'
        })
        if res.status_code == 200:
            data = res.json()
            if data.get('stdout'):
                print(data['stdout'])
            if data.get('stderr'):
                print(data['stderr'], file=sys.stderr)
            logger.info("Git reset completed.")
        else:
            logger.error(f"Git reset exec failed: {res.text}")
            sys.exit(1)

        # 3. Pip install dependencies if requested
        if args.install or args.install_git:
            if args.install_git:
                logger.info("Installing ONLY git dependencies from requirements.txt...")
                install_cmd = "grep -i '^git+' requirements.txt | xargs -r pip install -U"
                res_install = requests.post(exec_url, data={'command': install_cmd, 'timeout': '30', 'workdir': '/app'})
            else:
                logger.info("Re-installing ALL requirements...")
                pip_url = _tok_url(base_url, 'projects/pip-install', api_key, project_id)
                res_install = requests.post(pip_url)

            if res_install.status_code == 200:
                logger.info("Requirements install triggered successfully.")
                print_json(res_install.json())
            else:
                logger.error(f"Requirements install failed: {res_install.text}")
                sys.exit(1)

        # 4. Optional restart
        if not args.no_restart:
            logger.info("Restarting application...")
            restart_url = _tok_url(base_url, 'projects/restart', api_key, project_id)
            res_restart = requests.post(restart_url)
            if res_restart.status_code == 200:
                logger.info("Application restarted successfully.")
            else:
                logger.error(f"Failed to restart application: {res_restart.text}")
        else:
            logger.info("Skipping container restart (--no-reload).")

        print(f"\n{C_GREEN}Hotfix sync completed successfully.{C_RESET}\n")
        return

    # ── EXEC: Run command in container ──
    if args.command == 'exec':
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})

        if not args.cmd:
            logger.error("Specify command with --cmd")
            sys.exit(1)

        url = _tok_url(base_url, 'projects/exec', api_key, project_id)
        res = requests.post(url, data={
            'command': args.cmd,
            'timeout': str(min(max(args.timeout, 1), 30)),
            'workdir': args.workdir
        })
        if res.status_code == 200:
            data = res.json()
            if data.get('stdout'):
                print(data['stdout'])
            if data.get('stderr'):
                print(data['stderr'], file=sys.stderr)
            print_json(data)
        else:
            logger.error(f"Exec failed: {res.text}")
            sys.exit(1)
        return

    # ── PIP-INSTALL: Install packages in container ──
    if args.command == 'pip-install':
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})

        url = _tok_url(base_url, 'projects/pip-install', api_key, project_id)
        data = {}
        if args.packages:
            data['packages'] = args.packages
        res = requests.post(url, data=data)
        if res.status_code == 200:
            print_json(res.json())
        else:
            logger.error(f"pip-install failed: {res.text}")
            sys.exit(1)
        return

    # ── DB: read-only SQL query (viclix db "SELECT ...") ──
    if args.command == 'db':
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        sql = (args.target or '').strip()
        if not sql:
            logger.error('Provide a query, e.g. viclix db "SELECT * FROM users LIMIT 5"')
            sys.exit(1)
        url = _tok_url(base_url, 'projects/db-query', api_key, project_id)
        res = requests.post(url, json={'sql': sql})
        if res.status_code == 200:
            print_json(res.json())
        else:
            logger.error(f"db query failed: {res.text}")
            sys.exit(1)
        return

    # ── FS: list a container dir (viclix fs /app) / read a file (viclix read path) ──
    if args.command in ('fs', 'read'):
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        target_path = (args.target or '').strip()
        if args.command == 'read':
            if not target_path:
                logger.error('Provide a file path, e.g. viclix read /app/main.py')
                sys.exit(1)
            url = _tok_url(base_url, 'projects/fs/read', api_key, project_id)
            url += f"&path={requests.utils.quote(target_path)}"
        else:
            url = _tok_url(base_url, 'projects/fs', api_key, project_id)
            url += f"&path={requests.utils.quote(target_path or '/app')}"
        res = requests.get(url)
        if res.status_code == 200:
            print_json(res.json())
        else:
            logger.error(f"fs failed: {res.text}")
            sys.exit(1)
        return

    # ── ROLLBACK: revert to a previous build version (viclix rollback <version>) ──
    if args.command == 'rollback':
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        version = (args.target or '').strip()
        if not version:
            logger.error('Provide a version, e.g. viclix rollback 20260809123000  (see: viclix deploys)')
            sys.exit(1)
        url = _tok_url(base_url, 'projects/rollback', api_key, project_id)
        res = requests.post(url, json={'version': version})
        if res.status_code == 200:
            print_json(res.json())
        else:
            logger.error(f"rollback failed: {res.text}")
            sys.exit(1)
        return

    # ── ENV-SET / ENV-UNSET: single env var (viclix env-set KEY=VALUE / env-unset KEY) ──
    if args.command in ('env-set', 'env-unset'):
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        target = (args.target or '').strip()
        if args.command == 'env-set':
            if '=' not in target:
                logger.error('Use KEY=VALUE, e.g. viclix env-set DEBUG=1')
                sys.exit(1)
            key, value = target.split('=', 1)
            url = _tok_url(base_url, 'projects/env/set', api_key, project_id)
            res = requests.post(url, json={'key': key.strip(), 'value': value})
        else:
            if not target:
                logger.error('Provide a key, e.g. viclix env-unset DEBUG')
                sys.exit(1)
            url = _tok_url(base_url, f'projects/env/{requests.utils.quote(target)}', api_key, project_id)
            res = requests.delete(url)
        if res.status_code == 200:
            print_json(res.json())
        else:
            logger.error(f"{args.command} failed: {res.text}")
            sys.exit(1)
        return

    # ── PROBE: smoke-test a route of the app (viclix probe /api/users) ──
    if args.command == 'probe':
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        path = (args.target or '/').strip()
        payload = {'path': path, 'method': (getattr(args, 'method', None) or 'GET')}
        if getattr(args, 'data', None):
            payload['body'] = args.data
        url = _tok_url(base_url, 'projects/probe', api_key, project_id)
        res = requests.post(url, json=payload)
        if res.status_code == 200:
            print_json(res.json())
        else:
            logger.error(f"probe failed: {res.text}")
            sys.exit(1)
        return

    # ── DB-RESTORE / DB-EXEC (write SQL) ──
    if args.command in ('db-restore', 'db-exec'):
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        target = (args.target or '').strip()
        if args.command == 'db-restore':
            if not target:
                logger.error('Provide a snapshot filename, e.g. viclix db-restore 20260810_101500.dump  (see: viclix db-snapshots)')
                sys.exit(1)
            url = _tok_url(base_url, 'projects/db/restore', api_key, project_id)
            res = requests.post(url, json={'filename': target})
        else:  # db-exec — the invocation is the confirmation
            if not target:
                logger.error('Provide SQL, e.g. viclix db-exec "UPDATE users SET active=true"')
                sys.exit(1)
            url = _tok_url(base_url, 'projects/db/execute', api_key, project_id)
            res = requests.post(url, json={'sql': target, 'confirm': True})
        if res.status_code == 200:
            print_json(res.json())
        else:
            logger.error(f"{args.command} failed: {res.text}")
            sys.exit(1)
        return

    # ── AGENT-RUN / AGENT-STATUS: headless coding agent ──
    if args.command in ('agent-run', 'agent-status'):
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        target = (args.target or '').strip()
        if args.command == 'agent-run':
            if not target:
                logger.error('Provide a goal, e.g. viclix agent-run "add a /health endpoint"')
                sys.exit(1)
            body = {'goal': target, 'mode': (getattr(args, 'mode', None) or 'plan')}
            url = _tok_url(base_url, 'projects/agent/runs', api_key, project_id)
            res = requests.post(url, json=body)
        else:  # agent-status <run_id>
            if not target:
                logger.error('Provide a run id, e.g. viclix agent-status <run_id>')
                sys.exit(1)
            url = _tok_url(base_url, f'projects/agent/runs/{target}', api_key, project_id)
            res = requests.get(url)
        if res.status_code in (200, 201):
            print_json(res.json())
        else:
            logger.error(f"{args.command} failed: {res.text}")
            sys.exit(1)
        return

    # ── Other commands (status, logs, etc.) ──
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})

    endpoints = {
        'status': ('GET', 'projects/status'),
        'info': ('GET', 'projects'),
        'rebuild': ('POST', 'projects/rebuild'),
        'restart': ('POST', 'projects/restart'),
        'sleep': ('POST', 'projects/sleep'),
        'start': ('POST', 'projects/start'),
        'logs-build': ('GET', 'projects/logs/build'),
        'logs-app': ('GET', 'projects/logs/app'),
        'deploy-status': ('GET', 'projects/deploy/status'),
        'diagnostics': ('GET', 'projects/diagnostics'),
        'health': ('GET', 'projects/health'),
        'metrics': ('GET', 'projects/metrics'),
        'db-schema': ('GET', 'projects/db/schema'),
        'env-keys': ('GET', 'projects/env/keys'),
        'deploys': ('GET', 'projects/deploys'),
        'requests': ('GET', 'projects/requests'),
        'events': ('GET', 'projects/events'),
        'describe': ('GET', 'projects/config'),
        'packages': ('GET', 'projects/packages'),
        'domains': ('GET', 'projects/domains'),
        'scaling': ('GET', 'projects/scaling'),
        'db-snapshot': ('POST', 'projects/db/snapshot'),
        'db-snapshots': ('GET', 'projects/db/snapshots'),
        'agents': ('GET', 'projects/agents'),
    }

    method, path = endpoints[args.command]
    url = _tok_url(base_url, path, api_key, project_id)

    # Log filters (server-side tail/grep/level) so we pull only what matters.
    if args.command in ('logs-app', 'logs-build'):
        if getattr(args, 'tail', 0):
            url += f"&tail={int(args.tail)}"
        if getattr(args, 'grep', None):
            url += f"&grep={requests.utils.quote(args.grep)}"
        if getattr(args, 'level', None):
            url += f"&level={requests.utils.quote(args.level)}"
    if args.command in ('deploys', 'requests', 'events') and getattr(args, 'limit', 0):
        url += f"&limit={int(args.limit)}"

    if method == 'GET':
        response = requests.get(url)
    else:
        response = requests.post(url)

    if response.status_code == 200:
        res_json = response.json()
        if proj_data and proj_data.get('project_url') and 'project_url' not in res_json:
            res_json['project_url'] = proj_data['project_url']
        print_json(res_json)
    else:
        logger.error(f"Error: {response.text}")
        sys.exit(1)


if __name__ == "__main__":
    main()

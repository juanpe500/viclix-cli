"""Global config (~/.viclix/config.json) and per-project state (.viclix), plus
the KEY=VALUE env parsing shared by init/deploy.

Imports: console (logger). Paths (config_home/CONFIG_PATH/LOGS_DIR) live in
console.py — import them from there if a function here needs them.

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:55        DEFAULT_BASE_URL
  # COPY cli.py:186-198   DEFAULT_GITIGNORE
  # COPY cli.py:202-226   load_config()
  # COPY cli.py:229-237   save_config()
  # COPY cli.py:241-251   get_project_data()
  # COPY cli.py:254-268   save_project_data()
  # COPY cli.py:2250-2255 _save_project_data_full()
  # COPY cli.py:782-804   parse_env_file()
  # COPY cli.py:807-816   collect_env()

After pasting, `python -m py_compile config.py` must pass.
"""
import os
import sys
import json

from .console import logger, config_home, CONFIG_PATH

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────


# ── Paths / config ──────────────────────────────────────────────────────────
# Global, per-user config — overridable with VICLIX_HOME for tests/CI.
DEFAULT_BASE_URL = "https://dashboard.viclix.com/api/v1/"


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

def _save_project_data_full(data):
    """Overwrite the whole .viclix file (used by the interactive config menus)."""
    viclix_file = os.path.join(os.getcwd(), '.viclix')
    with open(viclix_file, 'w') as f:
        json.dump(data, f, indent=2)



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

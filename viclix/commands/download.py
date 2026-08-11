"""Download commands: `env` / `.env` (fetch stored dotenv) and `download`
(--dotenv / --sqlite / --file), plus their helpers.

Imports: console, config, api.

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:2109-2122  _confirm_overwrite()
  # COPY cli.py:2123-2144  _download_env()
  # COPY cli.py:2145-2163  _download_sqlite()
  # COPY cli.py:2164-2182  _download_file()
  # COPY cli.py:2183-2196  _print_download_options()
  # COPY cli.py:2198-2203  cmd_env()
  # COPY cli.py:2205-2248  cmd_download()

After pasting, `python -m py_compile commands/download.py` must pass.
"""
import os
import requests

from ..console import logger, C_BOLD, C_CYAN, C_GREEN, C_YELLOW, C_RESET
from ..config import load_config
from ..api import _tok_params, require_auth, _err_detail

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────

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


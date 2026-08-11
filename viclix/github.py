"""GitHub integration: repo lookup/creation, token capability checks, deploy-key
generation, and the token-permission help text.

Imports: console (logger, colors), gitutils (normalize_to_https,
strip_credentials — used by _github_repo_slug).

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:394-447   get_github_repo()
  # COPY cli.py:524-526   GH_CLASSIC_TOKEN_URL, GH_FINEGRAINED_TOKEN_URL, GH_TOKEN_SETTINGS_URL
  # COPY cli.py:529-546   _print_github_token_help()
  # COPY cli.py:549-557   _github_username()
  # COPY cli.py:560-564   _github_repo_slug()
  # COPY cli.py:567-579   _github_repo_accessible()
  # COPY cli.py:582-600   _github_can_create_repos()
  # COPY cli.py:603-610   _github_missing_permission_hint()
  # COPY cli.py:613-634   _generate_deploy_key()

After pasting, `python -m py_compile github.py` must pass.
"""
import os
import re
import sys
import shutil
import tempfile
import subprocess
import requests

from .console import logger, C_BOLD, C_CYAN, C_YELLOW, C_RESET
from .gitutils import normalize_to_https, strip_credentials

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────


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

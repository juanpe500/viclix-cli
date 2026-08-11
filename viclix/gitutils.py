"""Git plumbing: run_git, remote/branch helpers, URL normalization, repo setup.

Imports: console (logger). No cycle — console is a leaf.

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:272-286   run_git()
  # COPY cli.py:289-295   git_remote_url()
  # COPY cli.py:298-304   git_current_branch()
  # COPY cli.py:307-318   normalize_to_https()
  # COPY cli.py:321-328   strip_credentials()
  # COPY cli.py:331-336   embed_token()
  # COPY cli.py:339-379   setup_git_repo()
  # COPY cli.py:382-391   push_existing()
  # COPY cli.py:1653-1659 _norm_repo()

After pasting, `python -m py_compile gitutils.py` must pass.
"""
import os
import subprocess
from datetime import datetime

from .console import logger, C_CYAN, C_RESET
from .config import DEFAULT_GITIGNORE

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────


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


# ── Link an existing project (viclix link) ──────────────────────────────────
def _norm_repo(url):
    """Normalize a repo URL for comparison: https, no creds, no trailing / or
    .git, lowercased. Avoids str.removesuffix for older Pythons."""
    u = normalize_to_https(strip_credentials(url or '')).strip().rstrip('/').lower()
    if u.endswith('.git'):
        u = u[:-4]
    return u

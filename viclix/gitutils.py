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
import sys
import json
import shutil
import tempfile
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


# ── Seeding an empty folder: default starter + `--template` clone ────────────
# These give `viclix init` something to deploy when the current folder has no
# code yet, so an empty init produces a working, git-backed box instead of a
# failed build. The normal no-remote path then creates the repo and pushes.

# Runtimes we can scaffold, in menu order: (id, label, one-line blurb). Each id
# matches a Viclix stack (shared/stacks.py) so the generated files satisfy that
# stack's build/detect contract — every starter binds 0.0.0.0:$PORT (default
# 8000), which is how Viclix reaches the app behind Traefik.
SCAFFOLD_RUNTIMES = [
    ('fastapi', 'FastAPI (Python)',   'app/main.py + requirements.txt'),
    ('static',  'Static site (HTML)', 'index.html served by nginx'),
    ('node',    'Node (http server)', 'package.json + server.js'),
    ('nextjs',  'Next.js (React)',    'app/ router + package.json (npm build)'),
    ('go',      'Go (net/http)',      'go.mod + main.go'),
    ('django',  'Django (gunicorn)',  'manage.py + config/ project'),
    ('docker',  'Docker (custom)',    'your own Dockerfile — full control'),
]
SCAFFOLD_RUNTIME_IDS = [r[0] for r in SCAFFOLD_RUNTIMES]

# Curated starter repos we host so users can pick one instead of hunting for a
# URL. Each entry: (ref, label, one-line blurb). `ref` is passed straight to
# apply_template (owner/repo or a full git URL), so adding a template here is a
# one-line change. Shown as a sub-menu under "Clone a template repo".
VICLIX_TEMPLATES = [
    ('juanpe500/fastapi-mega-template', 'FastAPI Mega Template',
     'batteries-included FastAPI starter'),
]

# The app name is injected into code files via this token (avoids str.format
# brace-escaping headaches in JS/Go/JSX). package.json/go.mod use a slug instead.
_APP = '__VICLIX_APP_NAME__'

_TPL_FASTAPI_MAIN = '''from fastapi import FastAPI

app = FastAPI(title="__VICLIX_APP_NAME__")


@app.get("/")
def root():
    return {"status": "ok", "message": "__VICLIX_APP_NAME__ is live on Viclix"}
'''

_TPL_STATIC_INDEX = '''<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>__VICLIX_APP_NAME__</title>
</head>
<body>
  <h1>__VICLIX_APP_NAME__</h1>
  <p>Your Viclix site is live. Edit index.html to get started.</p>
</body>
</html>
'''

_TPL_NODE_SERVER = '''const http = require('http');

const port = process.env.PORT || 8000;
const host = process.env.HOST || '0.0.0.0';

const server = http.createServer((req, res) => {
  res.writeHead(200, { 'Content-Type': 'application/json' });
  res.end(JSON.stringify({ status: 'ok', message: '__VICLIX_APP_NAME__ is live on Viclix' }));
});

server.listen(port, host, () => {
  console.log(`Listening on ${host}:${port}`);
});
'''

_TPL_NEXT_LAYOUT = '''export const metadata = { title: '__VICLIX_APP_NAME__' };

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
'''

_TPL_NEXT_PAGE = '''export default function Page() {
  return (
    <main style={{ fontFamily: 'system-ui', padding: '2rem' }}>
      <h1>__VICLIX_APP_NAME__</h1>
      <p>Your Next.js app is live on Viclix.</p>
    </main>
  );
}
'''

_TPL_GO_MAIN = '''package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"
)

func main() {
	port := os.Getenv("PORT")
	if port == "" {
		port = "8000"
	}
	http.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]string{
			"status":  "ok",
			"message": "__VICLIX_APP_NAME__ is live on Viclix",
		})
	})
	log.Printf("listening on :%s", port)
	log.Fatal(http.ListenAndServe("0.0.0.0:"+port, nil))
}
'''

_TPL_DJANGO_MANAGE = '''#!/usr/bin/env python
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    from django.core.management import execute_from_command_line
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
'''

_TPL_DJANGO_SETTINGS = '''import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = ["django.contrib.staticfiles"]
MIDDLEWARE = ["django.middleware.common.CommonMiddleware"]

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
TEMPLATES = []

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
'''

_TPL_DJANGO_URLS = '''from django.http import JsonResponse
from django.urls import path


def index(request):
    return JsonResponse({"status": "ok", "message": "__VICLIX_APP_NAME__ is live on Viclix"})


urlpatterns = [path("", index)]
'''

_TPL_DJANGO_WSGI = '''import os
from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
application = get_wsgi_application()
'''

_TPL_DOCKERFILE = '''# Custom runtime — you own this image. Viclix builds this Dockerfile as-is and
# injects PORT / HOST at runtime, so listen on 0.0.0.0:$PORT. Replace the CMD
# below with your app; the placeholder just serves index.html.
FROM python:3.12-slim
WORKDIR /app
COPY . .
EXPOSE 8000
CMD ["sh", "-c", "python -m http.server ${PORT:-8000} --bind 0.0.0.0"]
'''


def _render(tpl, name):
    """Inject the display name into a code template."""
    return tpl.replace(_APP, name)


def _slugify(name, fallback='app'):
    """Lowercase, hyphenated, alnum-only — safe for an npm/go module name."""
    slug = ''.join(c if c.isalnum() else '-' for c in (name or '').lower())
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug.strip('-') or fallback


def _write_file(path, content):
    """Write a starter file, creating parent dirs, never clobbering an existing one."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    if not os.path.exists(path):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)


def _dir_has_code(exclude=('.viclix',)):
    """True if the current directory holds anything other than the ignorable
    bookkeeping files. Used to keep the empty-init scaffold and `--template`
    from ever writing over an existing project."""
    return any(e not in exclude for e in os.listdir('.'))


def resolve_template_ref(ref):
    """Turn a --template value into a clonable git URL, or None if unrecognized.

    Accepts a full URL (https:// or git@…) or an `owner/repo` GitHub shorthand.
    Built-in named starters can be added here later (map a short name → URL)."""
    ref = (ref or '').strip()
    if not ref:
        return None
    if '://' in ref or ref.startswith('git@'):
        return normalize_to_https(ref)
    # owner/repo shorthand → GitHub. Reject anything that looks like a local path.
    if '/' in ref and ' ' not in ref and '\\' not in ref and not ref.startswith('.'):
        return f"https://github.com/{ref}" if ref.endswith('.git') else f"https://github.com/{ref}.git"
    return None


def apply_template(ref, branch=None, github_token=None):
    """Clone a template repo into the (empty) current dir, minus its history.

    Leaves the files in place with NO .git, so the caller's normal no-remote
    path initializes a fresh repo and pushes it as the project's own. Exits with
    a clear message if the folder isn't empty (never clobbers existing code)."""
    url = resolve_template_ref(ref)
    if not url:
        logger.error(f"Unrecognized template '{ref}'. Use a full git URL "
                     "(https://… or git@…) or an owner/repo shorthand.")
        sys.exit(1)
    if os.path.exists('.git') or _dir_has_code():
        logger.error("--template needs an empty directory, but this folder already "
                     "has files. Run it in a fresh folder (e.g. mkdir myapp && cd myapp).")
        sys.exit(1)

    clone_url = embed_token(url, github_token) if (github_token and 'github.com' in url) else url
    tmp = tempfile.mkdtemp(prefix='viclix-tpl-')
    try:
        clone_args = ['clone', '--depth', '1']
        if branch:
            clone_args += ['-b', branch]
        clone_args += [clone_url, tmp]
        logger.info(f"Cloning template {strip_credentials(url)} …")
        run_git(clone_args)
        # Copy everything except the template's own git history into the project.
        for entry in os.listdir(tmp):
            if entry == '.git':
                continue
            src = os.path.join(tmp, entry)
            dst = os.path.join('.', entry)
            if os.path.isdir(src):
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    logger.info("Template applied — your project starts with a fresh git history.")


def write_default_starter(name, runtime='auto'):
    """Write a minimal, runnable starter for `runtime` into an empty folder so
    `viclix init` has real code to deploy. Never overwrites existing files.

    Each starter satisfies its Viclix stack's build/detect contract and binds
    0.0.0.0:$PORT. Unknown/'auto' runtimes fall back to FastAPI."""
    rt = (runtime or 'auto').strip().lower()
    slug = _slugify(name)

    if rt == 'static':
        _write_file('index.html', _render(_TPL_STATIC_INDEX, name))
        return

    if rt == 'node':
        pkg = {
            "name": slug, "version": "1.0.0", "private": True,
            "scripts": {"start": "node server.js"},
        }
        _write_file('package.json', json.dumps(pkg, indent=2) + "\n")
        _write_file('server.js', _render(_TPL_NODE_SERVER, name))
        return

    if rt == 'nextjs':
        pkg = {
            "name": slug, "version": "1.0.0", "private": True,
            "scripts": {"dev": "next dev", "build": "next build", "start": "next start"},
            "dependencies": {"next": "^14.2.5", "react": "^18.3.1", "react-dom": "^18.3.1"},
        }
        _write_file('package.json', json.dumps(pkg, indent=2) + "\n")
        _write_file(os.path.join('app', 'layout.js'), _render(_TPL_NEXT_LAYOUT, name))
        _write_file(os.path.join('app', 'page.js'), _render(_TPL_NEXT_PAGE, name))
        return

    if rt == 'go':
        _write_file('go.mod', f"module {slug}\n\ngo 1.22\n")
        _write_file('main.go', _render(_TPL_GO_MAIN, name))
        return

    if rt == 'django':
        _write_file('requirements.txt', "Django>=4.2,<5.1\ngunicorn\n")
        _write_file('manage.py', _TPL_DJANGO_MANAGE)
        _write_file(os.path.join('config', '__init__.py'), '')
        _write_file(os.path.join('config', 'settings.py'), _TPL_DJANGO_SETTINGS)
        _write_file(os.path.join('config', 'urls.py'), _render(_TPL_DJANGO_URLS, name))
        _write_file(os.path.join('config', 'wsgi.py'), _TPL_DJANGO_WSGI)
        return

    if rt == 'docker':
        _write_file('Dockerfile', _TPL_DOCKERFILE)
        _write_file('index.html', _render(_TPL_STATIC_INDEX, name))
        return

    # Default (covers 'auto', 'fastapi'): a working FastAPI app the builder detects.
    _write_file('requirements.txt', "fastapi\nuvicorn[standard]\n")
    _write_file(os.path.join('app', '__init__.py'), '')
    _write_file(os.path.join('app', 'main.py'), _render(_TPL_FASTAPI_MAIN, name))

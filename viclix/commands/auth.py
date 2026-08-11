"""Auth & account commands: login, logout, whoami, disconnect, update, and the
guided `setup` wizard (with its _wizard_* helpers).

Imports from lower layers: console, config, api, github, gitutils. The wizard
drives project creation, so it also imports cmd_init from .project (one-way:
auth -> project; project never imports auth).

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:865-934    cmd_login()
  # COPY cli.py:937-954    cmd_logout()
  # COPY cli.py:957-972    cmd_update()
  # COPY cli.py:975-988    cmd_disconnect()
  # COPY cli.py:991-1003   cmd_whoami()
  # COPY cli.py:1201-1216  _verify_account()
  # COPY cli.py:1219-1238  _print_status_header()
  # COPY cli.py:1553-1562  _print_cheatsheet()
  # COPY cli.py:1241-1252  _blank_init_args()
  # COPY cli.py:1255-1296  _wizard_login()
  # COPY cli.py:1297-1317  _wizard_connect_github()
  # COPY cli.py:1318-1374  _wizard_github_token()
  # COPY cli.py:1375-1407  _wizard_ssh_deploy_key()
  # COPY cli.py:1408-1447  _wizard_repo_access()
  # COPY cli.py:1448-1552  _wizard_project()
  # COPY cli.py:1565-1591  cmd_setup()

After pasting, `python -m py_compile commands/auth.py` must pass.
"""
import os
import sys
import json
import argparse
import subprocess
import requests

from ..console import (
    logger, print_json, CONFIG_PATH,
    _ask, _confirm, _interactive, _menu, _open_url, _mask_secret,
    C_CYAN, C_GREEN, C_YELLOW, C_RED, C_BOLD, C_RESET,
)
from ..config import load_config, save_config, DEFAULT_BASE_URL
from ..api import _dashboard_base
from ..github import (
    _print_github_token_help, _github_username, _github_repo_slug,
    _github_repo_accessible, _github_can_create_repos, _generate_deploy_key,
    GH_TOKEN_SETTINGS_URL, GH_CLASSIC_TOKEN_URL, GH_FINEGRAINED_TOKEN_URL,
)
from ..gitutils import git_remote_url, strip_credentials

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────



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


"""Project lifecycle commands: init, link, delete, and the two open-in-browser
commands — `open` (live app URL) and `dash` (dashboard project page).

Naming change (idea 3, hard switch):
  - cmd_dash = the OLD cmd_open body verbatim (opens {dash}/project/<id>).
  - cmd_open = NEW: resolves the live app URL via GET projects/config and opens
    that (custom domain aware). Built in the feature phase (idea 2) — a stub
    marker is left below; do not paste anything for it during migration.

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:1006-1027  cmd_open()  →  RENAME to `cmd_dash`
  # COPY cli.py:1030-1191  cmd_init()
  # COPY cli.py:1617-1651  cmd_delete()
  # COPY cli.py:1653-1659  _norm_repo()   (also referenced by gitutils copy — put ONE copy in gitutils.py and import it here; do NOT duplicate)
  # COPY cli.py:1662-1753  cmd_link()

  # NEW (feature phase, idea 2): def cmd_open(args) — resolve live URL, open it.

After pasting, `python -m py_compile commands/project.py` must pass.
"""
import os
import sys
import requests

from ..console import (
    logger,
    _ask, _interactive, _menu, _open_url, _mask_url_credentials,
    C_CYAN, C_GREEN, C_YELLOW, C_RED, C_BOLD, C_RESET,
)
from ..config import (
    load_config, get_project_data, save_project_data, collect_env,
)
from ..api import (
    _tok_url, _tok_params, _api_post, resolve_auth, _dashboard_base,
    api_upload_sqlite, api_rebuild, _explain_api_error, _err_detail,
)
from ..github import (
    get_github_repo, _github_repo_slug, _github_repo_accessible,
    _print_github_token_help,
)
from ..gitutils import (
    git_remote_url, git_current_branch, setup_git_repo, push_existing,
    normalize_to_https, strip_credentials, embed_token, _norm_repo,
    apply_template, write_default_starter, _dir_has_code, SCAFFOLD_RUNTIMES,
    VICLIX_TEMPLATES,
)

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────



def cmd_open(args):
    """Open this project's LIVE app URL (its real site) in the browser.

    Resolves the current public URL from the server (GET projects/config), which
    prefers a verified custom domain over the default subdomain — the same value
    the dashboard's 'Visit App' link uses. Falls back to the URL cached in
    .viclix when offline / not linked (warned as possibly stale)."""
    proj = get_project_data() or {}
    token, project_id = resolve_auth(args, proj)

    url = None
    if token and project_id:
        cfg = load_config(required=False)
        try:
            r = requests.get(f"{cfg['base_url']}projects/config",
                             params=_tok_params(token, project_id), timeout=15)
            if r.status_code == 200:
                url = (r.json() or {}).get('project_url')
            else:
                logger.warning(f"Couldn't resolve the live URL from the server ({_explain_api_error(r)}).")
        except requests.RequestException as e:
            logger.warning(f"Couldn't reach Viclix to resolve the live URL: {e}")

    if not url:
        url = proj.get('project_url')
        if url:
            logger.warning("Using the URL cached in .viclix — it may be stale "
                           "(renamed slug / new custom domain won't show).")

    if not url:
        logger.error("Couldn't find this project's live URL. Is the repo linked? "
                     "Try 'viclix status', or open the dashboard with 'viclix dash'.")
        sys.exit(1)

    logger.info(f"Opening {url}")
    if not _open_url(url):
        logger.warning("Couldn't launch a browser automatically — open the URL above manually.")


def cmd_dash(args):
    """Open this project's DASHBOARD page (/project/<id>) in the browser.

    Local-only: reads project_id from .viclix and the dashboard origin from the
    saved config (falling back to the default). No account token required — the
    dashboard prompts for its own login. An explicit id may be passed as an
    argument (``viclix dash <project_id>``) to open any project."""
    project_id = (getattr(args, 'target', None) or '').strip()
    if not project_id:
        proj = get_project_data() or {}
        project_id = proj.get('project_id')
    if not project_id:
        logger.error("This repo isn't linked to a Viclix project. Run 'viclix init' "
                     "or 'viclix link' here, or pass a project id: viclix dash <id>.")
        sys.exit(1)

    cfg = load_config(required=False)
    dash = _dashboard_base(cfg.get('base_url'))
    url = f"{dash}/project/{project_id}"
    logger.info(f"Opening {url}")
    if not _open_url(url):
        logger.warning("Couldn't launch a browser automatically — open the URL above manually.")


def _choose_empty_start(default_runtime='fastapi'):
    """Empty-folder interactive start: clone a template repo, or scaffold a fresh
    runtime. Returns ('template', ref) or ('runtime', runtime_id).
    Non-interactive, or no selection → ('runtime', fastapi)."""
    if not _interactive():
        return ('runtime', default_runtime)
    template_opt = "Clone a template repo (our presets, or paste your own)"
    labels = [template_opt] + [
        f"{label}  {C_YELLOW}({blurb}){C_RESET}" for _id, label, blurb in SCAFFOLD_RUNTIMES
    ]
    idx = _menu("Empty folder — how do you want to start?", labels)
    if idx is None:
        logger.info(f"No selection — using {default_runtime}.")
        return ('runtime', default_runtime)
    if idx == 0:
        ref = _choose_template_ref()
        if not ref:
            logger.info(f"No template given — using {default_runtime}.")
            return ('runtime', default_runtime)
        return ('template', ref)
    return ('runtime', SCAFFOLD_RUNTIMES[idx - 1][0])


def _choose_template_ref():
    """Pick a template to clone: one of our curated presets, or a repo the user
    pastes. Returns the ref (owner/repo or git URL) for apply_template, or None
    if nothing was chosen. Assumes an interactive session (caller gates)."""
    paste_opt = "Paste a git URL / owner-repo"
    labels = [
        f"{label}  {C_YELLOW}({blurb}){C_RESET}" for _ref, label, blurb in VICLIX_TEMPLATES
    ] + [paste_opt]
    idx = _menu("Clone a template repo — pick one:", labels)
    if idx is None:
        return None
    if idx < len(VICLIX_TEMPLATES):
        return VICLIX_TEMPLATES[idx][0]
    ref = _ask(f"{C_YELLOW}Template repo (git URL or owner/repo): {C_RESET}")
    return ref.strip() if ref else None


def _print_init_next_steps():
    """Post-init cheat sheet: the commands users reach for most, from this folder."""
    print(f"\n{C_BOLD}{C_CYAN}Handy commands from this folder:{C_RESET}")
    rows = [
        ("viclix dash",   "Open the project dashboard (just opened for you)"),
        ("viclix open",   "Open your live app in the browser"),
        ("viclix run",    "Run the app locally"),
        ("viclix deploy", "Push your changes and redeploy"),
        ("viclix env",    "Manage environment variables"),
    ]
    for cmd, desc in rows:
        print(f"  {C_GREEN}{cmd:<13}{C_RESET} {desc}")


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

    # ── Seed local code when there's nothing to deploy yet ──────────────────
    # Both paths leave files in the folder with NO remote, so the no-remote
    # branch below creates the repo and pushes — the project is git-backed like
    # any other.
    #   --template <ref>      → clone that starter into this empty folder
    #   empty folder, no repo → drop in a minimal working app (the chosen
    #                           default: scaffold + git-back, never a broken
    #                           empty deploy)
    # NOTE (pending): a repo-less `viclix sandbox` (the server's create-sandbox
    # primitive, git-less, source lives in the container) was deliberately left
    # OUT — every real path here is git-backed and sandbox doesn't fit that loop
    # yet. Revisit if a throwaway/no-git use case shows up.
    template = (getattr(args, 'template', None) or '').strip()
    is_empty = (not git_remote_url('origin') and not os.path.exists('.git')
                and not _dir_has_code())
    if template:
        apply_template(template, args.branch, github_token)
    elif is_empty:
        # An explicit --runtime is honored; otherwise ask: template or fresh starter.
        runtime = (args.runtime or 'auto').strip().lower()
        if runtime != 'auto':
            write_default_starter(name, runtime)
            args.runtime = runtime
            logger.info(f"Empty folder — added a minimal {runtime} starter to deploy.")
        else:
            kind, value = _choose_empty_start()
            if kind == 'template':
                apply_template(value, args.branch, github_token)
            else:
                write_default_starter(name, value)
                args.runtime = value  # persist the concrete pick into the create payload
                logger.info(f"Empty folder — added a minimal {value} starter to deploy.")

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

    # After an interactive init, open the dashboard project page (same target as
    # `viclix dash`) and leave a short cheat sheet — skipped in CI/piped runs,
    # where there's no TTY and no browser to launch.
    if _interactive():
        dash = _dashboard_base(cfg.get('base_url'))
        dash_url = f"{dash}/project/{project_id}"
        logger.info(f"Opening your project dashboard: {dash_url}")
        if not _open_url(dash_url):
            logger.warning(f"Couldn't open a browser automatically — visit {dash_url}")
        _print_init_next_steps()


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


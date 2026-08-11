"""Deploy & hotfix commands.

cmd_deploy and cmd_hotfix currently live INLINE inside cli.py `main()` as
`if args.command == ...:` blocks — they are NOT functions yet. To migrate each:
  1. Wrap the block body in `def cmd_deploy(args, cfg): ...` / `def cmd_hotfix(...)`.
  2. De-indent the body one level.
  3. Drop the `if args.command == '...':` line; keep every `return`.
  4. Add `base_url = cfg['base_url']` at the top (the block read `base_url` /
     `account_token` from main()'s locals).
No lines of logic are lost — only the `if` header becomes a `def`.

MIGRATION (paste from cli.py):

  # COPY cli.py:1592-1616  _deploy_first_time()   (already a function — paste verbatim)
  # COPY cli.py:2629-2709  cmd_deploy(args, cfg)  (WRAP the deploy block; add base_url=cfg['base_url'])
  # COPY cli.py:2712-2794  cmd_hotfix(args, cfg)  (WRAP the hotfix block; add base_url=cfg['base_url'])

NOTE (idea 1, feature phase): after migration, cmd_deploy defaults to waiting
(calls _wait_for_deploy unless args.no_wait); _wait_for_deploy gets the adaptive
3s→10s interval. Paste verbatim first.

After pasting, `python -m py_compile commands/deploy.py` must pass.
"""
import os
import sys
import requests
from datetime import datetime

from ..console import logger, print_json, _interactive, C_BOLD, C_CYAN, C_GREEN, C_RESET
from ..config import get_project_data, collect_env
from ..api import (
    _tok_url, resolve_auth, require_auth, _wait_for_deploy, reconcile_project_data,
    api_set_env, api_set_static, api_upload_sqlite,
)
from ..gitutils import run_git
from .auth import _wizard_project, _print_cheatsheet
from .project import cmd_init

# ─────────────────────────────────────────────────────────────────────────────
# Commands. cmd_deploy / cmd_hotfix were inline main() blocks (wrapped in a def
# with base_url = cfg['base_url'] at the top). _deploy_first_time was already a
# function; it moved here from cli.py.
# ─────────────────────────────────────────────────────────────────────────────
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


def cmd_deploy(args, cfg):
    base_url = cfg['base_url']
    # Upgrade an old-format .viclix (api_key/project_url only) to the current
    # shape: backfill project_id + runtime, refresh project_url, keep api_key.
    proj_data = reconcile_project_data(base_url, args)
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
        # Wait by default so the user sees the build finish; --no-wait opts out.
        if not getattr(args, 'no_wait', False):
            code = _wait_for_deploy(
                base_url, api_key, project_id,
                stream=getattr(args, 'stream', False),
                timeout=getattr(args, 'wait_timeout', 600))
            sys.exit(code)
    else:
        logger.error(f"Failed to trigger rebuild: {res.text}")
        sys.exit(1)


def cmd_hotfix(args, cfg):
    base_url = cfg['base_url']
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

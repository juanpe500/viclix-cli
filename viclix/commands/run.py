"""Local development: `run` / `local` (venv + deps + uvicorn) and the
`config run` interactive local-run setup, plus all their helpers.

Imports: console, config, api (_authorize_viclix_db for --viclix-db).

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:1755-1764  _venv_paths()
  # COPY cli.py:1766-1777  _create_venv()
  # COPY cli.py:1778-1785  _file_md5()
  # COPY cli.py:1786-1816  _ensure_requirements()
  # COPY cli.py:1817-1827  _detect_app()
  # COPY cli.py:1828-1832  _has_module()
  # COPY cli.py:1833-1848  _free_port()
  # COPY cli.py:1888-1902  _WIN_UVICORN_LAUNCHER
  # COPY cli.py:1903-1943  _uvicorn_command()
  # COPY cli.py:1944-1947  COOKIE_DOMAIN_VARS
  # COPY cli.py:1948-1952  COOKIE_SECURE_VARS
  # COPY cli.py:1953-1978  _local_env_keys()
  # COPY cli.py:1979-1996  _apply_local_env()
  # COPY cli.py:1997-2101  cmd_run()
  # COPY cli.py:2274-2293  _config_add_override()
  # COPY cli.py:2294-2308  _config_remove_override()
  # COPY cli.py:2309-2359  cmd_config_run()
  # COPY cli.py:2360-2367  _print_config_options()

After pasting, `python -m py_compile commands/run.py` must pass.
"""
import os
import sys
import json
import socket
import hashlib
import requests
import subprocess
import webbrowser

from ..console import (
    logger, _menu, config_home,
    C_CYAN, C_GREEN, C_YELLOW, C_RED, C_BOLD, C_RESET,
)
from ..config import (
    load_config, get_project_data, save_project_data, _save_project_data_full,
)
from ..api import _authorize_viclix_db, resolve_auth

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# ─────────────────────────────────────────────────────────────────────────────



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


# ── Config (viclix config …) ────────────────────────────────────────────────

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


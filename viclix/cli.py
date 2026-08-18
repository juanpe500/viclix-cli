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
import argparse

from .console import ASCII_ART, ColorHelpParser, logger
from .config import load_config
from .commands.auth import (
    cmd_setup, cmd_login, cmd_logout, cmd_update, cmd_disconnect, cmd_whoami,
    _print_status_header,
)
from .commands.project import cmd_open, cmd_dash, cmd_init, cmd_delete, cmd_link
from .commands.run import cmd_run, cmd_config_run, _print_config_options
from .commands.download import cmd_download, cmd_env
from .commands.deploy import cmd_deploy, cmd_hotfix
from .commands.skill import cmd_skill
from .commands.inspect import (
    cmd_exec, cmd_pip_install, cmd_db, cmd_fs_read, cmd_rollback,
    cmd_env_setunset, cmd_probe, cmd_db_restore_exec, cmd_generic,
)
from .commands.agents import (cmd_agent_run_status, cmd_agents, cmd_fleet,
                              cmd_approve_reject, cmd_fan_out)
from .commands.say import run_say_argv
from .commands.listen import cmd_listen
from .commands.local_model import cmd_local_model






COMMANDS = [
    'setup', 'update', 'skill',
    'login', 'logout', 'whoami',
    'init', 'link', 'open', 'dash', 'disconnect', 'deploy', 'hotfix', 'run', 'local', 'download', 'env', '.env',
    'config', 'delete',
    'status', 'info', 'rebuild', 'restart', 'sleep', 'start',
    'logs-build', 'logs-app', 'exec', 'pip-install',
    'deploy-status', 'diagnostics', 'health', 'metrics',
    'db', 'db-schema', 'env-keys', 'fs', 'read',
    'deploys', 'rollback', 'requests', 'events',
    'describe', 'env-set', 'env-unset', 'packages',
    'probe', 'domains', 'scaling',
    'db-snapshot', 'db-snapshots', 'db-restore', 'db-exec',
    'agent-run', 'agent-status', 'agents', 'fleet',
    'approvals', 'approve', 'reject', 'fan-out',
    'say', 'listen', 'local-model',
]

EPILOG = """\
commands:
  setup / login              sign in to your Viclix account (and connect GitHub)
  update                     upgrade the Viclix CLI to the latest version
  logout                     sign out (remove the stored account token)
  disconnect                 clear all stored CLI credentials (account + GitHub); keeps .viclix
  whoami                     show who you're signed in as
  init                       register the current repo as a project and deploy it
                             (empty folder → scaffolds a starter; --template <ref> clones one)
  link                       link this repo to an existing project (interactive; --project-key to share)
  open                       open this project's LIVE app URL (custom domain aware) (viclix open [id])
  dash                       open this project's dashboard page in your browser (viclix dash [id])
  delete                     permanently delete this project (asks to confirm)
  run / local                run the FastAPI app locally (venv + deps + uvicorn on :9100)
  config run                 interactive local-run setup (cookie fix + env overrides)
  env / .env                 download the project's stored .env
  download                   show download options (--dotenv / --sqlite / --file)
  deploy                     first run: set up + create; after: push + rebuild (waits by default; --no-wait / --full)
  hotfix                     fast git sync into the running container (-i reinstalls deps)
  status / info              inspect the project
  logs-build / logs-app      tail the build / app logs
  restart / sleep / start    lifecycle controls
  exec --cmd "..."           run a command inside the container
  pip-install                install packages in the container
  agents                     interactive AI chat for this project (full-screen TUI; --json dumps the list)
  fleet                      list this project's deployed maintenance agents
  approvals                  list actions a maintenance agent is waiting to run (--status all for every state)
  approve / reject           approve <id> (runs it) or reject <id> a proposed agent action
  agent-run "goal" [--wait]  headless coding run in THIS project (--wait polls to done; --mode plan|full)
  fan-out "goal" --projects a,b,c    launch the same coding run across many projects (--wait waits for all)
  say "text..."              speak text aloud (Edge TTS, streamed; default output device)
  listen                     dictate a reply by voice → confirm → clipboard (Whisper; needs viclix[voice])
  local-model                bridge a LOCAL model (Ollama/LM Studio/llama.cpp) to the agents via a tunnel
  skill                      print the CLI usage guide (for an AI driving the CLI)

examples:
  viclix setup
  viclix login
  viclix link
  viclix open
  viclix dash
  viclix skill
  viclix agents
  viclix init                                     (in an empty folder → starter app)
  viclix init --template youruser/fastapi-template
  viclix init --db sqlite --env-file .env --static /static/
  viclix init --runtime static --build "npm run build" --output dist
  viclix config run
  viclix run --reload
  viclix download --sqlite
  viclix deploy --full
  viclix deploy --no-wait
  viclix hotfix -i
  viclix say "Done — deployed dashboard and cp, all green."
  viclix say --lang en "Build finished."
  viclix say --listen "Done. What next?"      (speak, then hear your reply)
  viclix listen --stop "send,enviar"

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
    g_proj.add_argument('--template',
                        help='init: start an empty folder from a template repo (git URL or '
                             'owner/repo). Clones it in, drops its history, and creates your '
                             'own repo. Empty folder + no --template → a minimal starter app.')
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
                        help='deploy: wait for the build to finish (now the default; '
                             'kept for muscle memory)')
    g_ship.add_argument('--no-wait', action='store_true',
                        help="deploy: don't wait — trigger the rebuild and return immediately")
    g_ship.add_argument('--wait-timeout', type=int, default=600,
                        help='deploy: overall wait cap in seconds (default: 600, '
                             'generous for a full rebuild)')
    g_ship.add_argument('--stream', action='store_true',
                        help='deploy: stream the full live build log instead '
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
    g_logs.add_argument('--brain', help='agent-run: wear a brain (name or id) for this coding run')
    g_logs.add_argument('--brain-version', help='agent-run: pin a brain snapshot/version (name or id)')
    g_logs.add_argument('--status', help='approvals: filter by status (pending, default; approved, '
                                         'rejected, executed, expired, all)')
    g_logs.add_argument('--agent', help='approvals: filter to one agent id')
    g_logs.add_argument('--projects', help='fan-out: comma-separated project ids to run the goal on')
    g_logs.add_argument('--json', action='store_true',
                        help='agents: dump the raw conversation list instead of the interactive picker')

    g_say = parser.add_argument_group('speech (say / listen)')
    g_say.add_argument('--voice', help='say: exact Edge TTS voice id (e.g. es-ES-AlvaroNeural); overrides --lang')
    g_say.add_argument('--lang', help='say: es | en | mix (default: mix — a multilingual voice that reads both)')
    g_say.add_argument('--rate', help='say: speech speed like "+20%%" or "-10%%" ("" = normal; default +10%%)')
    g_say.add_argument('--listen', action='store_true', help='say: after speaking, beep and listen for a spoken reply')
    g_say.add_argument('--stop', help='listen: comma-separated stop words that end capture (default: send,puto)')
    g_say.add_argument('--model', help='listen: whisper model size (tiny/base/small/medium/large; default: small)')
    g_say.add_argument('--no-confirm', action='store_true', help='listen: skip the spoken "did you say…" confirmation')
    g_say.add_argument('--device', help='listen: auto | cuda | cpu (default: auto — falls back to CPU if no cuDNN)')

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

    g_lm = parser.add_argument_group('local-model')
    g_lm.add_argument('--serve', default='auto',
                      help='local server: auto|ollama|lmstudio|llamacpp (default: auto)')
    g_lm.add_argument('--provider', default='cloudflare',
                      help='tunnel: cloudflare (default, no account) | ngrok')
    g_lm.add_argument('--serve-port', type=int,
                      help='port of the local model server (default: per --serve)')
    g_lm.add_argument('--proxy-port', type=int,
                      help='port for the local auth proxy (default: auto-picked)')
    g_lm.add_argument('--coding', action='store_true',
                      help='use the local model for the coding assistant only')
    g_lm.add_argument('--agents', action='store_true',
                      help='use the local model for automatic agents only')
    g_lm.add_argument('--label', help='label for the stored credential (default: Local)')
    # NOTE: --model (reused from the speech group) sets the default model id;
    # --serve-port/--proxy-port avoid clashing with --port (local run).

    return parser


def main():
    print(ASCII_ART)

    # `say` gets its own parser (fast-path) so its flags work before OR after the
    # text — the shared parser below can't backfill a positional that follows an
    # option. Kept fully local: no account/config needed.
    import sys as _sys
    _argv = _sys.argv[1:]
    if _argv and _argv[0] == 'say':
        run_say_argv(_argv[1:])
        return

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

    # Usage guide — self-contained, no account or config needed.
    if args.command == 'skill':
        cmd_skill(args)
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

    # Voice dictation — fully local, no account or config needed.
    if args.command == 'listen':
        cmd_listen(args)
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

    # Open the project's LIVE app URL (resolves custom domain via the server;
    # falls back to the cached .viclix URL). 'dash' opens the dashboard page.
    if args.command == 'open':
        cmd_open(args)
        return
    if args.command == 'dash':
        cmd_dash(args)
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

    if args.command == 'whoami':
        cmd_whoami(args)
        return
    if args.command == 'init':
        cmd_init(args, cfg)
        return
    if args.command == 'deploy':
        cmd_deploy(args, cfg)
        return
    if args.command == 'hotfix':
        cmd_hotfix(args, cfg)
        return
    if args.command == 'exec':
        cmd_exec(args, cfg)
        return
    if args.command == 'pip-install':
        cmd_pip_install(args, cfg)
        return
    if args.command == 'db':
        cmd_db(args, cfg)
        return
    if args.command in ('fs', 'read'):
        cmd_fs_read(args, cfg)
        return
    if args.command == 'rollback':
        cmd_rollback(args, cfg)
        return
    if args.command in ('env-set', 'env-unset'):
        cmd_env_setunset(args, cfg)
        return
    if args.command == 'probe':
        cmd_probe(args, cfg)
        return
    if args.command in ('db-restore', 'db-exec'):
        cmd_db_restore_exec(args, cfg)
        return
    if args.command in ('agent-run', 'agent-status'):
        cmd_agent_run_status(args, cfg)
        return
    if args.command == 'agents':
        cmd_agents(args, cfg)
        return
    if args.command == 'fleet':
        cmd_fleet(args, cfg)
        return
    if args.command in ('approve', 'reject'):
        cmd_approve_reject(args, cfg)
        return
    if args.command == 'fan-out':
        cmd_fan_out(args, cfg)
        return
    if args.command == 'local-model':
        cmd_local_model(args, cfg)
        return

    # Everything else is a simple GET/POST against the endpoint table.
    cmd_generic(args, cfg)


if __name__ == "__main__":
    main()

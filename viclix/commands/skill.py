"""`viclix skill` — a dense, self-describing usage guide for an AI (or human)
driving the CLI. Ships in the wheel as a constant; no network call.
"""
from ..console import C_BOLD, C_CYAN, C_GREEN, C_RESET

SKILL_GUIDE = f"""\
{C_BOLD}{C_CYAN}Viclix CLI — operating guide{C_RESET}

{C_BOLD}What it is{C_RESET}
  Deploy and manage Viclix projects (FastAPI/static/node/… apps on Viclix) from
  the terminal. Every command prints a colorized banner then acts. Read-only
  inspect commands print JSON to stdout; drive them programmatically.

{C_BOLD}Auth model{C_RESET}
  • {C_GREEN}Account token{C_RESET} — global, in ~/.viclix/config.json (via 'viclix login' /
    'viclix setup'). Drives every project you own.
  • {C_GREEN}.viclix{C_RESET} file (repo root) — non-secret project_id + project_url + runtime.
    The account token + project_id is how a command knows which project to hit.
  • {C_GREEN}--project-key{C_RESET} / --api-key — per-project token override (sharing, CI).
  Precedence: --project-key/--api-key > .viclix api_key > account_token + project_id.

{C_BOLD}Core workflow{C_RESET}
  viclix setup                      sign in (+ connect GitHub) — first run
  viclix init [--db … --env-file …] register the current repo as a project
  viclix deploy                     push + rebuild; WAITS for the build by default
                                      (adaptive poll). --no-wait to return at once,
                                      --full for a full rebuild, --stream for live logs.
  viclix hotfix [-i]                fast git sync into the running container (not static)
  viclix logs-app / logs-build      tail app / build logs (--tail N --grep RE --level)
  viclix status | diagnostics | health | metrics   inspect the running app
  viclix open                       open the LIVE app URL (custom domain aware)
  viclix dash                       open the dashboard project page

{C_BOLD}Env / files / db{C_RESET}
  viclix env-set KEY=VALUE | env-unset KEY | env-keys
  viclix download env|sqlite|file PATH        pull files locally
  viclix fs /app    |  viclix read /app/main.py
  viclix db "SELECT …"        (read-only)      |  viclix db-exec "UPDATE …" (write)
  viclix db-snapshot | db-snapshots | db-restore FILE
  viclix exec --cmd "…"   |   viclix pip-install --packages "…"

{C_BOLD}AI{C_RESET}
  viclix agents                     interactive AI chat (full-screen TUI): send
                                      prompts, stream, /mode, clickable tool cards
                                      (--json dumps the conversation list)
  viclix agent-run "goal" [--mode plan|manual|auto_edit|full]   headless coding run
  viclix agent-status <run_id>      poll a run
  viclix fleet                      list deployed maintenance agents

{C_BOLD}Lifecycle / releases{C_RESET}
  viclix restart | sleep | start
  viclix deploys | rollback <version>
  viclix probe /path [--method POST --data …]  smoke-test a route

{C_BOLD}Gotchas{C_RESET}
  • Static sites: no 'hotfix' (served from the built image) — use 'deploy'.
  • 'open' hits the live site; 'dash' opens the dashboard. (Older CLIs had 'open'
    do the dashboard — that's now 'dash'.)
  • Inspect commands exit non-zero on API errors and print JSON on success, so
    they compose in scripts. 'deploy' (waiting) exits non-zero if the build fails.
  • Run 'viclix <command> --help' style: flags are grouped under 'viclix --help'.
"""


def cmd_skill(args):
    """Print the CLI usage guide (idea 4)."""
    print(SKILL_GUIDE)

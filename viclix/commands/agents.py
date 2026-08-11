"""AI commands.

Taxonomy (decided): `agent` == chat/conversation.
  - cmd_agents    NEW (idea 5): interactive `questionary` browser of the
                  project's AI conversations (AgentSession), view a transcript.
                  Built in the feature phase — stub marker below; nothing to
                  paste during migration.
  - cmd_fleet     the OLD `viclix agents` behavior — GET projects/agents (the
                  maintenance-agents list). Moved out of inspect.py's ENDPOINTS
                  table. During migration, take the ('agents' → GET
                  'projects/agents') entry and implement it here as cmd_fleet.
  - cmd_agent_run / cmd_agent_status  headless coding-agent runs (inline block).

MIGRATION (paste from cli.py):

  # COPY cli.py:2963-2985  cmd_agent_run_status(args, cfg)  (WRAP inline block;
  #   handles both 'agent-run' and 'agent-status'; add base_url=cfg['base_url'])

  # cmd_fleet: reimplement the ('agents', GET 'projects/agents') endpoint here
  #   (was one row of inspect.py's ENDPOINTS table) as a small function that
  #   GETs projects/agents and print_json's it — same as cmd_generic did.

  # NEW (feature phase, idea 5): def cmd_agents(args) — list AgentSessions via
  #   GET projects/agent/sessions, questionary-select one, GET the new
  #   projects/agent/sessions/{id} transcript endpoint, render turns. --json dumps.

After pasting, `python -m py_compile commands/agents.py` must pass.
"""
import sys
import time
import requests

from ..console import logger, print_json
from ..config import get_project_data, load_config
from ..api import _tok_url, require_auth

# A coding run is "done waiting for" once it reaches one of these — 'chat' means
# the agent produced its answer and is idle awaiting the next message.
_RUN_TERMINAL = {'done', 'error', 'cancelled', 'failed', 'chat', 'needs_input'}


def _run_is_terminal(run_json) -> bool:
    if not isinstance(run_json, dict):
        return True
    if run_json.get('finished_at'):
        return True
    return (run_json.get('status') or '').lower() in _RUN_TERMINAL


def _wait_for_run(poll_url_base, timeout=600, label='') -> dict | None:
    """Poll a coding-run URL (already authed with ?token=...) until it reaches a
    terminal state. CLIENT-SIDE loop — never one long request — so a long run
    survives proxy idle timeouts. `&since=` streams only new step indexes. Prints
    compact status transitions. Returns the final run json (or None on error)."""
    start = time.time()
    last_status, max_idx = None, 0
    prefix = f"[{label}] " if label else ''
    while True:
        try:
            r = requests.get(poll_url_base + f"&since={max_idx}", timeout=30)
        except requests.RequestException as e:
            logger.error(f"{prefix}poll failed: {e}")
            return None
        if r.status_code != 200:
            logger.error(f"{prefix}poll failed: {r.text}")
            return None
        data = r.json()
        for s in (data.get('steps') or []):
            if isinstance(s, dict) and s.get('idx', 0) > max_idx:
                max_idx = s['idx']
        status = (data.get('status') or 'running').lower()
        if status != last_status:
            logger.info(f"{prefix}{status}")
            last_status = status
        if _run_is_terminal(data):
            return data
        if time.time() - start > timeout:
            logger.error(f"{prefix}timed out after {timeout}s (status={status})")
            return data
        time.sleep(3)

# ─────────────────────────────────────────────────────────────────────────────
# Commands (extracted verbatim from cli.py main() inline blocks).
# ─────────────────────────────────────────────────────────────────────────────
def cmd_agent_run_status(args, cfg):
    """Headless coding-agent: 'agent-run' starts a run, 'agent-status' polls one."""
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    target = (args.target or '').strip()
    if args.command == 'agent-run':
        if not target:
            logger.error('Provide a goal, e.g. viclix agent-run "add a /health endpoint"')
            sys.exit(1)
        body = {'goal': target, 'mode': (getattr(args, 'mode', None) or 'plan')}
        url = _tok_url(base_url, 'projects/agent/runs', api_key, project_id)
        res = requests.post(url, json=body)
    else:  # agent-status <run_id>
        if not target:
            logger.error('Provide a run id, e.g. viclix agent-status <run_id>')
            sys.exit(1)
        url = _tok_url(base_url, f'projects/agent/runs/{target}', api_key, project_id)
        res = requests.get(url)
    if res.status_code not in (200, 201):
        logger.error(f"{args.command} failed: {res.text}")
        sys.exit(1)
    data = res.json()
    print_json(data)
    # dispatch-and-wait: after starting a run, optionally block until it finishes.
    if args.command == 'agent-run' and getattr(args, 'wait', False):
        run_id = data.get('run_id')
        if run_id:
            poll = _tok_url(base_url, f'projects/agent/runs/{run_id}', api_key, project_id)
            final = _wait_for_run(poll, timeout=getattr(args, 'wait_timeout', 600) or 600)
            if final:
                print_json(final)
                if (final.get('status') or '').lower() in ('error', 'failed', 'cancelled'):
                    sys.exit(1)


def cmd_fan_out(args, cfg):
    """Launch the same coding-agent goal across several projects at once.

    viclix fan-out "goal" --projects id1,id2,id3 [--mode plan|full] [--wait]
    Needs an ACCOUNT token (viclix login) since it spans projects. With --wait,
    polls every launched run to completion and prints a per-project summary.
    """
    base_url = cfg['base_url']
    goal = (args.target or '').strip()
    if not goal:
        logger.error('Provide a goal, e.g. viclix fan-out "add a /health endpoint" --projects a,b')
        sys.exit(1)
    ids = [p.strip() for p in (getattr(args, 'projects', None) or '').split(',') if p.strip()]
    if not ids:
        logger.error('Provide --projects id1,id2,id3')
        sys.exit(1)
    token = (load_config(required=False) or {}).get('account_token')
    if not token:
        logger.error("fan-out needs an account login. Run 'viclix login' first.")
        sys.exit(1)
    mode = getattr(args, 'mode', None) or 'plan'
    body = {'goal': goal, 'mode': mode,
            'targets': [{'project_id': pid} for pid in ids]}
    try:
        res = requests.post(f"{base_url}agent/dispatch?token={token}",
                            json=body, timeout=30)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix: {e}")
        sys.exit(1)
    if res.status_code != 200:
        logger.error(f"fan-out failed: {res.text}")
        sys.exit(1)
    data = res.json()
    print_json(data)
    if not getattr(args, 'wait', False):
        return
    # dispatch-and-wait across all launched runs (sequential poll; each is a cheap
    # client loop). Prints a final status per project.
    finals = []
    for r in (data.get('results') or []):
        pid, run_id = r.get('project_id'), r.get('run_id')
        if not run_id:
            finals.append({'project_id': pid, 'error': r.get('error')})
            continue
        poll = f"{base_url}agent/runs/{run_id}?token={token}&project_id={pid}"
        final = _wait_for_run(poll, timeout=getattr(args, 'wait_timeout', 600) or 600,
                              label=str(pid))
        finals.append({'project_id': pid, 'run_id': run_id,
                       'status': (final or {}).get('status'),
                       'cost': (final or {}).get('cost'),
                       'summary': (final or {}).get('summary')})
    print_json({'results': finals})


def cmd_fleet(args, cfg):
    """List the project's maintenance agents (GET projects/agents).

    This is the OLD `viclix agents` behavior, moved out of inspect.py's ENDPOINTS
    table verbatim. In the feature phase `agents` becomes the conversation
    browser (cmd_agents) and this keeps working under its own command name.
    """
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    url = _tok_url(base_url, 'projects/agents', api_key, project_id)
    response = requests.get(url)
    if response.status_code == 200:
        res_json = response.json()
        if proj_data and proj_data.get('project_url') and 'project_url' not in res_json:
            res_json['project_url'] = proj_data['project_url']
        print_json(res_json)
    else:
        logger.error(f"Error: {response.text}")
        sys.exit(1)

def cmd_approve_reject(args, cfg):
    """Approve (runs it) or reject a maintenance agent's proposed action.

    Usage: viclix approve <approval_id> | viclix reject <approval_id>
    Get the id from `viclix approvals`.
    """
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    approval_id = (args.target or '').strip()
    if not approval_id:
        logger.error(f'Provide an approval id, e.g. viclix {args.command} <id>  '
                     '(list them with: viclix approvals)')
        sys.exit(1)
    verb = 'approve' if args.command == 'approve' else 'reject'
    url = _tok_url(base_url, f'projects/approvals/{approval_id}/{verb}', api_key, project_id)
    res = requests.post(url)
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"{verb} failed: {res.text}")
        sys.exit(1)


# ── Conversation chat (idea 5): agent == chat/conversation ───────────────────
def cmd_agents(args, cfg):
    """Interactive AI chat for this project — pick/continue a conversation, send
    prompts, stream the run, choose the mode. A full-terminal TUI (textual).

    `--json` dumps the raw conversation list instead (scripts / AI callers).
    """
    if getattr(args, 'json', False):
        base_url = cfg['base_url']
        proj_data = get_project_data()
        api_key, project_id = require_auth(args, proj_data or {})
        try:
            r = requests.get(_tok_url(base_url, 'projects/agent/sessions', api_key, project_id), timeout=20)
        except requests.RequestException as e:
            logger.error(f"Could not reach Viclix: {e}")
            sys.exit(1)
        if r.status_code != 200:
            logger.error(f"Failed to list conversations: {r.text}")
            sys.exit(1)
        print_json(r.json() or {})
        return

    from .chat import cmd_agents_chat
    cmd_agents_chat(args, cfg)

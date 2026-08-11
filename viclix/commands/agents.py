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
import requests

from ..console import logger, print_json
from ..config import get_project_data
from ..api import _tok_url, require_auth

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
    if res.status_code in (200, 201):
        print_json(res.json())
    else:
        logger.error(f"{args.command} failed: {res.text}")
        sys.exit(1)


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

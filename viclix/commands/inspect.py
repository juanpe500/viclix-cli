"""Inspection / container / db commands, plus the generic read-only endpoint
table (status, logs, metrics, deploys, …).

Every command here currently lives INLINE in cli.py `main()` as an
`if args.command == ...:` block. Migrate each the same way as deploy.py:
wrap the block body in `def cmd_X(args, cfg): base_url = cfg['base_url']` then
paste the de-indented body (drop the `if`, keep `return`s). No logic is lost.

MIGRATION (paste from cli.py — each is an inline block → a function):

  # COPY cli.py:2797-2821  cmd_exec(args, cfg)
  # COPY cli.py:2824-2838  cmd_pip_install(args, cfg)
  # COPY cli.py:2841-2855  cmd_db(args, cfg)
  # COPY cli.py:2858-2877  cmd_fs_read(args, cfg)        (handles both 'fs' and 'read')
  # COPY cli.py:2880-2894  cmd_rollback(args, cfg)
  # COPY cli.py:2897-2919  cmd_env_setunset(args, cfg)   (handles 'env-set' and 'env-unset')
  # COPY cli.py:2922-2936  cmd_probe(args, cfg)
  # COPY cli.py:2939-2960  cmd_db_restore_exec(args, cfg) (handles 'db-restore' and 'db-exec')

  # COPY cli.py:2991-3016  ENDPOINTS  (the {command: (METHOD, path)} dict)
  #   → REMOVE the 'agents' line: 2260... it moves to agents.py as cmd_fleet.
  # COPY cli.py:3018-3044  cmd_generic(args, cfg)  (WRAP: method/path lookup +
  #   log filters + GET/POST + print_json). Keep the proj_data/require_auth lines
  #   at 2988-2989 inside this function.

After pasting, `python -m py_compile commands/inspect.py` must pass.
"""
import sys
import requests

from ..console import logger, print_json
from ..config import get_project_data
from ..api import _tok_url, require_auth

# ─────────────────────────────────────────────────────────────────────────────
# Commands (extracted verbatim from cli.py main() inline blocks — each block
# body wrapped in a function; only the `if args.command ==` header became a
# `def`, and `base_url = cfg['base_url']` was added at the top).
# ─────────────────────────────────────────────────────────────────────────────
def cmd_exec(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})

    if not args.cmd:
        logger.error("Specify command with --cmd")
        sys.exit(1)

    url = _tok_url(base_url, 'projects/exec', api_key, project_id)
    res = requests.post(url, data={
        'command': args.cmd,
        'timeout': str(min(max(args.timeout, 1), 30)),
        'workdir': args.workdir
    })
    if res.status_code == 200:
        data = res.json()
        if data.get('stdout'):
            print(data['stdout'])
        if data.get('stderr'):
            print(data['stderr'], file=sys.stderr)
        print_json(data)
    else:
        logger.error(f"Exec failed: {res.text}")
        sys.exit(1)


def cmd_pip_install(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})

    url = _tok_url(base_url, 'projects/pip-install', api_key, project_id)
    data = {}
    if args.packages:
        data['packages'] = args.packages
    res = requests.post(url, data=data)
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"pip-install failed: {res.text}")
        sys.exit(1)


def cmd_db(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    sql = (args.target or '').strip()
    if not sql:
        logger.error('Provide a query, e.g. viclix db "SELECT * FROM users LIMIT 5"')
        sys.exit(1)
    url = _tok_url(base_url, 'projects/db-query', api_key, project_id)
    res = requests.post(url, json={'sql': sql})
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"db query failed: {res.text}")
        sys.exit(1)


def cmd_fs_read(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    target_path = (args.target or '').strip()
    if args.command == 'read':
        if not target_path:
            logger.error('Provide a file path, e.g. viclix read /app/main.py')
            sys.exit(1)
        url = _tok_url(base_url, 'projects/fs/read', api_key, project_id)
        url += f"&path={requests.utils.quote(target_path)}"
    else:
        url = _tok_url(base_url, 'projects/fs', api_key, project_id)
        url += f"&path={requests.utils.quote(target_path or '/app')}"
    res = requests.get(url)
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"fs failed: {res.text}")
        sys.exit(1)


def cmd_rollback(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    version = (args.target or '').strip()
    if not version:
        logger.error('Provide a version, e.g. viclix rollback 20260809123000  (see: viclix deploys)')
        sys.exit(1)
    url = _tok_url(base_url, 'projects/rollback', api_key, project_id)
    res = requests.post(url, json={'version': version})
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"rollback failed: {res.text}")
        sys.exit(1)


def cmd_env_setunset(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    target = (args.target or '').strip()
    if args.command == 'env-set':
        if '=' not in target:
            logger.error('Use KEY=VALUE, e.g. viclix env-set DEBUG=1')
            sys.exit(1)
        key, value = target.split('=', 1)
        url = _tok_url(base_url, 'projects/env/set', api_key, project_id)
        res = requests.post(url, json={'key': key.strip(), 'value': value})
    else:
        if not target:
            logger.error('Provide a key, e.g. viclix env-unset DEBUG')
            sys.exit(1)
        url = _tok_url(base_url, f'projects/env/{requests.utils.quote(target)}', api_key, project_id)
        res = requests.delete(url)
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"{args.command} failed: {res.text}")
        sys.exit(1)


def cmd_probe(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    path = (args.target or '/').strip()
    payload = {'path': path, 'method': (getattr(args, 'method', None) or 'GET')}
    if getattr(args, 'data', None):
        payload['body'] = args.data
    url = _tok_url(base_url, 'projects/probe', api_key, project_id)
    res = requests.post(url, json=payload)
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"probe failed: {res.text}")
        sys.exit(1)


def cmd_db_restore_exec(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})
    target = (args.target or '').strip()
    if args.command == 'db-restore':
        if not target:
            logger.error('Provide a snapshot filename, e.g. viclix db-restore 20260810_101500.dump  (see: viclix db-snapshots)')
            sys.exit(1)
        url = _tok_url(base_url, 'projects/db/restore', api_key, project_id)
        res = requests.post(url, json={'filename': target})
    else:  # db-exec — the invocation is the confirmation
        if not target:
            logger.error('Provide SQL, e.g. viclix db-exec "UPDATE users SET active=true"')
            sys.exit(1)
        url = _tok_url(base_url, 'projects/db/execute', api_key, project_id)
        res = requests.post(url, json={'sql': target, 'confirm': True})
    if res.status_code == 200:
        print_json(res.json())
    else:
        logger.error(f"{args.command} failed: {res.text}")
        sys.exit(1)


# ── Generic read-only endpoint table (status, logs, metrics, deploys, …) ─────
# The 'agents' row moved to commands/agents.py as cmd_fleet.
ENDPOINTS = {
    'status': ('GET', 'projects/status'),
    'info': ('GET', 'projects'),
    'rebuild': ('POST', 'projects/rebuild'),
    'restart': ('POST', 'projects/restart'),
    'sleep': ('POST', 'projects/sleep'),
    'start': ('POST', 'projects/start'),
    'logs-build': ('GET', 'projects/logs/build'),
    'logs-app': ('GET', 'projects/logs/app'),
    'deploy-status': ('GET', 'projects/deploy/status'),
    'diagnostics': ('GET', 'projects/diagnostics'),
    'health': ('GET', 'projects/health'),
    'metrics': ('GET', 'projects/metrics'),
    'db-schema': ('GET', 'projects/db/schema'),
    'env-keys': ('GET', 'projects/env/keys'),
    'deploys': ('GET', 'projects/deploys'),
    'requests': ('GET', 'projects/requests'),
    'events': ('GET', 'projects/events'),
    'describe': ('GET', 'projects/config'),
    'packages': ('GET', 'projects/packages'),
    'domains': ('GET', 'projects/domains'),
    'scaling': ('GET', 'projects/scaling'),
    'db-snapshot': ('POST', 'projects/db/snapshot'),
    'db-snapshots': ('GET', 'projects/db/snapshots'),
}


def cmd_generic(args, cfg):
    base_url = cfg['base_url']
    proj_data = get_project_data()
    api_key, project_id = require_auth(args, proj_data or {})

    method, path = ENDPOINTS[args.command]
    url = _tok_url(base_url, path, api_key, project_id)

    # Log filters (server-side tail/grep/level) so we pull only what matters.
    if args.command in ('logs-app', 'logs-build'):
        if getattr(args, 'tail', 0):
            url += f"&tail={int(args.tail)}"
        if getattr(args, 'grep', None):
            url += f"&grep={requests.utils.quote(args.grep)}"
        if getattr(args, 'level', None):
            url += f"&level={requests.utils.quote(args.level)}"
    if args.command in ('deploys', 'requests', 'events') and getattr(args, 'limit', 0):
        url += f"&limit={int(args.limit)}"

    if method == 'GET':
        response = requests.get(url)
    else:
        response = requests.post(url)

    if response.status_code == 200:
        res_json = response.json()
        if proj_data and proj_data.get('project_url') and 'project_url' not in res_json:
            res_json['project_url'] = proj_data['project_url']
        print_json(res_json)
    else:
        logger.error(f"Error: {response.text}")
        sys.exit(1)

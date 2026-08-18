"""Viclix API layer: authenticated URL builders, POST helpers, the env/static/
rebuild/sqlite calls, deploy-status polling, auth resolution, and small response
formatters.

Imports: console (logger, colors), config (load_config, get_project_data,
DEFAULT_BASE_URL).

NOTE (idea 1): `_wait_for_deploy` is edited during feature work — adaptive poll
interval (3s, then 10s after 30s). Paste it verbatim first; edit later.

MIGRATION (paste from cli.py, in this order):

  # COPY cli.py:638-645   _tok_url()
  # COPY cli.py:648-654   _tok_params()
  # COPY cli.py:657-733   _wait_for_deploy()          [idea-1 edits land here later]
  # COPY cli.py:736-759   resolve_auth()
  # COPY cli.py:762-769   require_auth()
  # COPY cli.py:773-779   _api_post()
  # COPY cli.py:819-832   api_upload_sqlite()
  # COPY cli.py:835-843   api_set_env()
  # COPY cli.py:845-852   api_set_static()
  # COPY cli.py:855-861   api_rebuild()
  # COPY cli.py:1195-1198 _dashboard_base()
  # COPY cli.py:507-515   _explain_api_error()
  # COPY cli.py:2102-2106 _err_detail()
  # COPY cli.py:1849-1885 _authorize_viclix_db()

After pasting, `python -m py_compile api.py` must pass.
"""
import os
import sys
import requests

from .console import logger, C_CYAN, C_GREEN, C_YELLOW, C_RED, C_RESET
from .config import load_config, get_project_data, save_project_data, DEFAULT_BASE_URL

# ─────────────────────────────────────────────────────────────────────────────
# PASTE ZONE — copy the symbols listed above, in order.
# _wait_for_deploy does `import time` inside the function body — keep that.
# ─────────────────────────────────────────────────────────────────────────────



# ── Auth resolution (account token vs project key) ──────────────────────────
def _tok_url(base_url, path, token, project_id=None):
    """Build an authenticated API URL. Adds project_id when present, which the
    server needs to pick the project for an ACCOUNT token (harmless — validated —
    for a project token)."""
    url = f"{base_url}{path}?token={token}"
    if project_id:
        url += f"&project_id={project_id}"
    return url


def _tok_params(token, project_id=None, **extra):
    """Same idea as _tok_url but for requests(..., params=...)."""
    p = {"token": token}
    if project_id:
        p["project_id"] = project_id
    p.update(extra)
    return p


def _wait_for_deploy(base_url, api_key, project_id, stream=False, timeout=600):
    """Poll projects/deploy/status until the build reaches a terminal state.

    A CLIENT-SIDE loop — never one long-held request — so a 5-minute full rebuild
    survives proxy idle timeouts. Two independent stops: an overall `timeout`, and
    a 90s stall guard (no phase change AND no new log bytes). Default output is
    compact phase/progress; `stream` tails the full live build log incrementally
    (only new bytes each poll via since_offset). Returns an exit code: 0 running,
    1 failed/stalled/timed-out.
    """
    import time
    stall_limit = 90
    started = last_change = time.time()
    offset = 0
    last_phase = last_progress = None
    url_base = _tok_url(base_url, 'projects/deploy/status', api_key, project_id)
    print(f"{C_CYAN}Waiting for deploy…{C_RESET}")
    while True:
        # Adaptive interval: snappy for the first 30s, then back off so a long
        # full rebuild (5+ min) doesn't hammer the endpoint.
        poll = 3 if time.time() - started < 30 else 10
        try:
            r = requests.get(f"{url_base}&since_offset={offset}", timeout=15)
        except Exception as e:
            if time.time() - started > timeout:
                logger.error("Timed out waiting for deploy. Check: viclix logs-build")
                return 1
            logger.warning(f"poll error: {e}")
            time.sleep(poll)
            continue
        if r.status_code != 200:
            logger.warning(f"deploy/status {r.status_code}: {r.text[:200]}")
            time.sleep(poll)
            continue

        d = r.json()
        state = d.get('state')
        phase = d.get('phase')
        progress = d.get('progress_pct')
        delta = d.get('log_delta') or ''
        new_offset = d.get('log_offset') or offset

        moved = False
        if stream and delta:
            sys.stdout.write(delta)
            sys.stdout.flush()
            moved = True
        elif (phase, progress) != (last_phase, last_progress):
            print(f"  {C_CYAN}▸{C_RESET} {phase} · {progress}%")
            moved = True
        if new_offset != offset:
            moved = True
        offset = new_offset
        last_phase, last_progress = phase, progress
        if moved:
            last_change = time.time()

        if d.get('terminal'):
            if d.get('ok') is True or state == 'running':
                print(f"\n{C_GREEN}✓ deploy running{C_RESET}")
                return 0
            if state == 'sleeping':
                print(f"\n{C_YELLOW}app is sleeping{C_RESET}")
                return 0
            print(f"\n{C_RED}✗ deploy failed{C_RESET} ({d.get('deploy_status')})")
            tail = (d.get('log_delta') or '').strip()
            if tail:
                print(tail[-2000:])
            print("See: viclix logs-build")
            return 1

        now = time.time()
        if now - started > timeout:
            logger.error(f"Timed out after {timeout}s (state: {state}). Check: viclix logs-build")
            return 1
        if now - last_change > stall_limit:
            logger.error(f"No progress for {stall_limit}s (state: {state}) — build looks "
                         f"stalled. Check: viclix logs-build")
            return 1
        time.sleep(poll)


def resolve_auth(args, proj=None):
    """Return (token, project_id) for a per-project API call.

    Precedence:
      1. --project-key / --api-key on the CLI      → that project token.
      2. api_key stored in .viclix                 → that project token (legacy /
                                                     a shared other-account key).
      3. account token (global config) + project_id from .viclix → account path.

    The account path is what lets one account token drive every project; the
    project_id (non-secret, stored in .viclix) tells the server which one.
    """
    if proj is None:
        proj = get_project_data() or {}
    project_id = proj.get('project_id')
    explicit = getattr(args, 'project_key', None) or getattr(args, 'api_key', None)
    if explicit:
        return explicit, project_id
    if proj.get('api_key'):
        return proj['api_key'], project_id
    cfg = load_config(required=False)
    if cfg.get('account_token') and project_id:
        return cfg['account_token'], project_id
    return None, project_id


def require_auth(args, proj=None):
    """resolve_auth, but exit with a helpful message when nothing is available."""
    token, project_id = resolve_auth(args, proj)
    if not token:
        logger.error("This repo isn't linked to a Viclix project. Run 'viclix init' "
                     "or 'viclix link' here, or pass --project-key.")
        sys.exit(1)
    return token, project_id


def reconcile_project_data(base_url, args):
    """Backfill an old-format .viclix into the current shape and return the
    (possibly updated) proj dict.

    Early Viclix wrote only ``{api_key, project_url}``; the current format also
    stores the non-secret ``project_id`` (which lets the account token drive the
    project) and ``runtime``. This upgrades those older files in place:

      • adds ``project_id`` / ``runtime`` when missing,
      • refreshes ``project_url`` from the server resolver (custom-domain aware,
        so a stale slug/domain gets corrected),
      • KEEPS any existing ``api_key`` (you can remove it yourself later to run on
        the account token alone).

    No-op when the file is already complete, or when there's no usable token /
    the server is unreachable (so it never blocks a deploy). Works via a project
    ``api_key`` (which self-identifies) or an account token + existing project_id.
    """
    proj = get_project_data() or {}
    if proj.get('project_id') and proj.get('project_url') and proj.get('runtime'):
        return proj  # already current — no network

    token, project_id = resolve_auth(args, proj)
    if not token:
        return proj  # nothing to resolve with (handled by the first-deploy path)

    updates = {}
    # id + runtime — GET /projects echoes them for a project token (an account
    # token is rejected there, but then project_id is already known anyway).
    try:
        r = requests.get(_tok_url(base_url, 'projects', token, project_id), timeout=15)
        if r.status_code == 200:
            d = r.json() or {}
            if not proj.get('project_id') and d.get('id'):
                updates['project_id'] = d['id']
            if not proj.get('runtime') and d.get('runtime'):
                updates['runtime'] = d['runtime']
    except requests.RequestException:
        pass

    # project_url — resolve the current public URL (custom domain > default).
    pid = updates.get('project_id') or project_id
    try:
        rc = requests.get(_tok_url(base_url, 'projects/config', token, pid), timeout=15)
        if rc.status_code == 200:
            url = (rc.json() or {}).get('project_url')
            if url and url != proj.get('project_url'):
                updates['project_url'] = url
    except requests.RequestException:
        pass

    if updates:
        save_project_data(**updates)  # preserves api_key / local_env
        logger.info(f"Updated .viclix ({', '.join(sorted(updates))}).")
        proj = get_project_data() or proj
    return proj


# ── Env + API helpers (parity with the manual wizard) ───────────────────────
def _api_post(url, **kwargs):
    """POST with a clean error instead of a traceback on network failure."""
    try:
        return requests.post(url, **kwargs)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix: {e}")
        sys.exit(1)


def api_upload_sqlite(base_url, token, project_id, remote_path, local_file):
    url = _tok_url(base_url, 'projects/sqlite/upload', token, project_id)
    logger.info(f"Uploading SQLite seed '{os.path.basename(local_file)}' → {remote_path}")
    with open(local_file, 'rb') as f:
        res = _api_post(
            url,
            data={'path': remote_path},
            files={'data': (os.path.basename(local_file), f)},
        )
    if res.status_code == 200:
        logger.info("SQLite seed uploaded.")
    else:
        logger.error(f"SQLite upload failed: {res.text}")
        sys.exit(1)


def api_set_env(base_url, token, project_id, env, mode='merge'):
    url = _tok_url(base_url, 'projects/env', token, project_id)
    res = _api_post(url, json={'env_vars': env, 'mode': mode})
    if res.status_code == 200:
        logger.info(f"Environment updated ({len(env)} variable(s)).")
    else:
        logger.error(f"Failed to set env: {res.text}")
        sys.exit(1)


def api_set_static(base_url, token, project_id, prefix, cache):
    url = _tok_url(base_url, 'projects/static', token, project_id)
    res = _api_post(url, json={'path_prefix': prefix, 'cache_max_age': cache or 86400})
    if res.status_code == 200:
        logger.info(f"Static path set: {prefix}")
    else:
        logger.error(f"Failed to set static path: {res.text}")
        sys.exit(1)


def api_rebuild(base_url, token, project_id, full=True):
    url = _tok_url(base_url, 'projects/rebuild', token, project_id)
    if full:
        url += "&full=true"
    res = _api_post(url)
    if res.status_code != 200:
        logger.warning(f"Could not trigger the first deploy automatically: {res.text}")


# ── Guided setup (viclix setup / first run) ─────────────────────────────────
def _dashboard_base(base_url):
    """Turn the API base (…/api/v1/) into the dashboard origin for browser links."""
    root = (base_url or DEFAULT_BASE_URL).split('/api/')[0].rstrip('/')
    return root or 'https://dashboard.viclix.com'


def _explain_api_error(res):
    """Turn a Viclix API error response into a short, human sentence."""
    detail = _err_detail(res)
    low = (detail or '').lower()
    if res.status_code in (401, 403):
        if 'account token' in low and 'project_id' in low:
            return "This repo isn't linked to a project yet. Run 'viclix link' or 'viclix init'."
        return "Your token was rejected. Check it in Settings → Tokens on the dashboard."
    return detail or f"HTTP {res.status_code}"


# ── Download (viclix download) ──────────────────────────────────────────────
def _err_detail(res):
    try:
        return res.json().get('detail') or res.text
    except Exception:
        return res.text


def _authorize_viclix_db(base_url, token, project_id=None):
    """Whitelist this machine's IP on the project's Viclix DB and return its URL."""
    if not token:
        logger.error("--viclix-db needs a Viclix project — run inside a repo set up with 'viclix init'.")
        return None
    # Detect our public IP so the server whitelists the right one (it also
    # falls back to the request IP if we can't).
    ip = None
    try:
        ip = requests.get("https://api.ipify.org", timeout=8).text.strip()
    except requests.RequestException:
        ip = None
    try:
        res = requests.post(f"{base_url}projects/db/authorize",
                            params=_tok_params(token, project_id), json={"ip": ip}, timeout=20)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix to authorize the database: {e}")
        return None
    if res.status_code != 200:
        try:
            detail = res.json().get("detail") or res.text
        except Exception:
            detail = res.text
        logger.error(f"Viclix DB authorization failed: {detail}")
        return None
    data = res.json()
    logger.info(f"Authorized {data.get('ip')} on Viclix DB '{data.get('db_name')}' — DATABASE_URL injected.")
    logger.debug(f"DB authorize response: {data}")
    return data.get("database_url")


# ── Local-model BYOK provider (viclix local-model) ──────────────────────────
def api_register_local_provider(base_url, account_token, payload):
    """Upsert the user's BYOK 'local' provider (public tunnel URL + shared token).

    Account-level call (user-wide, not per-project), like agent/dispatch. Returns
    the server's credential view on success or None on failure (logged)."""
    try:
        res = requests.post(f"{base_url}agent/provider",
                            params={"token": account_token}, json=payload, timeout=30)
    except requests.RequestException as e:
        logger.error(f"Could not reach Viclix to register the provider: {e}")
        return None
    if res.status_code != 200:
        logger.error(f"Provider register failed: {_err_detail(res)}")
        return None
    return res.json()


def api_deactivate_local_provider(base_url, account_token, provider="local"):
    """Deactivate the user's BYOK provider (teardown). Best-effort — logs on
    failure but never raises, so Ctrl+C cleanup always completes."""
    try:
        res = requests.post(f"{base_url}agent/provider/deactivate",
                            params={"token": account_token},
                            json={"provider": provider}, timeout=15)
    except requests.RequestException as e:
        logger.warning(f"Could not deactivate the provider (tunnel already down?): {e}")
        return None
    if res.status_code != 200:
        logger.warning(f"Provider deactivate failed: {_err_detail(res)}")
        return None
    return res.json()


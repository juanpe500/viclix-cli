"""`viclix mcp` — connect external MCP servers so the Viclix agents can use
their tools while they work on your apps.

Two ways in:
  * `viclix mcp expose <name> --port <p>` — the star of the show: expose a LOCAL
    MCP server (e.g. the chrome-browser server on 127.0.0.1:3777/mcp) to the
    Viclix cloud. The agents run remotely and can't reach your localhost, so this
    opens a token-guarded auth proxy + a public tunnel (cloudflared/ngrok) and
    registers that rotating https URL. Runs in the foreground; Ctrl+C tears the
    tunnel down and disables the server.
  * `viclix mcp add <name> <url>` — register an already-public MCP server directly
    (no tunnel).

Plus `list`, `test`, `remove`. The heavy lifting (proxy + tunnel) is shared with
`local-model` via commands/_tunnel.py.

Imports: console, config, api, _tunnel. Stdlib + requests only.
"""
import sys
import time
import secrets

from ..console import logger, C_BOLD, C_CYAN, C_GREEN, C_YELLOW, C_RED, C_RESET
from ..config import get_project_data
from ..api import (api_mcp_register, api_mcp_list, api_mcp_test, api_mcp_deactivate)
from ._tunnel import _start_proxy, start_tunnel, _kill


def _scope(args):
    """Resolve project_id from --project (this repo's .viclix) / --all-projects.
    Default = all projects (NULL) so the server is broadly available."""
    if getattr(args, "project", False):
        data = get_project_data() or {}
        pid = data.get("project_id")
        if not pid:
            logger.error("--project needs a linked repo (run inside a folder set up "
                         "with `viclix init`/`viclix link`). Use --all-projects instead.")
            sys.exit(1)
        return pid
    return None  # --all-projects / default → global


def _print_test(res):
    """Pretty-print an api_mcp_test result. Returns True if tools were found."""
    if not res:
        print(f"{C_RED}✗ could not reach Viclix to run the test.{C_RESET}")
        return False
    if res.get("ok"):
        tools = res.get("tools") or []
        n = res.get("tool_count") or len(tools)
        shown = ", ".join(tools[:12]) + (", …" if len(tools) > 12 else "")
        print(f"{C_GREEN}✓ {n} tool{'' if n == 1 else 's'}{C_RESET}"
              + (f" — {shown}" if shown else ""))
        return True
    print(f"{C_RED}✗ {res.get('error') or 'connection failed.'}{C_RESET}")
    return False


def _scope_label(project_id):
    return "this project" if project_id else "all projects"


# ── Subcommands ─────────────────────────────────────────────────────────────
def _cmd_expose(args, base_url, account_token):
    name = (args.target_arg or "").strip()
    port = getattr(args, "port", None)
    if not name or not port:
        logger.error("Usage: viclix mcp expose <name> --port <local-port> [--path /mcp]")
        sys.exit(1)
    path = getattr(args, "path", None) or "/mcp"
    if not path.startswith("/"):
        path = "/" + path
    provider_kind = (getattr(args, "provider", None) or "cloudflare").strip().lower()
    proxy_port = getattr(args, "proxy_port", None)
    project_id = _scope(args)

    token = secrets.token_urlsafe(32)
    upstream = f"http://127.0.0.1:{port}"
    proxy_srv, actual_proxy_port = _start_proxy(upstream, token, proxy_port)
    logger.info(f"Auth proxy on 127.0.0.1:{actual_proxy_port} → {upstream}")

    tunnel_proc = None
    registered = False
    try:
        tunnel_proc, public = start_tunnel(provider_kind, actual_proxy_port)
        if not public.startswith("https://"):
            # SSRF guard on the server permits http:// public URLs, but we only
            # ever register the https tunnel endpoint.
            logger.error(f"Tunnel returned a non-https URL ({public}); refusing.")
            return
        full_url = public.rstrip("/") + path
        print(f"\n{C_GREEN}✓ tunnel up:{C_RESET} {public}")

        # A fresh tunnel hostname needs a few seconds to resolve in public DNS
        # before the Viclix cloud (which resolves + SSRF-checks it) will accept it.
        print(f"{C_CYAN}Registering (waiting for tunnel DNS to propagate)…{C_RESET}")
        res = api_mcp_register(base_url, account_token, {
            "name": name,
            "url": full_url,
            "auth_header": "Authorization",
            "auth_value": f"Bearer {token}",
            "project_id": project_id,
            "enabled": True,
        }, retries=12, retry_delay=3)
        if not res:
            logger.error("Could not register the MCP server — tearing down.")
            return
        registered = True
        print(f"{C_GREEN}✓ registered{C_RESET} MCP server {C_CYAN}{name}{C_RESET} "
              f"→ {full_url}  ·  scope {_scope_label(project_id)}")

        # Auto-test so the user sees the discovered tools immediately.
        print(f"{C_CYAN}Testing…{C_RESET}")
        ok = _print_test(api_mcp_test(base_url, account_token, name=name))
        if not ok:
            print(f"{C_YELLOW}⚠ No tools discovered. Is the local server serving "
                  f"{path} on port {port}? Leaving it up — fix the server and it'll "
                  f"work on the next agent run, or Ctrl+C to stop.{C_RESET}")

        print(f"\n{C_YELLOW}⚠ This exposes your local MCP server to the internet while "
              f"the tunnel is up.{C_RESET}\n  Only requests carrying the secret token are "
              f"accepted. Press Ctrl+C to stop and close the tunnel.\n")

        while True:
            if tunnel_proc.poll() is not None:
                logger.error("Tunnel process exited — stopping.")
                break
            time.sleep(1)
    except KeyboardInterrupt:
        print(f"\n{C_CYAN}Stopping…{C_RESET}")
    finally:
        _kill(tunnel_proc)
        try:
            proxy_srv.shutdown()
        except Exception:  # noqa: BLE001
            pass
        if registered:
            api_mcp_deactivate(base_url, account_token, name)
            logger.info(f"Disabled MCP server '{name}' (agents no longer see its tools).")
        print(f"{C_GREEN}✓ closed.{C_RESET}")


def _cmd_add(args, base_url, account_token):
    name = (args.target_arg or "").strip()
    url = (getattr(args, "target_arg2", None) or "").strip()
    if not name or not url:
        logger.error('Usage: viclix mcp add <name> <https-url> [--header "K: V"]')
        sys.exit(1)
    payload = {"name": name, "url": url, "project_id": _scope(args), "enabled": True}
    header = getattr(args, "header", None)
    if header:
        if ":" not in header:
            logger.error('--header must be "Name: value" (e.g. --header "Authorization: Bearer xyz")')
            sys.exit(1)
        hk, hv = header.split(":", 1)
        payload["auth_header"] = hk.strip()
        payload["auth_value"] = hv.strip()
    res = api_mcp_register(base_url, account_token, payload)
    if not res:
        sys.exit(1)
    print(f"{C_GREEN}✓ registered{C_RESET} MCP server {C_CYAN}{name}{C_RESET} "
          f"→ {url}  ·  scope {_scope_label(payload['project_id'])}")
    print(f"{C_CYAN}Testing…{C_RESET}")
    _print_test(api_mcp_test(base_url, account_token, name=name))


def _cmd_list(args, base_url, account_token):
    servers = api_mcp_list(base_url, account_token)
    if servers is None:
        sys.exit(1)
    if not servers:
        print("No MCP servers registered. Add one with `viclix mcp add` or "
              "`viclix mcp expose`.")
        return
    print(f"\n{C_BOLD}Your MCP servers:{C_RESET}")
    for s in servers:
        state = f"{C_GREEN}enabled{C_RESET}" if s.get("enabled") else f"{C_YELLOW}disabled{C_RESET}"
        scope = "1 project" if s.get("project_id") else "all projects"
        auth = " 🔒" if s.get("has_auth") else ""
        print(f"  {C_CYAN}{s.get('name')}{C_RESET}  [{state}]  {scope}{auth}")
        print(f"      {s.get('url')}")
    print()


def _cmd_test(args, base_url, account_token):
    name = (args.target_arg or "").strip()
    if not name:
        logger.error("Usage: viclix mcp test <name>")
        sys.exit(1)
    print(f"{C_CYAN}Testing {name}…{C_RESET}")
    ok = _print_test(api_mcp_test(base_url, account_token, name=name))
    sys.exit(0 if ok else 1)


def _cmd_remove(args, base_url, account_token):
    name = (args.target_arg or "").strip()
    if not name:
        logger.error("Usage: viclix mcp remove <name>")
        sys.exit(1)
    res = api_mcp_deactivate(base_url, account_token, name)
    if res is None:
        sys.exit(1)
    n = res.get("deactivated", 0)
    if n:
        print(f"{C_GREEN}✓ disabled{C_RESET} MCP server {C_CYAN}{name}{C_RESET} "
              f"(the agents no longer see its tools).")
    else:
        print(f"{C_YELLOW}No MCP server named '{name}'.{C_RESET}")


# ── Entry point ─────────────────────────────────────────────────────────────
def cmd_mcp(args, cfg):
    account_token = cfg.get("account_token")
    base_url = cfg["base_url"]
    sub = (getattr(args, "target", None) or "").strip().lower()

    handlers = {
        "expose": _cmd_expose,
        "add": _cmd_add,
        "list": _cmd_list,
        "test": _cmd_test,
        "remove": _cmd_remove,
        "rm": _cmd_remove,
        "delete": _cmd_remove,
    }
    fn = handlers.get(sub)
    if not fn:
        print(f"{C_BOLD}viclix mcp{C_RESET} — connect external MCP servers to the agents\n")
        print("  viclix mcp expose <name> --port <p> [--path /mcp] [--provider ngrok] [--project]")
        print("                            expose a LOCAL MCP server via a tunnel (foreground)")
        print('  viclix mcp add <name> <https-url> [--header "K: V"] [--project]')
        print("                            register an already-public MCP server")
        print("  viclix mcp list           list your registered servers")
        print("  viclix mcp test <name>    connect and list the server's tools")
        print("  viclix mcp remove <name>  disable a server")
        print("\nScope: default = all your projects; --project = this repo only.")
        return
    fn(args, base_url, account_token)

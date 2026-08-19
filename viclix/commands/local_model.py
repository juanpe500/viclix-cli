"""`viclix local-model` — expose a LOCAL model (Ollama / llama.cpp / LM Studio)
to the Viclix agents through a public tunnel + a token-guarded auth proxy.

Why a proxy: the Viclix agent runs on the remote box, so it can't reach the
user's localhost — a public tunnel bridges that. But a quick tunnel (cloudflared
trycloudflare / ngrok) does NO auth at the edge, and Ollama ignores the
Authorization header, so the exposed endpoint would be wide open. This command
runs a tiny local proxy that validates `Authorization: Bearer <token>` and
forwards (streaming) to the local server; the tunnel points at the proxy, not at
Ollama. The same token is stored (encrypted) as the BYOK provider's api_key, so
only Viclix can drive the model.

Flow: detect local server → pick model → mint token → start auth proxy → start
tunnel → register BYOK provider 'local' (base_url = <public>/v1, api_key = token)
→ run in foreground → on Ctrl+C tear everything down and deactivate the provider.

Imports: console, config, api. Stdlib + requests only (no new deps).
"""
import sys
import time

import requests

from ..console import logger, C_BOLD, C_CYAN, C_GREEN, C_YELLOW, C_RESET
from ..api import api_register_local_provider, api_deactivate_local_provider
from ._tunnel import _start_proxy, _start_cloudflared, _start_ngrok, _kill

# Known local OpenAI-compatible servers: default port + how to list models.
_SERVERS = {
    "ollama":   {"port": 11434, "tags": "/api/tags"},   # also exposes /v1
    "lmstudio": {"port": 1234,  "tags": None},           # /v1/models only
    "llamacpp": {"port": 8080,  "tags": None},           # /v1/models only
}


# ── Local server detection / model listing ──────────────────────────────────
def _probe(port, tags_path):
    """Return the list of model ids a local server on ``port`` exposes, or None
    if nothing answers. Tries the OpenAI `/v1/models` shape first, then the
    server-specific tags endpoint (Ollama)."""
    base = f"http://127.0.0.1:{port}"
    try:
        r = requests.get(f"{base}/v1/models", timeout=3)
        if r.status_code == 200:
            data = r.json() or {}
            ids = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            if ids:
                return ids
    except requests.RequestException:
        pass
    if tags_path:
        try:
            r = requests.get(f"{base}{tags_path}", timeout=3)
            if r.status_code == 200:
                data = r.json() or {}
                return [m.get("name") for m in (data.get("models") or []) if m.get("name")]
        except requests.RequestException:
            pass
    return None


def _detect(serve, port_override):
    """Resolve (server_name, port, [model ids]).

    ``serve`` is 'auto' or one of _SERVERS. Autodetect probes each known server
    and picks the first that answers."""
    if serve and serve != "auto":
        spec = _SERVERS.get(serve)
        if not spec:
            logger.error(f"Unknown --serve '{serve}'. Use: {', '.join(_SERVERS)} or auto.")
            sys.exit(1)
        port = port_override or spec["port"]
        models = _probe(port, spec["tags"])
        if models is None:
            logger.error(f"No {serve} server answering on 127.0.0.1:{port}. "
                         f"Is it running? (e.g. `ollama serve`)")
            sys.exit(1)
        return serve, port, models
    # autodetect
    for name, spec in _SERVERS.items():
        port = port_override or spec["port"]
        models = _probe(port, spec["tags"])
        if models is not None:
            logger.info(f"Detected {name} on 127.0.0.1:{port}.")
            return name, port, models
    logger.error("No local model server detected on the known ports "
                 "(ollama 11434, lmstudio 1234, llamacpp 8080). Start one first, "
                 "or pass --serve/--serve-port.")
    sys.exit(1)


def _bare_entry(mid):
    """Minimal picker entry (no metadata) — the fallback when enrichment fails."""
    return {
        "id": mid, "name": mid, "created": 0, "context_length": 0,
        "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
        "supported_parameters": [], "reasoning": None,
        "pricing": {"prompt": "0", "completion": "0"},
    }


def _entry(mid, ctx=0, tools=False, vision=False, reasoning=False):
    """Build a picker-shaped model entry (matches _enrich_openai_style_models /
    model_selector.js _facts: context_length, architecture, supported_parameters,
    reasoning, free pricing)."""
    supported = []
    if tools:
        supported.append("tools")
    if reasoning:
        supported.append("reasoning_effort")
    return {
        "id": mid, "name": mid, "created": 0,
        "context_length": int(ctx or 0),
        "architecture": {
            "input_modalities": ["text", "image"] if vision else ["text"],
            "output_modalities": ["text"],
        },
        "supported_parameters": supported,
        "reasoning": {"mandatory": False} if reasoning else None,
        "pricing": {"prompt": "0", "completion": "0"},  # local = free
    }


def _enrich_ollama(port, ids):
    """Per-model context + capabilities from Ollama's native POST /api/show."""
    base = f"http://127.0.0.1:{port}"
    out = []
    for mid in ids:
        try:
            r = requests.post(f"{base}/api/show", json={"model": mid}, timeout=5)
            if r.status_code != 200:
                out.append(_bare_entry(mid))
                continue
            d = r.json() or {}
            caps = set(d.get("capabilities") or [])
            ctx = 0
            for k, v in (d.get("model_info") or {}).items():
                if k.endswith(".context_length") and isinstance(v, int):
                    ctx = v
                    break
            out.append(_entry(mid, ctx=ctx, tools="tools" in caps,
                              vision="vision" in caps,
                              reasoning="thinking" in caps or "reasoning" in caps))
        except (requests.RequestException, ValueError):
            out.append(_bare_entry(mid))
    return out


def _enrich_lmstudio(port, ids):
    """Context + type (vlm→vision) from LM Studio's native GET /api/v0/models."""
    base = f"http://127.0.0.1:{port}"
    meta = {}
    try:
        r = requests.get(f"{base}/api/v0/models", timeout=5)
        if r.status_code == 200:
            for m in (r.json() or {}).get("data", []):
                if m.get("id"):
                    meta[m["id"]] = m
    except (requests.RequestException, ValueError):
        pass
    out = []
    for mid in ids:
        m = meta.get(mid)
        if not m:
            out.append(_bare_entry(mid))
            continue
        ctx = m.get("max_context_length") or m.get("loaded_context_length") or 0
        vision = (m.get("type") == "vlm")
        # LM Studio doesn't report tool/reasoning support; leave conservative.
        out.append(_entry(mid, ctx=ctx, vision=vision))
    return out


def _enrich_llamacpp(port, ids):
    """Single-model context from llama.cpp's GET /props (n_ctx of the loaded model)."""
    ctx = 0
    try:
        r = requests.get(f"http://127.0.0.1:{port}/props", timeout=5)
        if r.status_code == 200:
            d = r.json() or {}
            ctx = (d.get("default_generation_settings") or {}).get("n_ctx") \
                or d.get("n_ctx") or 0
    except (requests.RequestException, ValueError):
        pass
    return [_entry(mid, ctx=ctx) for mid in ids]


def _enrich_models(server_name, port, ids):
    """Return a picker-shaped catalog for the local models, enriched with
    context + capabilities from the server's native (non-OpenAI) endpoints. Falls
    back to bare entries per model on any failure."""
    if not ids:
        return []
    try:
        if server_name == "ollama":
            return _enrich_ollama(port, ids)
        if server_name == "lmstudio":
            return _enrich_lmstudio(port, ids)
        if server_name == "llamacpp":
            return _enrich_llamacpp(port, ids)
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        pass
    return [_bare_entry(mid) for mid in ids]


def _pick_model(models, preselect):
    """Return the model id to register as the default. ``preselect`` (--model)
    wins; otherwise auto-pick a single model, or prompt among several."""
    if preselect:
        return preselect
    if not models:
        return None
    if len(models) == 1:
        return models[0]
    print(f"\n{C_BOLD}Local models:{C_RESET}")
    for i, m in enumerate(models, 1):
        print(f"  {C_CYAN}{i}{C_RESET}. {m}")
    try:
        raw = input(f"Pick a default model [1-{len(models)}, default 1]: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return models[0]
    if not raw:
        return models[0]
    try:
        idx = int(raw)
        if 1 <= idx <= len(models):
            return models[idx - 1]
    except ValueError:
        pass
    logger.warning(f"Invalid choice — using {models[0]}.")
    return models[0]


# ── Command ─────────────────────────────────────────────────────────────────
def cmd_local_model(args, cfg):
    """Bridge a local model to the Viclix agents via a token-guarded tunnel."""
    import secrets

    account_token = cfg.get("account_token")
    base_url = cfg["base_url"]

    serve = (getattr(args, "serve", None) or "auto").strip().lower()
    provider_kind = (getattr(args, "provider", None) or "cloudflare").strip().lower()
    serve_port = getattr(args, "serve_port", None)
    proxy_port = getattr(args, "proxy_port", None)
    label = getattr(args, "label", None) or "Local"

    # Scope: default to BOTH coding + agents unless the user narrows it.
    use_coding = bool(getattr(args, "coding", False))
    use_agents = bool(getattr(args, "agents", False))
    if not use_coding and not use_agents:
        use_coding = use_agents = True

    server_name, port, models = _detect(serve, serve_port)
    model = _pick_model(models, getattr(args, "model", None))
    logger.info(f"Using local model: {model or '(agent picks)'}")

    # Enrich the catalog (context + tools/vision/reasoning) from the server's
    # native endpoints so the web picker shows real metadata — the OpenAI
    # /v1/models list can't provide it.
    catalog = _enrich_models(server_name, port, models)
    _n_ctx = sum(1 for e in catalog if e.get("context_length"))
    if catalog:
        logger.info(f"Enriched {len(catalog)} model(s) "
                    f"({_n_ctx} with context metadata).")

    token = secrets.token_urlsafe(32)
    upstream = f"http://127.0.0.1:{port}"
    proxy_srv, actual_proxy_port = _start_proxy(upstream, token, proxy_port)
    logger.info(f"Auth proxy on 127.0.0.1:{actual_proxy_port} → {upstream}")

    tunnel_proc = None
    registered = False
    try:
        if provider_kind == "ngrok":
            tunnel_proc, public = _start_ngrok(actual_proxy_port)
        else:
            tunnel_proc, public = _start_cloudflared(actual_proxy_port)
        api_base = public.rstrip("/") + "/v1"
        print(f"\n{C_GREEN}✓ tunnel up:{C_RESET} {public}")

        payload = {
            "provider": "local",
            "base_url": api_base,
            "api_key": token,
            "default_model": model,
            "use_for_coding": use_coding,
            "use_for_agents": use_agents,
            "models": catalog,  # enriched catalog for the web picker
        }
        res = api_register_local_provider(base_url, account_token, payload)
        if not res:
            logger.error("Could not register the provider — tearing down.")
            return
        registered = True
        scope = "coding + agents" if (use_coding and use_agents) else \
                ("coding" if use_coding else "agents")
        print(f"{C_GREEN}✓ registered{C_RESET} BYOK provider "
              f"{C_CYAN}local{C_RESET} ({label}) · model "
              f"{C_CYAN}{model or 'agent-picked'}{C_RESET} · scope {scope}")
        print(f"\n{C_YELLOW}⚠ This exposes your local model over the internet while the "
              f"tunnel is up.{C_RESET}\n  Only requests with the secret token are accepted. "
              f"Press Ctrl+C to stop and close the tunnel.\n")

        # Foreground: keep the proxy + tunnel alive until interrupted or the
        # tunnel dies on its own.
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
            api_deactivate_local_provider(base_url, account_token, "local")
            logger.info("Deactivated the 'local' provider (agents fall back to Viclix models).")
        print(f"{C_GREEN}✓ closed.{C_RESET}")

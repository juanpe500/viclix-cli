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
import os
import re
import sys
import time
import shutil
import socket
import platform
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from ..console import logger, config_home, C_BOLD, C_CYAN, C_GREEN, C_YELLOW, C_RESET
from ..api import api_register_local_provider, api_deactivate_local_provider

# Known local OpenAI-compatible servers: default port + how to list models.
_SERVERS = {
    "ollama":   {"port": 11434, "tags": "/api/tags"},   # also exposes /v1
    "lmstudio": {"port": 1234,  "tags": None},           # /v1/models only
    "llamacpp": {"port": 8080,  "tags": None},           # /v1/models only
}

# Hop-by-hop headers never forwarded (RFC 7230 §6.1) + ones we manage ourselves.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
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


# ── Auth proxy ──────────────────────────────────────────────────────────────
def _free_port(preferred):
    """Return ``preferred`` if bindable, else an OS-assigned free port."""
    for candidate in (preferred, 0):
        if candidate is None:
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.bind(("127.0.0.1", candidate))
            port = s.getsockname()[1]
            s.close()
            return port
        except OSError:
            s.close()
    return 0


class _ProxyHandler(BaseHTTPRequestHandler):
    """Validates the Bearer token, then forwards to the local server, streaming
    the response back (so SSE chat completions relay chunk-by-chunk)."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence default stderr access log
        pass

    def _auth_ok(self):
        return self.headers.get("Authorization", "") == f"Bearer {self.server.token}"

    def _reject(self, code, msg):
        body = ('{"error":%r}' % msg).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _relay(self, method):
        if not self._auth_ok():
            self.server.rejected += 1
            return self._reject(401, "unauthorized")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        target = self.server.upstream + self.path
        fwd = {k: v for k, v in self.headers.items()
               if k.lower() not in _HOP
               and k.lower() not in ("host", "authorization", "content-length")}
        try:
            up = requests.request(method, target, data=body, headers=fwd,
                                  stream=True, timeout=(10, 600))
        except requests.RequestException as e:
            return self._reject(502, f"upstream error: {e}")
        self.send_response(up.status_code)
        for k, v in up.headers.items():
            if k.lower() in _HOP or k.lower() in ("content-length", "connection"):
                continue
            self.send_header(k, v)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        try:
            for chunk in up.iter_content(chunk_size=8192):
                if not chunk:
                    continue
                self.wfile.write(b"%X\r\n" % len(chunk) + chunk + b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            up.close()

    def do_GET(self):
        self._relay("GET")

    def do_POST(self):
        self._relay("POST")

    def do_DELETE(self):
        self._relay("DELETE")


def _start_proxy(upstream, token, proxy_port):
    """Start the auth proxy in a daemon thread. Returns (server, actual_port)."""
    port = _free_port(proxy_port)
    server = ThreadingHTTPServer(("127.0.0.1", port), _ProxyHandler)
    server.upstream = upstream
    server.token = token
    server.rejected = 0
    server.daemon_threads = True
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    return server, port


# ── Tunnel (cloudflared / ngrok) ────────────────────────────────────────────
def _cloudflared_path():
    p = shutil.which("cloudflared")
    if p:
        return p
    ext = ".exe" if os.name == "nt" else ""
    cached = os.path.join(config_home(), "bin", f"cloudflared{ext}")
    return cached if os.path.exists(cached) else None


def _download_cloudflared():
    """Fetch the cloudflared binary into ~/.viclix/bin for linux/windows (single
    static binary). macOS ships a .tgz — point the user at brew instead."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64",
            "arm64": "arm64", "i386": "386", "i686": "386"}.get(machine, "amd64")
    if system == "darwin":
        logger.error("cloudflared not found. Install it: brew install cloudflared "
                     "(or use --provider ngrok).")
        return None
    if system == "windows":
        asset = f"cloudflared-windows-{arch}.exe"
    elif system == "linux":
        asset = f"cloudflared-linux-{arch}"
    else:
        logger.error(f"Unsupported platform {system}. Install cloudflared manually.")
        return None
    url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/{asset}"
    ext = ".exe" if system == "windows" else ""
    dest_dir = os.path.join(config_home(), "bin")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"cloudflared{ext}")
    logger.info(f"Downloading cloudflared ({asset})…")
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
    except (requests.RequestException, OSError) as e:
        logger.error(f"Could not download cloudflared: {e}")
        return None
    if system != "windows":
        try:
            os.chmod(dest, 0o755)
        except OSError:
            pass
    logger.info(f"Installed cloudflared → {dest}")
    return dest


def _start_cloudflared(port):
    """Start a cloudflared quick tunnel to 127.0.0.1:port. Returns (proc, url)."""
    binary = _cloudflared_path() or _download_cloudflared()
    if not binary:
        sys.exit(1)
    proc = subprocess.Popen(
        [binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{port}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    url_re = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    url = _read_url_from_stream(proc, url_re, timeout=40)
    if not url:
        _kill(proc)
        logger.error("cloudflared did not produce a public URL in time.")
        sys.exit(1)
    return proc, url


def _read_url_from_stream(proc, url_re, timeout):
    """Scan a subprocess's stdout for a URL, up to ``timeout`` seconds. Keeps a
    drain thread alive afterwards so the pipe never fills and blocks the tunnel."""
    found = {"url": None}
    done = threading.Event()

    def _scan():
        for line in iter(proc.stdout.readline, ""):
            if not found["url"]:
                m = url_re.search(line)
                if m:
                    found["url"] = m.group(0)
                    done.set()
            if proc.poll() is not None:
                break
        done.set()

    threading.Thread(target=_scan, daemon=True).start()
    done.wait(timeout)
    return found["url"]


def _start_ngrok(port):
    """Start ngrok and read the public https URL from its local API (:4040)."""
    binary = shutil.which("ngrok")
    if not binary:
        logger.error("ngrok not found. Install it and run `ngrok config add-authtoken …` "
                     "first, or use the default cloudflared provider.")
        sys.exit(1)
    proc = subprocess.Popen(
        [binary, "http", str(port), "--log", "stdout"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    url = None
    for _ in range(40):  # ~20s
        try:
            r = requests.get("http://127.0.0.1:4040/api/tunnels", timeout=2)
            if r.status_code == 200:
                for t in (r.json() or {}).get("tunnels", []):
                    pub = t.get("public_url", "")
                    if pub.startswith("https://"):
                        url = pub
                        break
        except requests.RequestException:
            pass
        if url:
            break
        time.sleep(0.5)
    if not url:
        _kill(proc)
        logger.error("ngrok did not produce a public URL in time.")
        sys.exit(1)
    return proc, url


def _kill(proc):
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


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

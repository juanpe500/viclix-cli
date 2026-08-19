"""Shared tunnel + auth-proxy plumbing for the commands that expose a LOCAL
service (an OpenAI-compatible model, an MCP server, …) to the Viclix cloud.

The Viclix agents run on the remote box, so they can't reach the user's
localhost — a public tunnel (cloudflared trycloudflare / ngrok) bridges that. But
a quick tunnel does NO auth at the edge, so this module also runs a tiny local
proxy that validates ``Authorization: Bearer <token>`` and forwards (streaming)
to the local server; the tunnel points at the proxy, not at the real service.

Used by `viclix local-model` and `viclix mcp expose`. Stdlib + requests only.
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

from ..console import logger, config_home

# Hop-by-hop headers never forwarded (RFC 7230 §6.1) + ones we manage ourselves.
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
}


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
    the response back (so SSE / chunked responses relay chunk-by-chunk)."""

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


def start_tunnel(provider_kind, port):
    """Start the requested tunnel provider to 127.0.0.1:port. Returns (proc, url)."""
    if (provider_kind or "").strip().lower() == "ngrok":
        return _start_ngrok(port)
    return _start_cloudflared(port)


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

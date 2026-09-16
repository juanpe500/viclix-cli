"""``viclix stats`` — a live wall for your whole fleet on one screen.

Reads a fleet file (``~/.viclix/stats.json``), pulls each app's token-protected
``/_stats/api`` (the one ``viclix_sdk.mount_stats`` serves), and shows a dense
NOC-style board — one row per app with a status LED, its live metrics, latency
and a sparkline. Built for a vertical monitor left on 24/7.

    viclix stats                 # start the wall + open the browser
    viclix stats --no-browser    # just serve it (e.g. on a headless box)
    viclix stats --port 8900     # pin the port

The fleet file (created for you on first run if missing):

    {
      "refresh": 15,
      "apps": [
        {"name": "Judgly", "url": "https://app.judgly.com", "token": "…"},
        {"name": "Argo",   "url": "https://argoclan.com",   "token": "…"}
      ]
    }

``token`` is the same value passed to ``mount_stats(app, token=…)`` in that app.
Optionally use ``"token_env": "JUDGLY_STATS_TOKEN"`` to read it from this
machine's environment instead of writing the secret into the file. ``path``
defaults to ``/_stats/api``.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import requests

from ..console import (logger, config_home, C_CYAN, C_GREEN, C_YELLOW,
                       C_RED, C_RESET)

FLEET_PATH = os.path.join(config_home(), "stats.json")
DEFAULT_REFRESH = 15
HISTORY = 20

_TEMPLATE = {
    "refresh": DEFAULT_REFRESH,
    "apps": [
        {"name": "Example", "url": "https://app.example.com",
         "token": "paste-the-mount_stats-token-here", "path": "/_stats/api"},
    ],
}


def _free_port(start=8900, host="127.0.0.1"):
    port = start
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((host, port))
                return port
            except OSError:
                port += 1
    return start


def _load_fleet():
    if not os.path.exists(FLEET_PATH):
        os.makedirs(config_home(), exist_ok=True)
        with open(FLEET_PATH, "w", encoding="utf-8") as f:
            json.dump(_TEMPLATE, f, indent=2)
        logger.info(f"Created a starter fleet file at {FLEET_PATH}")
        logger.info("Edit it (add your apps + tokens) and run 'viclix stats' again.")
        return None
    try:
        with open(FLEET_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        logger.error(f"Could not read {FLEET_PATH}: {e}")
        return None
    apps = [a for a in (data.get("apps") or []) if a.get("url")]
    if not apps:
        logger.error(f"No apps in {FLEET_PATH}. Add at least one under \"apps\".")
        return None
    real = [a for a in apps if "example.com" not in a.get("url", "")]
    if not real:
        logger.error(f"{FLEET_PATH} still has only the example app. Edit it first.")
        return None
    data["apps"] = real
    return data


class _State:
    """Latest poll result per app, guarded by a lock."""

    def __init__(self, apps):
        self.lock = threading.Lock()
        self.apps = {}
        for a in apps:
            self.apps[a["name"]] = {
                "name": a["name"], "url": a["url"].rstrip("/"),
                "ok": None, "latency_ms": None, "updated": 0.0,
                "metrics": [], "activity": [], "error": None,
                "history": [],
            }

    def snapshot(self):
        now = time.time()
        with self.lock:
            out = []
            up = down = 0
            for st in self.apps.values():
                age = int(now - st["updated"]) if st["updated"] else None
                row = dict(st)
                row["age_s"] = age
                # stale (missed 3 polls) counts as down even without an error
                row["ok"] = bool(st["ok"])
                out.append(row)
                up += 1 if row["ok"] else 0
                down += 0 if row["ok"] else 1
            return {"ts": now, "up": up, "down": down, "apps": out}


def _poll_once(app, state, timeout=10):
    name = app["name"]
    url = app["url"].rstrip("/") + (app.get("path") or "/_stats/api")
    token = app.get("token")
    if not token and app.get("token_env"):
        token = os.environ.get(app["token_env"])
    headers = {"X-Viclix-Token": token} if token else {}
    t0 = time.perf_counter()
    try:
        r = requests.get(url, headers=headers, timeout=timeout)
        dt = round((time.perf_counter() - t0) * 1000, 1)
        if r.status_code != 200:
            reason = "not found (token? not instrumented?)" if r.status_code == 404 \
                else f"HTTP {r.status_code}"
            with state.lock:
                st = state.apps[name]
                st["ok"] = False; st["latency_ms"] = dt
                st["updated"] = time.time(); st["error"] = reason
            return
        d = r.json()
        with state.lock:
            st = state.apps[name]
            st["ok"] = True; st["latency_ms"] = dt; st["updated"] = time.time()
            st["error"] = None
            st["metrics"] = d.get("metrics", [])
            st["activity"] = d.get("activity", [])
            first = _first_number(st["metrics"])
            val = first if first is not None else dt
            st["history"] = (st["history"] + [val])[-HISTORY:]
    except Exception as e:
        dt = round((time.perf_counter() - t0) * 1000, 1)
        with state.lock:
            st = state.apps[name]
            st["ok"] = False; st["latency_ms"] = None
            st["updated"] = time.time()
            st["error"] = type(e).__name__.replace("Error", "").lower() or "unreachable"


def _first_number(metrics):
    for m in metrics:
        v = m.get("value")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def _poller(fleet, state, stop):
    refresh = max(3, int(fleet.get("refresh", DEFAULT_REFRESH)))
    apps = fleet["apps"]
    while not stop.is_set():
        threads = []
        for a in apps:
            t = threading.Thread(target=_poll_once, args=(a, state), daemon=True)
            t.start(); threads.append(t)
        for t in threads:
            t.join(timeout=12)
        stop.wait(refresh)


def _make_handler(state, refresh):
    page = _PAGE.replace("__REFRESH__", str(int(refresh) * 1000))

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass  # keep the terminal clean

        def _send(self, body, ctype):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path.startswith("/state"):
                self._send(json.dumps(state.snapshot()), "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            else:
                self.send_response(404); self.end_headers()

    return H


def cmd_stats(args):
    fleet = _load_fleet()
    if fleet is None:
        return
    refresh = max(3, int(fleet.get("refresh", DEFAULT_REFRESH)))
    state = _State(fleet["apps"])

    stop = threading.Event()
    poller = threading.Thread(target=_poller, args=(fleet, state, stop), daemon=True)
    poller.start()

    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or _free_port(8900, host)
    httpd = ThreadingHTTPServer((host, port), _make_handler(state, refresh))
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}"

    print(f"{C_GREEN}✓ fleet wall{C_RESET} → {C_CYAN}{url}{C_RESET}  "
          f"({len(fleet['apps'])} apps · pull {refresh}s)")
    print(f"  fleet file: {FLEET_PATH}")
    print(f"  {C_YELLOW}Ctrl+C to stop{C_RESET}")
    if not getattr(args, "no_browser", False):
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print(f"\n{C_YELLOW}stopping…{C_RESET}")
    finally:
        stop.set()
        httpd.shutdown()


# ── the NOC wall (Direction A) ────────────────────────────────────────
_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet · viclix stats</title>
<style>
:root{--bg:#050608;--row:#0b0e15;--line:#171d29;--ink:#e9eefb;--dim:#6b7688;
  --up:#33d17a;--down:#ff5865;--warn:#ffb547;--accent:#5b8cff;
  --mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
*{box-sizing:border-box;margin:0}
html,body{height:100%}
body{background:var(--bg);color:var(--ink);
  font:14px/1.35 ui-sans-serif,system-ui,Segoe UI,Roboto,sans-serif;
  padding:18px 16px 20px;display:flex;flex-direction:column;gap:12px}
header{display:flex;align-items:center;justify-content:space-between;
  padding-bottom:12px;border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:baseline;gap:10px}
.brand h1{font-size:19px;letter-spacing:3px;font-weight:700}
.brand span{color:var(--dim);font:12px/1 var(--mono);letter-spacing:2px}
.summary{display:flex;gap:8px}
.pill{font:12px/1 var(--mono);letter-spacing:1px;padding:6px 10px;border-radius:999px;border:1px solid var(--line)}
.pill.ok{color:var(--up);border-color:#1c3a2a;background:#0c1a12}
.pill.bad{color:var(--down);border-color:#3a1c22;background:#1a0c0f}
.list{display:flex;flex-direction:column;gap:8px;flex:1}
.app{background:var(--row);border:1px solid var(--line);border-radius:12px;padding:12px 14px;
  display:grid;grid-template-columns:14px 1fr auto;gap:12px;align-items:center}
.app.dn{background:linear-gradient(90deg,#1a0c0f,var(--row))}
.led{width:10px;height:10px;border-radius:50%;background:var(--up);box-shadow:0 0 10px 1px var(--up)}
.led.dn{background:var(--down);box-shadow:0 0 10px 1px var(--down);animation:blink 1.1s steps(2) infinite}
.led.wn{background:var(--warn);box-shadow:0 0 10px 1px var(--warn)}
.led.un{background:#3a4152;box-shadow:none}
@keyframes blink{50%{opacity:.25}}
.main{min-width:0}
.name{display:flex;align-items:center;gap:10px}
.name b{font-size:16px;letter-spacing:.3px}
.host{font:11px/1 var(--mono);color:var(--dim)}
.metrics{display:flex;gap:22px;margin-top:8px;flex-wrap:wrap}
.m{display:flex;flex-direction:column;gap:2px}
.m .k{font:10px/1 var(--mono);letter-spacing:1.5px;color:var(--dim);text-transform:uppercase}
.m .v{font:19px/1 var(--mono);font-weight:600;font-variant-numeric:tabular-nums}
.err{color:var(--down);font:12px/1.6 var(--mono);margin-top:8px}
.right{text-align:right;display:flex;flex-direction:column;align-items:flex-end;gap:6px}
.lat{font:12px/1 var(--mono);color:var(--dim)}
.lat b{color:var(--ink)}
.spark{display:flex;align-items:flex-end;gap:2px;height:26px}
.spark i{width:4px;background:#22304a;border-radius:1px;min-height:3px}
.spark.dn i{background:#3a1c22}
.age{font:10px/1 var(--mono);color:var(--dim);letter-spacing:1px}
footer{display:flex;justify-content:space-between;font:11px/1 var(--mono);color:var(--dim);
  letter-spacing:1px;padding-top:6px;border-top:1px solid var(--line)}
.off{opacity:.55;text-align:center;color:var(--dim);padding:40px;font-style:italic}
</style></head>
<body>
<header>
  <div class="brand"><h1>FLEET</h1><span id="sub">live</span></div>
  <div class="summary" id="sum"></div>
</header>
<div class="list" id="list"><div class="off">connecting…</div></div>
<footer><span>viclix stats</span><span id="clk"></span></footer>
<script>
const REFRESH=__REFRESH__;
function fmtVal(v,f){if(v==null)return"—";
  if(f==="money")return"$"+Number(v).toLocaleString();
  if(f==="float")return Number(v).toLocaleString(undefined,{maximumFractionDigits:2});
  if(f==="int")return Math.round(Number(v)).toLocaleString();
  if(typeof v==="object")return Object.values(v).map(x=>fmtVal(x,f)).join(" / ");
  return String(v);}
function host(u){try{return new URL(u).host}catch(e){return u}}
function spark(hist,dn){
  if(!hist||!hist.length)hist=[0];
  const max=Math.max(...hist,1),min=Math.min(...hist,0),rng=(max-min)||1;
  return `<div class="spark ${dn?'dn':''}">`+hist.map(v=>{
    const h=4+Math.round(20*((v-min)/rng));return `<i style="height:${h}px"></i>`}).join("")+`</div>`;
}
function metricCells(ms){
  if(!ms||!ms.length)return "";
  return ms.slice(0,5).map(m=>`<div class="m"><span class="k">${m.label||m.key}</span>
    <span class="v">${fmtVal(m.value,m.fmt)}</span></div>`).join("");
}
function age(s){if(s==null)return "—";if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"m";return Math.floor(s/3600)+"h";}
async function tick(){
  let d;try{d=await(await fetch("/state")).json();}catch(e){return;}
  document.getElementById("sub").textContent=`live · pull ${Math.round(REFRESH/1000)}s`;
  document.getElementById("sum").innerHTML=
    `<span class="pill ok">${d.up} UP</span>`+(d.down?`<span class="pill bad">${d.down} DOWN</span>`:``);
  const rows=d.apps.map(a=>{
    const pending=a.ok===null||a.updated===0;
    const cls=pending?"un":a.ok?"":"dn";
    const stale=a.age_s!=null&&a.age_s>REFRESH/1000*3;
    const led=pending?"un":(!a.ok?"dn":(stale||(a.latency_ms>400)?"wn":""));
    const right = a.ok
      ? `<span class="lat">lat <b>${a.latency_ms??'—'}ms</b></span>${spark(a.history,false)}<span class="age">↻ ${age(a.age_s)}</span>`
      : `<span style="color:var(--down);font:12px/1 var(--mono)">${pending?'…':(a.error||'down')}</span>${spark(a.history,true)}<span class="age">↻ ${age(a.age_s)}</span>`;
    const body = a.ok
      ? `<div class="metrics">${metricCells(a.metrics)||'<span class="host">no metrics</span>'}</div>`
      : (pending?``:`<div class="err">${a.error||'no response'}</div>`);
    return `<div class="app ${a.ok?'':'dn'}">
      <div class="led ${led}"></div>
      <div class="main"><div class="name"><b>${a.name}</b><span class="host">${host(a.url)}</span></div>${body}</div>
      <div class="right">${right}</div></div>`;
  }).join("");
  document.getElementById("list").innerHTML=rows||`<div class="off">no apps</div>`;
}
const clk=()=>document.getElementById("clk").textContent=new Date().toLocaleTimeString();
clk();setInterval(clk,1000);
tick();setInterval(tick,Math.max(2000,REFRESH));
</script>
</body></html>"""

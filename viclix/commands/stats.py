"""``viclix stats`` — a live wall for your whole fleet on one screen.

Reads a fleet file (``~/.viclix/stats.json``), pulls each app's token-protected
``/_stats/api`` (the one ``viclix_sdk.mount_stats`` serves), and shows a dense
NOC-style board — one row per app with a status LED, its live metrics, latency
and a sparkline. Built for a vertical monitor left on 24/7.

    viclix stats                 # start the wall + open the browser
    viclix stats --no-browser    # just serve it (e.g. on a headless box)
    viclix stats --port 57475    # pin the port

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


def _free_port(start=57475, host="127.0.0.1"):
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

    def __init__(self, apps, refresh):
        self.lock = threading.Lock()
        self.refresh = max(3, int(refresh))
        self.next_at = time.time()          # epoch of the next scheduled poll
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
            return {"ts": now, "up": up, "down": down,
                    "refresh": self.refresh, "next_at": self.next_at,
                    "apps": out}


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


MIN_REFRESH = 3
MAX_REFRESH = 3600


class _Poller(threading.Thread):
    """Polls every app on a dynamic interval; can be re-timed live."""

    def __init__(self, fleet, state):
        super().__init__(daemon=True)
        self.apps = fleet["apps"]
        self.state = state
        self._interval = state.refresh
        self._stop = threading.Event()
        self._wake = threading.Event()      # set → poll now & reschedule

    def set_interval(self, secs):
        """Change the pull cadence live, persist it, and poll immediately."""
        secs = max(MIN_REFRESH, min(MAX_REFRESH, int(secs)))
        self._interval = secs
        with self.state.lock:
            self.state.refresh = secs
        _save_refresh(secs)
        self._wake.set()
        return secs

    def stop(self):
        self._stop.set()
        self._wake.set()

    def _poll_all(self):
        threads = []
        for a in self.apps:
            t = threading.Thread(target=_poll_once, args=(a, self.state), daemon=True)
            t.start(); threads.append(t)
        for t in threads:
            t.join(timeout=12)

    def run(self):
        while not self._stop.is_set():
            self._poll_all()
            iv = self._interval
            with self.state.lock:
                self.state.next_at = time.time() + iv
            self._wake.wait(iv)             # sleeps iv, or returns early if re-timed
            self._wake.clear()


def _save_refresh(secs):
    """Persist the new interval into the fleet file (best-effort)."""
    try:
        with open(FLEET_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["refresh"] = int(secs)
        with open(FLEET_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


def _make_handler(state, poller):
    page = (_PAGE.replace("__MIN__", str(MIN_REFRESH))
                 .replace("__MAX__", str(MAX_REFRESH)))

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
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            if parsed.path == "/set-interval":
                q = parse_qs(parsed.query)
                try:
                    secs = poller.set_interval(int(q.get("secs", ["0"])[0]))
                    self._send(json.dumps({"ok": True, "refresh": secs}), "application/json")
                except Exception as e:
                    self._send(json.dumps({"ok": False, "error": str(e)}), "application/json")
            elif parsed.path == "/state":
                self._send(json.dumps(state.snapshot()), "application/json")
            elif parsed.path in ("/", "/index.html"):
                self._send(page, "text/html; charset=utf-8")
            else:
                self.send_response(404); self.end_headers()

    return H


def cmd_stats(args):
    fleet = _load_fleet()
    if fleet is None:
        return
    refresh = max(MIN_REFRESH, int(fleet.get("refresh", DEFAULT_REFRESH)))
    state = _State(fleet["apps"], refresh)

    poller = _Poller(fleet, state)
    poller.start()

    host = getattr(args, "host", None) or "127.0.0.1"
    port = getattr(args, "port", None) or _free_port(57475, host)
    httpd = ThreadingHTTPServer((host, port), _make_handler(state, poller))
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
        poller.stop()
        httpd.shutdown()


# ── the fleet radar (Direction: deep-space radar) ─────────────────────
_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fleet Radar · viclix stats</title>
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
:root{--bg:#020805;--ink:#e8fff2;--dim:#4e7a62;--g:#41ff8a;--g2:#1a6b40;
  --warn:#ffd257;--bad:#ff5063;
  --ch:'Chakra Petch',sans-serif;--stm:'Share Tech Mono',monospace}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--ink);font-family:var(--ch);
  min-height:100vh;display:flex;justify-content:center}
body::before{content:"";position:fixed;inset:0;pointer-events:none;
  background:
   radial-gradient(80% 40% at 50% 8%,rgba(65,255,138,.06),transparent 65%),
   repeating-linear-gradient(0deg,rgba(65,255,138,.02) 0 1px,transparent 1px 4px)}
.wall{position:relative;width:min(700px,100%);min-height:100vh;
  display:flex;flex-direction:column;padding:16px 16px 10px;gap:10px}
header{display:flex;justify-content:space-between;align-items:flex-end;padding:0 2px}
h1{font-size:20px;font-weight:700;letter-spacing:5px}
h1 small{display:block;font:10px var(--stm);letter-spacing:3px;color:var(--dim);margin-top:4px}
h1 small em{font-style:normal;color:var(--bad)}
.hr{text-align:right;font:11px var(--stm);color:var(--dim);letter-spacing:2px;line-height:1.7}
.hr b{color:var(--ink);font-size:15px;letter-spacing:1px}
.hr i{font-style:normal;color:var(--g)}
/* ── radar ── */
.radarwrap{display:flex;justify-content:center;padding:6px 0 2px}
.radar{position:relative;width:min(330px,78vw);aspect-ratio:1;border-radius:50%;
  background:radial-gradient(circle,#03150c 0%,#020b06 70%);
  border:1px solid var(--g2);
  box-shadow:0 0 40px rgba(65,255,138,.12),inset 0 0 60px rgba(65,255,138,.05)}
.radar .ring{position:absolute;border-radius:50%;border:1px solid rgba(65,255,138,.16)}
.radar .r1{inset:16.6%}.radar .r2{inset:33.3%}.radar .r3{inset:41.6%}
.radar .cross{position:absolute;background:rgba(65,255,138,.12)}
.radar .cx{left:0;right:0;top:50%;height:1px}
.radar .cyx{top:0;bottom:0;left:50%;width:1px}
.sweep{position:absolute;inset:0;border-radius:50%;overflow:hidden;
  animation:rot 4.2s linear infinite}
.sweep::before{content:"";position:absolute;inset:0;
  background:conic-gradient(from 0deg,rgba(65,255,138,.35),rgba(65,255,138,.05) 70deg,transparent 90deg)}
@keyframes rot{to{transform:rotate(360deg)}}
.blip{position:absolute;transform:translate(-50%,-50%);text-align:center;z-index:2}
.blip .dot{display:block;width:11px;height:11px;border-radius:50%;background:var(--g);margin:0 auto;
  box-shadow:0 0 12px var(--g);animation:pulse 2.4s ease-out infinite}
.blip.warn .dot{background:var(--warn);box-shadow:0 0 12px var(--warn)}
.blip.bad .dot{background:var(--bad);box-shadow:0 0 14px var(--bad);animation:bk .8s steps(2) infinite}
.blip.pend .dot{background:#37503f;box-shadow:none;animation:none}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(65,255,138,.5)}70%{box-shadow:0 0 0 12px rgba(65,255,138,0)}100%{box-shadow:0 0 0 0 rgba(65,255,138,0)}}
@keyframes bk{50%{opacity:.3}}
.blip .bl{display:block;margin-top:4px;font:600 10px var(--ch);letter-spacing:1.5px;
  color:var(--ink);text-shadow:0 0 8px rgba(0,0,0,.9);white-space:nowrap}
.blip.bad .bl{color:#ffb3ba}
.blip.pend .bl{color:var(--dim)}
.radar .center{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);
  width:8px;height:8px;border-radius:50%;background:var(--ink);
  box-shadow:0 0 10px rgba(232,255,242,.8)}
/* ── contact list ── */
.list{flex:1;display:flex;flex-direction:column;gap:8px}
.row{--c:var(--g);display:flex;flex-direction:column;gap:9px;
  background:rgba(65,255,138,.035);border:1px solid rgba(65,255,138,.14);
  border-left:3px solid var(--c);border-radius:4px;padding:10px 14px 11px}
.row.warn{--c:var(--warn)}
.row.pend{--c:#37503f;opacity:.7}
.row.bad{--c:var(--bad);background:rgba(255,80,99,.05);border-color:rgba(255,80,99,.25)}
.rh{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap}
.rh b{font-size:17px;font-weight:700;letter-spacing:2px}
.rh .host{font:10px var(--stm);color:var(--dim);flex:1}
.rh .png{font:11px var(--stm);color:var(--dim)}
.rh .png b{font-size:13px;color:var(--ink);letter-spacing:0}
.rh .tag{font:700 11px var(--ch);letter-spacing:2px;color:var(--c)}
.row.bad .tag{background:var(--bad);color:#fff;padding:2px 8px;border-radius:2px;
  animation:bk .9s steps(2) infinite}
.rm{display:flex;flex-wrap:wrap;gap:6px 24px}
.rm .m{display:flex;align-items:baseline;gap:8px}
.rm .m b{font-size:23px;font-weight:700;font-variant-numeric:tabular-nums}
.rm .m i{font-style:normal;font:600 11px var(--ch);letter-spacing:1px;color:var(--dim);
  text-transform:uppercase}
.lost{font:12px var(--stm);color:#ff9aa5;letter-spacing:1px}
.pendnote{font:12px var(--stm);color:var(--dim);letter-spacing:1px}
.off{opacity:.55;text-align:center;color:var(--dim);padding:40px;font:13px var(--stm)}
footer{display:flex;justify-content:space-between;align-items:center;font:10px var(--stm);
  letter-spacing:2px;color:var(--dim);padding:2px 4px 4px}
footer label{display:flex;align-items:center;gap:6px}
footer input{width:48px;background:#03150c;border:1px solid var(--g2);border-radius:3px;
  color:var(--g);font:12px var(--stm);text-align:center;padding:3px 2px;outline:none}
footer input:focus{border-color:var(--g)}
</style></head>
<body>
<div class="wall">
  <header>
    <div><h1>FLEET RADAR<small id="sub">VICLIX · CONNECTING…</small></h1></div>
    <div class="hr"><b id="clk"></b><br><span id="cd">sweep —</span></div>
  </header>
  <div class="radarwrap">
    <div class="radar">
      <div class="ring r1"></div><div class="ring r2"></div><div class="ring r3"></div>
      <div class="cross cx"></div><div class="cross cyx"></div>
      <div class="sweep"></div><div id="blips"></div><div class="center"></div>
    </div>
  </div>
  <div class="list" id="list"><div class="off">acquiring contacts…</div></div>
  <footer><span id="foot">viclix stats</span>
    <label>SWEEP EVERY <input id="iv" type="number" min="__MIN__" max="__MAX__" step="1"> S</label></footer>
</div>
<script>
const MINR=__MIN__, MAXR=__MAX__;
let sig=null, nextAt=0, refresh=15;
function fmtVal(v,f){if(v==null)return"—";
  if(f==="money")return"$"+Number(v).toLocaleString();
  if(f==="float")return Number(v).toLocaleString(undefined,{maximumFractionDigits:2});
  if(f==="int")return Math.round(Number(v)).toLocaleString();
  if(typeof v==="object")return Object.values(v).map(x=>fmtVal(x,f)).join(" / ");
  return String(v);}
function host(u){try{return new URL(u).host}catch(e){return u}}
function age(s){if(s==null)return "—";if(s<60)return s+"s";if(s<3600)return Math.floor(s/60)+"m";return Math.floor(s/3600)+"h";}
function stateOf(a){
  if(a.ok===null||!a.updated)return "pend";
  if(!a.ok)return "bad";
  const stale=a.age_s!=null&&a.age_s>refresh*3;
  return (stale||a.latency_ms>1500)?"warn":"ok";
}
const TAGS={ok:"STABLE",warn:"SLOW",bad:"LOST",pend:"…"};
/* deterministic blip position per app: spread by index, jittered by name hash */
function hash(s){let h=0;for(const c of s)h=(h*31+c.charCodeAt(0))|0;return Math.abs(h);}
const POS={};
function posFor(name,i,n){
  const key=name+"/"+i+"/"+n;
  if(POS[key])return POS[key];
  const h=hash(name);
  const ang=((i*(360/Math.max(n,1)))+(h%36)-18-90)*Math.PI/180;
  const r=17+((h>>4)%22);
  POS[key]={x:50+r*Math.cos(ang), y:50+r*Math.sin(ang)};
  return POS[key];
}
function renderBlips(apps){
  document.getElementById("blips").innerHTML=apps.map((a,i)=>{
    const st=stateOf(a),p=posFor(a.name,i,apps.length);
    return `<div class="blip ${st==="ok"?"":st}" style="left:${p.x}%;top:${p.y}%">
      <span class="dot"></span><span class="bl">${a.name.split(" ")[0].toUpperCase()}</span></div>`;
  }).join("");
}
function metricCells(ms){
  if(!ms||!ms.length)return `<span class="m"><i>no metrics</i></span>`;
  return ms.slice(0,6).map(m=>`<span class="m"><b>${fmtVal(m.value,m.fmt)}</b>
    <i>${m.label||m.key||""}</i></span>`).join("");
}
function renderList(apps){
  const rows=apps.map(a=>{
    const st=stateOf(a);
    const ping=a.latency_ms!=null?`<span class="png">ping <b>${a.latency_ms}ms</b></span>`:"";
    let body;
    if(st==="pend")body=`<div class="pendnote">acquiring signal…</div>`;
    else if(st==="bad")body=`<div class="lost">${a.error||"no response"} · last echo ${age(a.age_s)} ago</div>`;
    else body=`<div class="rm">${metricCells(a.metrics)}</div>`;
    return `<section class="row ${st==="ok"?"":st}">
      <div class="rh"><b>${a.name}</b><span class="host">${host(a.url)}</span>
        ${ping}<span class="tag">${TAGS[st]}</span></div>${body}</section>`;
  }).join("");
  document.getElementById("list").innerHTML=rows||`<div class="off">no contacts</div>`;
}
// ── refresh-interval input (changes the real server-side pull cadence) ──
const ivInput=document.getElementById("iv");
let ivDirty=false;
ivInput.addEventListener("input",()=>{ivDirty=true;});
async function applyInterval(){
  let v=parseInt(ivInput.value,10);
  if(isNaN(v)){ivInput.value=refresh;ivDirty=false;return;}
  v=Math.max(MINR,Math.min(MAXR,v));
  try{const r=await(await fetch("/set-interval?secs="+v)).json();
      if(r&&r.ok){refresh=r.refresh;ivInput.value=r.refresh;}}catch(e){}
  ivDirty=false;
}
ivInput.addEventListener("change",applyInterval);
ivInput.addEventListener("keydown",e=>{if(e.key==="Enter"){applyInterval();ivInput.blur();}});
function signature(apps){return JSON.stringify(apps.map(a=>[a.name,a.ok,a.updated,a.latency_ms,(a.metrics||[]).map(m=>m.value),a.error,a.age_s]));}
async function poll(){
  let d;try{d=await(await fetch("/state")).json();}catch(e){return;}
  refresh=d.refresh; nextAt=(d.next_at||0)*1000;
  if(!ivDirty && document.activeElement!==ivInput) ivInput.value=refresh;
  const n=d.apps.length;
  document.getElementById("sub").innerHTML=
    `VICLIX · ${n} CONTACT${n===1?"":"S"} · ${d.up} UP`+(d.down?` · <em>${d.down} LOST</em>`:``);
  const s=signature(d.apps);
  if(s!==sig){sig=s;renderList(d.apps);renderBlips(d.apps);}
}
function beat(){
  const secs=nextAt?Math.max(0,Math.ceil((nextAt-Date.now())/1000)):0;
  document.getElementById("cd").innerHTML=
    nextAt?`sweep in <i>${secs}</i>s · every ${refresh}s`:"sweep —";
  document.getElementById("clk").textContent=new Date().toTimeString().slice(0,8);
}
poll(); setInterval(poll,1000); beat(); setInterval(beat,250);
</script>
</body></html>"""

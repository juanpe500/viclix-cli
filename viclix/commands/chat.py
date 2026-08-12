"""Interactive full-terminal chat for `viclix agents` — a Textual TUI over the
headless coding-agent (Phase A).

Flow (token API only, no server change):
  • pick or start a session
  • type a prompt → POST /api/v1/projects/agent/runs {goal, mode, session_id}
  • stream the run by polling GET .../agent/runs/{run_id}?since=<maxIdx>
  • render each step kind; tool calls/results are CLICKABLE collapsibles
  • Esc cancels the running turn; Ctrl+Q quits

Model selection (/model) is Phase B — for now the run uses the account's default
model (shown in the footer) and /model opens the dashboard provider page.

Textual is imported lazily by `launch()` so the rest of the CLI never depends on
it; a missing install degrades to a friendly message.
"""
import re
import json
import time
import webbrowser
from datetime import datetime

import requests

from ..console import logger
from ..api import require_auth, _tok_url, _dashboard_base, reconcile_project_data

# Statuses at which a run is finished and polling stops.
TERMINAL = {"done", "error", "cancelled", "chat", "needs_input"}
MODES = ["plan", "manual", "auto_edit", "full"]
# Read-only tools whose call+result collapse into ONE card (title = the call with
# chips/query, body = the result once the ← response arrives). Two blocks are
# redundant for these; write/exec tools keep their two-block rendering.
MERGE_TOOLS = {"read_file", "read_files", "search_files", "list_files",
               "list_dir", "grep_files", "glob_files", "grep", "exec_command",
               "db_query", "db_execute", "db_exec", "fetch_url",
               # read-like observers: the result IS the payload → fills the body
               "read_skill_file", "get_logs", "get_build_logs", "get_project_status",
               "recall", "list_agents", "run_script"}
# Log-dump readers: their output legitimately contains "error"/"traceback" as
# CONTENT, not a tool failure — never tint their card red on those keywords.
LOG_TOOLS = {"get_logs", "get_build_logs"}
# Tools rendered like a shell run: "$ <cmd>" then stdout/stderr in the body.
EXEC_TOOLS = {"exec_command", "run_script"}
# Tools whose result is a 'Label: value' dump → prettified body (dim labels).
KV_TOOLS = {"get_project_status"}
# Group B — side-effect tools: one card, the result just confirms (✓ + a short
# note parsed from it) on the title and tints green/red. (Growing set.)
CONFIRM_TOOLS = {"remember"}
# Write tools: also one card, but the body KEEPS the written content (the call
# already shows it); the result only surfaces failures + a ✓/char-count on the
# title, so the redundant "Wrote … (N chars)" block disappears.
WRITE_TOOLS = {"write_file", "apply_patch", "create_file", "edit_file"}
# Control-flow tools with dedicated step-kind rendering (ask card, reply markdown,
# done total). Their tool_call/tool_result rows are redundant → never rendered.
SILENT_TOOLS = {"ask", "request_env_access", "reply", "done"}
MODE_HELP = {
    "plan": "read-only — explores & answers, never edits/deploys",
    "manual": "builds; every edit & exec waits for your approval",
    "auto_edit": "builds; edits auto-apply, execs still gated",
    "full": "builds; auto-approves all but redeploy/run-agent/delete-agent",
}


def _esc(s):
    """Escape Textual markup in dynamic text — agent output, tool results, code
    and titles routinely contain '[' / '[/]' which the markup parser would try to
    interpret as tags (raising MarkupError). Our own literal tags are added around
    already-escaped content, so they still render."""
    return str(s or "").replace("[", r"\[")


def _fmtk(n):
    """Compact token count: 7098 → '7k', 371000 → '371k', 1200000 → '1.2M'."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n / 1000:.0f}k"
    return str(n)


def _extract_patch(raw):
    """The patch/diff text out of an apply_patch call's JSON args."""
    try:
        a = json.loads(raw)
        return a.get("patch") or a.get("diff") or a.get("content") or raw or ""
    except Exception:
        return raw or ""


def _patch_stats(patch):
    """(files, added, removed) for a patch — supports the '*** Update File:' and
    unified-diff formats."""
    files, add, rem = [], 0, 0
    for line in str(patch or "").splitlines():
        m = re.match(r"\*\*\*\s+(?:Update|Add|Delete)\s+File:\s+(.+)", line)
        if m:
            files.append(m.group(1).strip())
            continue
        m2 = re.match(r"\+\+\+\s+[ab]/(.+)", line)
        if m2:
            files.append(m2.group(1).strip())
            continue
        if line.startswith("+") and not line.startswith("+++"):
            add += 1
        elif line.startswith("-") and not line.startswith("---"):
            rem += 1
    seen = []
    for f in files:
        if f not in seen:
            seen.append(f)
    return seen, add, rem


def _render_patch(patch):
    """Markup for a diff: +added green, -removed red, @@ hunks cyan, headers dim,
    context plain. Meant for a Static on a dark background."""
    out = []
    for line in str(patch or "").splitlines():
        e = _esc(line)
        if line.startswith(("+++", "---", "***", "diff ", "index ", "Index:")):
            out.append(f"[dim]{e}[/]")
        elif line.startswith("@@"):
            out.append(f"[b cyan]{e}[/]")
        elif line.startswith("+"):
            out.append(f"[green]{e}[/]")
        elif line.startswith("-"):
            out.append(f"[red]{e}[/]")
        else:
            out.append(e)
    return "\n".join(out)


def _clip_lines(s, n):
    """Keep at most n lines; append a '(+N more lines)' note when clipped."""
    lines = str(s or "").splitlines()
    if len(lines) <= n:
        return str(s or "")
    return "\n".join(lines[:n]) + f"\n… (+{len(lines) - n} more lines)"


def _result_style(content):
    """Classify a tool result → 'ok' | 'err' | None, to tint the card."""
    c = str(content or "")
    m = re.search(r"exit code:\s*(-?\d+)", c, re.IGNORECASE)
    if m:
        return "ok" if m.group(1) == "0" else "err"
    low = c.lower()
    if "✗" in c or "traceback (most recent call last)" in low or "denied by user" in low \
            or low.startswith("error") or "\nerror" in low:
        return "err"
    return None


def _result_preview(res, maxlen=70):
    """A one-line gist of a tool result → dim suffix on the collapsed card title,
    so an observer's answer (e.g. 'No memories found.') shows without expanding."""
    lines = [ln for ln in str(res or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    first = " ".join(lines[0].split())
    if len(first) > maxlen:
        first = first[:maxlen - 1] + "…"
    if len(lines) > 1:
        first += f"  (+{len(lines) - 1} lines)"
    return first


# Status words to tint inside a key:value dump (get_project_status expanded body).
_STATUS_GOOD = {"running", "on", "verified", "healthy", "active", "public"}
_STATUS_BAD = {"stopped", "sleeping", "crashed", "error", "down", "off", "502"}


def _fmt_kv_body(res):
    """Prettify a 'Label: value' status dump: dim the labels so values pop, and
    green/red the known good/bad status words. Untouched lines pass through."""
    out = []
    for line in str(res or "").splitlines():
        m = re.match(r"^(\s*)([A-Z][\w ./()+-]*?):(\s*)(.*)$", line)
        if not m:
            out.append(_esc(line))
            continue
        indent, label, sp, val = m.groups()
        w = val.strip().lower()
        if w in _STATUS_GOOD:
            ev = f"[green]{_esc(val)}[/]"
        elif w in _STATUS_BAD:
            ev = f"[red]{_esc(val)}[/]"
        else:
            ev = _esc(val)
        out.append(f"{indent}[dim]{_esc(label)}:[/]{sp}{ev}")
    return "\n".join(out)


def _fmtn(n):
    """Exact comma-grouped token count (e.g. 5,629) — for per-step/turn detail."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def _tok_tag(step):
    """Dim '· <in> in · <out> out' suffix for a step that reports token usage."""
    i = step.get("input_tokens") or 0
    o = step.get("output_tokens") or 0
    if i or o:
        return f"   [dim]· {_fmtk(i)} in · {_fmtk(o)} out[/]"
    return ""


# Trigger-type badge for maintenance-agent sessions (matches the web AI grid).
_TRIGGER_BADGE = {
    "interval": "⏱ scheduled", "cron": "⏱ cron", "event": "⚡ event",
    "webhook": "🔗 webhook", "manual": "✋ manual",
}


def _ago(iso):
    """Relative age of a timestamp: '5s ago', '3m ago', '2h ago', '4d ago'."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return ""
    secs = (datetime.utcnow() - dt).total_seconds()
    secs = max(0, secs)
    if secs < 60:
        return f"{int(secs)}s ago"
    if secs < 3600:
        return f"{int(secs // 60)}m ago"
    if secs < 86400:
        return f"{int(secs // 3600)}h ago"
    if secs < 2_592_000:
        return f"{int(secs // 86400)}d ago"
    return f"{int(secs // 2_592_000)}mo ago"


def _shortdate(iso):
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(iso)[:16].replace("T", " ")


def cmd_agents_chat(args, cfg):
    """Entry point: launch the interactive chat, or explain if textual is missing."""
    base_url = cfg["base_url"]
    # Upgrade an old-format .viclix (backfills project_id from the api_key, etc.).
    proj = reconcile_project_data(base_url, args)
    token, project_id = require_auth(args, proj or {})
    # project_id may be None when using a project api_key — the token
    # self-identifies the project, so that's fine.
    try:
        app = _build_app(base_url, token, project_id)
    except ModuleNotFoundError:
        logger.error("The interactive chat needs the 'textual' package. Install it with:\n"
                     "    pip install --upgrade textual\n"
                     "or view conversations read-only with:  viclix agents --json")
        return
    app.run()


def _build_app(base_url, token, project_id):
    """Construct the Textual app. Imports live here so importing this module (and
    the CLI as a whole) never requires textual until the chat is actually used."""
    from textual.app import App, ComposeResult
    from textual.screen import Screen, ModalScreen
    from textual.containers import VerticalScroll, Vertical
    from textual.suggester import SuggestFromList
    from textual.widgets import (
        Header, Footer, Input, Static, Label, ListView, ListItem, Collapsible, Markdown,
        Checkbox, Button,
    )

    # Slash-command autocomplete (inline ghost text; → or Tab accepts).
    slash_suggester = SuggestFromList(
        ["/help", "/model", "/usage", "/show", "/new", "/sessions", "/quit",
         "/mode plan", "/mode manual", "/mode auto_edit", "/mode full"],
        case_sensitive=False,
    )

    class PromptInput(Input):
        """Input with shell-style history: ↑ recalls previous messages (keep
        pressing to go further back), ↓ moves forward, restoring the draft at the
        end. History lives on the app (self.app.history)."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._hist_idx = None    # None = not browsing; else index into app.history
            self._draft = ""         # what was typed before browsing started

        def on_key(self, event):
            hist = getattr(self.app, "history", None) or []
            if event.key == "up":
                event.stop()
                event.prevent_default()
                if not hist:
                    return
                if self._hist_idx is None:
                    self._draft = self.value
                    self._hist_idx = len(hist) - 1
                elif self._hist_idx > 0:
                    self._hist_idx -= 1
                self.value = hist[self._hist_idx]
                self.cursor_position = len(self.value)
            elif event.key == "down":
                event.stop()
                event.prevent_default()
                if self._hist_idx is None:
                    return
                self._hist_idx += 1
                if self._hist_idx >= len(hist):
                    self._hist_idx = None
                    self.value = self._draft
                else:
                    self.value = hist[self._hist_idx]
                self.cursor_position = len(self.value)
            elif event.key not in ("left", "right", "home", "end", "enter"):
                # any real edit restarts history navigation
                self._hist_idx = None

    dash = _dashboard_base(base_url)

    # ── small render helpers (return a widget for one agent step) ────────────
    def _pretty_args(content):
        try:
            return json.dumps(json.loads(content), indent=2)
        except Exception:
            return content or ""

    def _write_preview(raw):
        """The actual file content/patch written (real newlines), not the JSON."""
        try:
            a = json.loads(raw)
            txt = a.get("content") or a.get("patch") or a.get("diff") or a.get("text") or ""
        except Exception:
            txt = raw or ""
        return _esc(_clip_lines(txt, 200))

    def _chip(s):
        return f"[on #3a3a3a] {_esc(str(s))} [/]"

    def _basename(p):
        p = str(p)
        return p.rsplit("/", 1)[-1] if "/" in p else p

    def _summarize(kind, tool, content):
        """Markup-safe title for a tool card. Parses common args into chips."""
        c = (content or "").strip().replace("\n", " ")
        if kind == "tool_result":
            return f"[b]← {_esc(tool or 'result')}[/]  {_esc(c[:90])}"
        # tool_call — turn the JSON args into nice chips / text
        try:
            args = json.loads(content)
        except Exception:
            args = None
        head = f"[b]→ {_esc(tool or 'tool')}[/]"
        if tool == "apply_patch":
            files, add, rem = _patch_stats(_extract_patch(content))
            chips = "  ".join(_chip(_basename(f)) for f in files[:4]) or _chip("patch")
            return f"{head}  {chips}   [green]+{add}[/] [red]-{rem}[/]"
        if isinstance(args, dict):
            if isinstance(args.get("paths"), list) and args["paths"]:
                chips = "  ".join(_chip(_basename(p)) for p in args["paths"][:8])
                extra = f" [dim]+{len(args['paths']) - 8}[/]" if len(args["paths"]) > 8 else ""
                return f"{head}  {chips}{extra}"
            if args.get("path"):
                rng = ""
                for a, b in (("start_line", "end_line"), ("start", "end"), ("from_line", "to_line")):
                    if args.get(a) and args.get(b):
                        rng = f" {args[a]}-{args[b]}"
                        break
                return f"{head}  {_chip(_basename(args['path']) + rng)}"
            if args.get("command"):
                cmd = str(args["command"])
                first = cmd.splitlines()[0] if cmd else cmd
                more = "…" if ("\n" in cmd or len(first) > 120) else ""
                return f"{head}  [dim]$[/] {_esc(first[:120])}{more}"
            if args.get("sql"):
                sql = " ".join(str(args["sql"]).split())
                more = "…" if len(sql) > 120 else ""
                return f"{head}  [green]{_esc(sql[:120])}{more}[/]"
            if args.get("query"):
                extra = ""
                if args.get("context") is not None:
                    extra += f"   [dim]ctx {_esc(str(args['context']))}[/]"
                for k in ("path", "dir", "glob", "file_pattern", "include", "exclude"):
                    if args.get(k):
                        extra += f"   [dim]{k}={_esc(str(args[k])[:28])}[/]"
                return f"{head}  [green]\"{_esc(str(args['query'])[:70])}\"[/]{extra}"
            if args.get("url"):
                return f"{head}  [cyan]{_esc(str(args['url'])[:80])}[/]"
            if args.get("name"):
                return f"{head}  {_chip(args['name'])}"
            if args.get("key"):         # remember — the memory key
                return f"{head}  {_chip(str(args['key']))}"
            if args.get("file"):        # run_script — the script + its args
                extra = f" {_esc(str(args['args'])[:40])}" if args.get("args") else ""
                return f"{head}  {_chip(_basename(str(args['file'])))}{extra}"
            if not args:                # no-arg observers (list_agents, status…)
                return head
        if not c or c in ("{}", "null"):
            return head
        return f"{head}  {_esc(c[:80])}"

    def step_widget(step):
        kind = step.get("kind")
        tool = step.get("tool") or step.get("tool_name")
        content = step.get("content") or ""
        if kind == "llm":
            return Static(f"[dim italic]{_esc(content.strip())}[/]{_tok_tag(step)}", classes="think")
        if kind == "reply":
            return Static(_esc(content.strip()), classes="reply")
        if kind == "status":
            return Static(f"[dim]· {_esc(content.strip())}[/]", classes="status")
        if kind == "error":
            return Static(f"[b red]error[/] {_esc(content.strip())}", classes="error")
        if kind in ("tool_call", "tool_result"):
            body = content
            if kind == "tool_call":
                try:
                    body = json.dumps(json.loads(content), indent=2)
                except Exception:
                    body = content
            col = Collapsible(Static(_esc(body)), title=_summarize(kind, tool, content),
                              collapsed=True, classes="tool")
            return col
        if kind == "ask":
            try:
                q = json.loads(content)
                txt = q.get("question", content)
                opts = q.get("options") or []
                extra = ("\n  " + "  ".join(f"\\[{i+1}] {_esc(o)}" for i, o in enumerate(opts))) if opts else ""
                return Static(f"[b yellow]? {_esc(txt)}[/]{extra}\n[dim](type your answer as the next message)[/]",
                              classes="ask")
            except Exception:
                return Static(f"[b yellow]? {_esc(content)}[/]", classes="ask")
        if kind == "approval":
            try:
                a = json.loads(content)
                return Static(f"[b magenta]approval needed[/] for [b]{_esc(a.get('tool'))}[/] "
                              f"[dim](interactive approvals land in a later version — "
                              f"use mode 'full' or approve in the dashboard)[/]", classes="ask")
            except Exception:
                return Static(f"[b magenta]approval needed[/] {_esc(content)}", classes="ask")
        # ui_action / browser_test / restart_needed / approval_result / others
        return Static(f"[dim]· {_esc(kind)}: {_esc(content.strip()[:120])}[/]", classes="status")

    class AskCard(Vertical):
        """Clarifying-question card (mirrors ai.html renderAsk). Only the *pending*
        question — the agent's latest message, still awaiting a reply — is rendered
        interactive (one clickable button per option; a typed answer works too, the
        'Other' path). A question that a later turn has superseded is rendered
        compact & inert: a one-line summary with no buttons, so stale options can't
        be clicked. Answering continues the run with the original goal + this Q&A."""

        def __init__(self, payload, active=True):
            super().__init__(classes="askcard" if active else "askcard askcard-done")
            self.payload = payload if isinstance(payload, dict) else {}
            self.question = str(self.payload.get("question") or "").strip()
            self.active = active
            self.answered = not active   # a historical card is already resolved

        def compose(self):
            if not self.active:
                # superseded → compact, inert one-liner (the answer shows as the
                # next `you` bubble below it, so we don't repeat it here).
                yield Static(f"[dim]?[/] [dim]{_esc(self.question)}[/]", classes="askq")
                return
            yield Static(f"[b yellow]?[/] {_esc(self.question)}", classes="askq")
            env = self.payload.get("env") or {}
            if isinstance(env, dict) and env.get("variables"):
                acc = "read & write" if env.get("access") == "read_write" else "read"
                yield Static(f"[dim]· .env {_esc(', '.join(str(v) for v in env['variables']))}"
                             f"  ({acc})[/]", classes="askmeta")
            for i, o in enumerate(self.payload.get("options") or []):
                if isinstance(o, dict):
                    label = str(o.get("label") or "").strip()
                    desc = str(o.get("description") or "").strip()
                else:
                    label, desc = str(o).strip(), ""
                if not label:
                    continue
                b = Button(f"{i + 1}. {_esc(label)}", classes="askopt")
                b._answer = label   # picked up by ChatScreen.on_button_pressed
                yield b
                if desc:
                    yield Static(f"   [dim]— {_esc(desc)}[/]", classes="askdesc")
            yield Static("[dim]click an option, or type your own answer below ↓[/]",
                         classes="askmeta")

        def resolve(self, answer=None):
            """Close an *active* card once it's no longer the latest message: hide
            its interactive controls and collapse to a compact summary line. Safe to
            call once; historical cards are born resolved."""
            if self.answered:
                return
            self.answered = True
            for w in self.query(".askopt, .askdesc, .askmeta"):
                w.display = False
            summary = f"[dim]?[/] [dim]{_esc(self.question)}[/]"
            if answer:
                summary += f"   [green]→ {_esc(answer)}[/]"
            heads = self.query(".askq")
            if heads:
                heads.first(Static).update(summary)
            self.add_class("askcard-done")

    # ── session picker screen ────────────────────────────────────────────────
    class SessionPicker(Screen):
        BINDINGS = [("ctrl+q", "app.quit", "Quit"), ("r", "refresh", "Refresh")]

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Label("Your AI conversations — pick one to continue, or start a new chat:",
                        classes="hint")
            yield ListView(id="sessions")
            yield Footer()

        def on_mount(self):
            self.load()

        def action_refresh(self):
            self.load()

        def load(self):
            self._load_worker()

        def _load_worker(self):
            def go():
                sessions, agents = [], []
                try:
                    r = requests.get(_tok_url(base_url, "projects/agent/sessions", token, project_id),
                                     timeout=20)
                    if r.status_code == 200:
                        sessions = (r.json() or {}).get("sessions", [])
                except requests.RequestException:
                    pass
                try:
                    ra = requests.get(_tok_url(base_url, "projects/agents", token, project_id), timeout=20)
                    if ra.status_code == 200:
                        agents = (ra.json() or {}).get("agents", [])
                except requests.RequestException:
                    pass
                self.app.call_from_thread(self._fill, sessions, agents)
            self.run_worker(go, thread=True, exclusive=True)

        def _fill(self, sessions, agents):
            lv = self.query_one("#sessions", ListView)
            lv.clear()
            lv.append(ListItem(Label("[b]＋  New chat[/]"), id="new"))

            # Triggered agents (from the fleet) — shown even when OFF / never run.
            for a in agents or []:
                trig = a.get("trigger_type") or "manual"
                state = "[green]active[/]" if a.get("enabled") else "[dim]off[/]"
                badge = f"[magenta]{_TRIGGER_BADGE.get(trig, '🤖 ' + trig)}[/] [dim]·[/] {state}"
                name = (a.get("name") or "(unnamed)").replace("\n", " ").strip()[:60]
                meta = [f"[dim]ran {_ago(a['last_run_at'])}[/]" if a.get("last_run_at")
                        else "[dim]never run[/]"]
                model = a.get("model")
                if model:
                    meta.append(f"[cyan]{_esc(str(model).split('/')[-1])}[/]")
                spent = a.get("total_spent") or 0
                if spent:
                    meta.append(f"[green]${spent:.4f}[/]")
                item = ListItem(Label(f"{badge}  [b]{_esc(name)}[/]\n    {'   '.join(meta)}"))
                item.session_id = None
                item.agent_id = a.get("id")
                lv.append(item)

            # Chat conversations (agent-kind sessions are represented by the agent above).
            for s in sessions:
                if s.get("kind") == "agent":
                    continue
                title = (s.get("title") or "(untitled)").replace("\n", " ").strip()[:66]
                meta = []
                created = _shortdate(s.get("created_at"))
                if created:
                    meta.append(f"[dim]{created}[/]")
                ago = _ago(s.get("updated_at"))
                if ago:
                    meta.append(f"[dim]· last {ago}[/]")
                model = s.get("model")
                if model:
                    meta.append(f"[cyan]{_esc(model.split('/')[-1])}[/]")
                it = s.get("input_tokens") or 0
                ctx = s.get("context_length")
                if it:
                    meta.append(f"[dim]{_fmtk(it)}/{_fmtk(ctx)} tok[/]" if ctx
                                else f"[dim]{_fmtk(it)} in[/]")
                cost = s.get("cost") or 0
                if cost:
                    meta.append(f"[green]${cost:.4f}[/]")
                item = ListItem(Label(f"[dim]💬 chat[/]  [b]{_esc(title)}[/]\n    {'   '.join(meta)}"))
                item.session_id = s.get("id")
                item.agent_id = None
                lv.append(item)
            lv.focus()

        def on_list_view_selected(self, event):
            item = event.item
            if item.id == "new":
                self.app.open_chat(None)
                return
            aid = getattr(item, "agent_id", None)
            if aid:
                self._open_agent(aid)
                return
            self.app.open_chat(getattr(item, "session_id", None))

        def _open_agent(self, agent_id):
            """An agent has no single 'session' — open its latest run's conversation."""
            def go():
                sid = None
                try:
                    r = requests.get(_tok_url(base_url, f"projects/agents/{agent_id}", token, project_id),
                                     timeout=20)
                    if r.status_code == 200:
                        for run in (r.json() or {}).get("recent_runs", []):
                            if run.get("session_id"):
                                sid = run["session_id"]
                                break
                except requests.RequestException:
                    pass
                self.app.call_from_thread(self._agent_opened, sid)
            self.run_worker(go, thread=True)

        def _agent_opened(self, sid):
            if sid:
                self.app.open_chat(sid)
            else:
                self.app.notify("This agent has no runs yet — nothing to open.", severity="warning")

    # ── model picker (modal): favorites list; ←/→ tunes reasoning inline ────────
    _EFFORT_ORDER = ["minimal", "low", "medium", "high", "xhigh"]

    class ModelPicker(ModalScreen):
        BINDINGS = [
            ("escape", "dismiss", "Close"),
            ("left", "effort(-1)", "− reasoning"),
            ("right", "effort(1)", "+ reasoning"),
        ]

        def __init__(self, favorites, current, current_effort):
            super().__init__()
            self.favorites = favorites or []
            self.current = current
            self.current_effort = current_effort
            self.efforts = {}   # mid -> ordered list of effort options (may include 'none')
            self.sel = {}       # mid -> currently selected effort
            for f in self.favorites:
                if not (isinstance(f, dict) and f.get("id")
                        and f.get("supports_reasoning") and f.get("reasoning_efforts")):
                    continue
                mid = f["id"]
                opts = [e for e in _EFFORT_ORDER if e in f["reasoning_efforts"]]
                opts += [e for e in f["reasoning_efforts"] if e not in _EFFORT_ORDER]
                if not f.get("reasoning_mandatory"):
                    opts.append("none")   # allow turning reasoning off
                self.efforts[mid] = opts
                self.sel[mid] = self._init_effort(f, opts)

        def _init_effort(self, f, opts):
            mid = f["id"]
            if mid == self.current and self.current_effort in opts:
                return self.current_effort
            if f.get("reasoning_default") in opts:
                return f["reasoning_default"]
            for pref in ("medium", "high", "low"):
                if pref in opts:
                    return pref
            return opts[0] if opts else None

        def _row_label(self, f):
            mid = f.get("id") or ""
            name = f.get("name") or mid
            meta = []
            ctx = f.get("context_length")
            if ctx:
                try:
                    meta.append(f"[cyan]{int(ctx) // 1000}k[/][dim] ctx[/]")
                except (TypeError, ValueError):
                    pass
            pin, pout = f.get("price_in_per_m"), f.get("price_out_per_m")
            if pin is not None or pout is not None:
                pin_s = f"${pin}" if pin is not None else "$?"
                pout_s = f"${pout}" if pout is not None else "$?"
                meta.append(f"[green]{pin_s}[/][dim]/[/][yellow]{pout_s}[/][dim] per M[/]")
            if mid in self.efforts:
                eff = self.sel.get(mid) or "none"
                if eff == "none":
                    meta.append("[magenta]🧠[/] [dim]‹[/] [b]off[/] [dim]›  ←→[/]")
                else:
                    meta.append(f"[magenta]🧠[/] [dim]‹[/] [b magenta]{_esc(eff)}[/] [dim]›  ←→[/]")
            elif f.get("supports_reasoning"):
                meta.append("[magenta]🧠 reasoning[/]")
            metaline = "   ".join(meta)
            mark = "[green]●[/] " if mid == self.current else "  "
            return (f"{mark}[b]{_esc(name)}[/]\n"
                    f"    [dim]{_esc(mid)}[/]" + (f"    {metaline}" if metaline else ""))

        def compose(self) -> ComposeResult:
            items = []
            for f in self.favorites:
                if isinstance(f, dict):
                    mid = f.get("id") or ""
                    li = ListItem(Label(self._row_label(f)))
                    li.fav = f
                else:
                    mid = str(f)
                    li = ListItem(Label(f"  [b]{_esc(mid)}[/]"))
                    li.fav = None
                li.model_id = mid
                li.is_open = False
                items.append(li)
            openitem = ListItem(Label("🌐  [b]Open model selector in browser…[/]"))
            openitem.model_id = None
            openitem.is_open = True
            openitem.fav = None
            items.append(openitem)
            yield Vertical(
                Label("[b]Choose a model[/]   [dim](↑↓ pick ·[/] "
                      "[b magenta]←→ change 🧠 reasoning[/] [dim]· Enter · Esc)[/]"),
                ListView(*items, id="models"),
                id="picker",
            )

        def on_mount(self):
            self.query_one("#models", ListView).focus()

        def action_effort(self, delta: int):
            lv = self.query_one("#models", ListView)
            item = lv.highlighted_child
            if item is None:
                return
            mid = getattr(item, "model_id", None)
            opts = self.efforts.get(mid)
            if not opts:
                return
            cur = self.sel.get(mid)
            i = opts.index(cur) if cur in opts else 0
            self.sel[mid] = opts[(i + delta) % len(opts)]
            item.query_one(Label).update(self._row_label(item.fav))

        def on_list_view_selected(self, event):
            item = event.item
            if getattr(item, "is_open", False):
                try:
                    webbrowser.open(f"{dash}/settings/ai-providers")
                except Exception:
                    pass
                self.dismiss(None)
                return
            mid = getattr(item, "model_id", None)
            eff = self.sel.get(mid)
            self.dismiss({"model": mid, "effort": (eff if eff and eff != "none" else None)})

    # ── display settings (modal): choose what to show, like the web's gear ──────
    class ShowModal(ModalScreen):
        BINDINGS = [("escape", "dismiss", "Close")]

        def compose(self) -> ComposeResult:
            s = self.app.show
            yield Vertical(
                Label("[b]Display settings[/]   [dim](Space toggles · Esc applies)[/]"),
                Checkbox("Tokens & cost on thoughts / steps", s.get("thought_meta", True),
                         id="cb_thought_meta"),
                Checkbox("Iteration #  on thoughts", s.get("iter", True), id="cb_iter"),
                Checkbox("Per-message Total line", s.get("turn_total", True), id="cb_turn_total"),
                Label("[dim]The session Σ bar (bottom) is always shown.[/]"),
                id="showbox",
            )

        def on_mount(self):
            cbs = self.query(Checkbox)
            if cbs:
                cbs.first().focus()

        def on_checkbox_changed(self, event):
            key = event.checkbox.id.replace("cb_", "")
            self.app.show[key] = bool(event.value)

    # ── usage panel (modal): context window + spend by model + total ────────────
    class UsageModal(ModalScreen):
        BINDINGS = [("escape", "dismiss", "Close"), ("u", "dismiss", "Close")]

        def compose(self) -> ComposeResult:
            a = self.app
            lines = ["[b]CONTEXT WINDOW[/]"]
            cmax = a.ctx_max_by_model.get(a.model)
            if cmax:
                pct = (a.ctx_used / cmax * 100) if cmax else 0
                lines.append(f"  {_fmtk(a.ctx_used)} / {_fmtk(cmax)} tok      [green]{pct:.1f}%[/]")
            else:
                lines.append(f"  {_fmtk(a.ctx_used)} tok in context   "
                             f"[dim](max unknown — favorite the model to see %)[/]")
            lines.append("")
            lines.append("[b]SPENT BY MODEL[/]")
            if not a.usage_by_model:
                lines.append("  [dim]no runs yet this session[/]")
            for model, u in a.usage_by_model.items():
                lines.append(f"  {_esc(model)}   [dim]{u['msgs']} msg[/]   "
                             f"[cyan]{_fmtk(u['in'])} ↑[/] [yellow]{_fmtk(u['out'])} ↓[/]   "
                             f"[green]${u['cost']:.4f}[/]")
            total_msgs = sum(u["msgs"] for u in a.usage_by_model.values())
            lines.append("")
            lines.append(f"[b]Total[/]   [dim]{total_msgs} msg[/]   "
                         f"[cyan]{_fmtk(a.tok_in)} ↑[/] [yellow]{_fmtk(a.tok_out)} ↓[/]   "
                         f"[green]${a.cost:.4f}[/]")
            yield Vertical(
                Static("\n".join(lines)),
                Label("[dim]Esc to close[/]"),
                id="usagebox",
            )

    # ── chat screen ───────────────────────────────────────────────────────────
    class ChatScreen(Screen):
        BINDINGS = [
            ("ctrl+q", "app.quit", "Quit"),
            ("escape", "cancel", "Cancel turn"),
            ("ctrl+n", "sessions", "Sessions"),
        ]

        def __init__(self, session_id):
            super().__init__()
            self.session_id = session_id
            self.run_id = None
            self.busy = False
            self._iter = ""            # latest "Iteration N/M" seen (folded into thinking)
            self._prev_at = None       # previous step timestamp (for "Thought for Xs")
            self._phase = "computing"  # computing → thinking (live indicator)
            self._think_timer = None
            self._think_start = 0.0
            self._cur_model = None     # model of the run currently rendering
            self._pending_tool = {}    # tool_name -> body widget awaiting its result
            self._iter_header = None   # current iteration's thought header (enriched by its llm)
            self._last_goal = ""       # newest run goal — wraps an `ask` answer as a continuation
            self._pending_ask = None   # the live/interactive AskCard (a typed message answers it)
            self._in_load = False      # True while replaying a saved transcript
            self._load_pending_step = None  # during load: the one ask step that stays interactive

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield Static("[dim]▸ your last message will pin here[/]", id="lastmsg")
            yield VerticalScroll(id="log")
            yield Static("", id="thinking")
            yield Static("[dim]esc cancel turn   ·   ^n sessions   ·   ^q quit   ·   /help[/]",
                         id="keyhints")
            yield PromptInput(placeholder="Type a message…  (/ for commands · ↑ history)",
                              id="prompt", suggester=slash_suggester)
            yield Static(self._status_text(), id="status")

        def _set_last(self, text):
            """Pin the user's most recent message at the top so it's always visible."""
            t = " ".join((text or "").split())
            self.query_one("#lastmsg", Static).update(f"[b green]▸ you asked:[/] {_esc(t)}")

        def _status_text(self):
            a = self.app
            cmax = a.ctx_max_by_model.get(a.model)
            if cmax:
                pct = (a.ctx_used / cmax * 100) if cmax else 0
                ctx = f"{_fmtk(a.ctx_used)}/{_fmtk(cmax)} ({pct:.0f}%)"
            else:
                ctx = _fmtk(a.ctx_used)
            reason = f" [magenta]R:{_esc(a.reasoning_effort)}[/]" if a.reasoning_effort else ""
            return (f"[b]{_esc(a.model)}[/] [dim]· {a.mode}[/]{reason}   "
                    f"[dim]ctx[/] {ctx}   "
                    f"Σ [cyan]↑{_fmtk(a.tok_in)}[/] [yellow]↓{_fmtk(a.tok_out)}[/] "
                    f"[green]${a.cost:.4f}[/]   [dim](/usage /model /mode /help)[/]")

        def _refresh_status(self):
            self.query_one("#status", Static).update(self._status_text())

        def on_mount(self):
            self.query_one("#prompt", Input).focus()
            if self.session_id:
                self._load_transcript()

        def _log(self):
            return self.query_one("#log", VerticalScroll)

        def _add(self, widget):
            self._log().mount(widget)
            self._log().scroll_end(animate=False)

        # ── live "computing/thinking" indicator (animated spinner + elapsed) ──
        SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

        def _start_thinking(self):
            self._phase = "computing"
            self._think_start = time.monotonic()
            th = self.query_one("#thinking", Static)
            th.display = True
            if self._think_timer is None:
                self._think_timer = self.set_interval(0.1, self._tick_think)
            else:
                self._think_timer.resume()

        def _tick_think(self):
            elapsed = time.monotonic() - self._think_start
            spin = self.SPINNER[int(elapsed * 10) % len(self.SPINNER)]
            self.query_one("#thinking", Static).update(f"[b]{spin}[/] {self._phase}… {elapsed:.1f}s")

        def _stop_thinking(self):
            if self._think_timer is not None:
                self._think_timer.pause()
            th = self.query_one("#thinking", Static)
            th.update("")
            th.display = False

        # ── step rendering (stateful: folds Iteration, Markdown for llm/reply) ──
        def _step_cost(self, i, o):
            """Per-step USD cost from the current run's model price (cached)."""
            pin, pout, _c = self.app.price_by_model.get(self._cur_model or "", (None, None, None))
            if pin is None and pout is None:
                return None
            return (i * (pin or 0) + o * (pout or 0)) / 1_000_000

        def _thought_header(self, dur=None, i=0, o=0):
            """Markup for a '💭 Thought[for Xs] · in·out·$ · #N/M' line, honoring
            the /show flags. Used for every iteration (tokens only when its llm
            reports them)."""
            parts = [f"[dim]💭 Thought{(' for ' + dur) if dur else ''}[/]"]
            if self.app.show.get("thought_meta") and (i or o):
                seg = f"[cyan]{_fmtn(i)} in[/] [dim]·[/] [yellow]{_fmtn(o)} out[/]"
                c = self._step_cost(i, o)
                if c:
                    seg += f" [dim]·[/] [green]${c:.4f}[/]"
                parts.append(seg)
            if self.app.show.get("iter") and self._iter:
                parts.append(f"[dim]#{self._iter}[/]")
            return "   [dim]·[/]   ".join(parts)

        def _step_duration(self, step):
            """Seconds since the previous step (server timestamps) → 'Thought for Xs'."""
            cur = None
            at = step.get("at")
            if at:
                try:
                    cur = datetime.fromisoformat(at)
                except ValueError:
                    cur = None
            dur = None
            if cur and self._prev_at:
                secs = (cur - self._prev_at).total_seconds()
                if 0 <= secs < 3600:
                    dur = f"{secs:.1f}s"
            if cur:
                self._prev_at = cur
            return dur

        def _add_step(self, step):
            if self.busy:
                self._phase = "thinking"
            kind = step.get("kind")
            content = (step.get("content") or "").strip()
            if kind == "status":
                m = re.match(r"Iteration (\d+)/(\d+)", content)
                if m:
                    # Every iteration gets a thought header — even ones that only
                    # run a tool (no llm prose). The iteration's llm, if any,
                    # enriches THIS header with tokens/cost + prose below.
                    # Seed the timeline at the iteration's start so its llm/tool step
                    # measures pure thinking time ("Thought for Xs") — including the
                    # very first iteration, which otherwise had no prior step to
                    # measure from (matching the web's per-iteration timing).
                    self._step_duration(step)
                    self._iter = f"{m.group(1)}/{m.group(2)}"
                    self._iter_header = Static(self._thought_header(), classes="think")
                    self._add(self._iter_header)
                    return
                if content:
                    self._add(Static(f"[dim]· {_esc(content)}[/]", classes="status"))
                return
            if kind == "llm":
                dur = self._step_duration(step)
                head = self._thought_header(dur, step.get("input_tokens") or 0,
                                            step.get("output_tokens") or 0)
                if self._iter_header is not None:
                    self._iter_header.update(head)   # enrich this iteration's header
                    self._iter_header = None
                else:
                    self._add(Static(head, classes="think"))
                if content:
                    self._add(Markdown(content, classes="mdblock"))
                return
            if kind == "reply":
                self._step_duration(step)
                self._pending_tool.clear()       # a reply ends the tool sequence
                self._iter_header = None
                self._close_pending_ask()        # a reply supersedes any open question
                if content:
                    self._add(Markdown(content, classes="mdblock"))
                return
            if kind == "ask":
                self._step_duration(step)
                self._iter_header = None
                # Only the latest question stays interactive. While replaying a
                # transcript, that's the single pre-computed pending step; live, a
                # fresh ask supersedes whatever came before.
                active = (step is self._load_pending_step) if self._in_load else True
                if active:
                    self._close_pending_ask()
                try:
                    payload = json.loads(content)
                except Exception:
                    payload = {"question": content, "options": []}
                card = AskCard(payload, active=active)
                self._add(card)
                if active:
                    self._pending_ask = card
                return
            tool = step.get("tool") or step.get("tool_name")
            mergeable = tool in MERGE_TOOLS or tool in WRITE_TOOLS or tool in CONFIRM_TOOLS
            dur = self._step_duration(step)   # advance the timeline once per step
            # A tool_call may carry the iteration's LLM usage (when the model
            # emitted a tool directly, with no separate llm step) — enrich this
            # iteration's thought header with those tokens + thinking time.
            if kind == "tool_call" and self._iter_header is not None:
                _i = step.get("input_tokens") or 0
                _o = step.get("output_tokens") or 0
                if _i or _o:
                    self._iter_header.update(self._thought_header(dur, _i, _o))
                    self._iter_header = None
            # Control-flow tools render via their own step kind (ask / reply / done);
            # their raw call+result rows are pure noise next to that card — drop them,
            # but only AFTER the enrichment above: the ask tool_call is what carries
            # the iteration's tokens/timing (there's no separate llm step).
            if tool in SILENT_TOOLS and kind in ("tool_call", "tool_result"):
                return
            # Merge tools → one card. Call makes it (title = chips/query/command,
            # body = args/content). The result then either fills the body
            # (read/exec) or just confirms on the title (write), + tints by result.
            if kind == "tool_call" and mergeable:
                if tool == "apply_patch":
                    body = Static(_render_patch(_extract_patch(content)), classes="patchbody")
                elif tool in WRITE_TOOLS or tool == "remember":
                    body = Static(_write_preview(content))   # the written file / note, not JSON
                else:
                    body = Static(f"[dim]{_esc(_pretty_args(content))}[/]")
                card = Collapsible(body, title=_summarize("tool_call", tool, content),
                                   collapsed=True, classes="tool")
                self._add(card)
                self._pending_tool[tool] = (body, card, content)
                return
            if kind == "tool_result" and mergeable and tool in self._pending_tool:
                body, card, call_content = self._pending_tool.pop(tool)
                style = _result_style(content or "")
                res = (content or "").strip()
                if tool == "remember":
                    # body keeps the note (from the call); confirm scope on the title
                    if style == "err":
                        body.update(_esc(res))
                        card.add_class("tool-err")
                    else:
                        m = re.search(r"in (\w+) memory", res)
                        scope = m.group(1) if m else ""
                        try:
                            card.title += f"   [green]✓{(' ' + scope) if scope else ''}[/]"
                        except Exception:
                            pass
                        card.add_class("tool-ok")
                elif tool in WRITE_TOOLS:
                    # keep the written content in the body; surface only failures
                    if style == "err":
                        body.update(_esc(res))
                        card.add_class("tool-err")
                    elif tool == "apply_patch":
                        card.add_class("tool-edit")
                        files, add, rem = _patch_stats(_extract_patch(call_content))
                        chips = "  ".join(f"{_chip(_basename(f))}[green]✓[/]" for f in files[:4]) \
                            or f"{_chip('patch')}[green]✓[/]"
                        try:
                            card.title = f"[b]→ apply_patch[/]  {chips}   [green]+{add}[/] [red]-{rem}[/]"
                        except Exception:
                            pass
                    else:
                        card.add_class("tool-edit")   # blue = edit, not execution
                        m = re.search(r"\((\d[\d,]*)\s*chars?\)", content or "")
                        try:
                            card.title += f"   [cyan]✓{' ' + m.group(1) + ' chars' if m else ''}[/]"
                        except Exception:
                            pass
                elif tool in EXEC_TOOLS:
                    # show the FULL invocation (up to 20 lines) then its output
                    try:
                        a = json.loads(call_content) or {}
                    except Exception:
                        a = {}
                    if tool == "run_script":
                        cmd = " ".join(str(x) for x in (a.get("file", ""), a.get("args", "")) if x)
                    else:
                        cmd = a.get("command", "")
                    parts = []
                    if cmd:
                        parts.append(f"[dim]$ {_esc(_clip_lines(cmd, 20))}[/]")
                    parts.append(_esc(res) or "[dim](no output)[/]")
                    body.update("\n\n".join(parts))
                    if style:
                        card.add_class(f"tool-{style}")
                elif tool == "fetch_url":
                    # collapsed body = full response (status + headers + body);
                    # the title gets a colored "HTTP 200 OK" badge + green/red tint
                    body.update(_esc(res) or "[dim](no response)[/]")
                    m = re.match(r"\s*HTTP\s+(\d{3})", res)
                    if m:
                        code = int(m.group(1))
                        ok = 200 <= code < 400
                        badge = res.splitlines()[0].strip()   # e.g. "HTTP 200 OK"
                        try:
                            card.title += f"   [{'green' if ok else 'red'}]{_esc(badge)}[/]"
                        except Exception:
                            pass
                        card.add_class("tool-ok" if ok else "tool-err")
                    elif style:
                        card.add_class(f"tool-{style}")
                else:
                    if tool in KV_TOOLS:
                        body.update(_fmt_kv_body(res) or "[dim](no output)[/]")
                    else:
                        body.update(_esc(res) or "[dim](no output)[/]")
                    # A one-line gist on the collapsed title (the observer's answer
                    # without expanding) — skip when the result already IS the title.
                    prev = _result_preview(res)
                    if prev:
                        try:
                            card.title += f"   [dim]{_esc(prev)}[/]"
                        except Exception:
                            pass
                    # log dumps carry "error"/"traceback" as content, not a failure
                    if style and tool not in LOG_TOOLS:
                        card.add_class(f"tool-{style}")
                return
            # write/exec tools, errors, asks, approvals, unmatched results → 2 cards
            self._add(step_widget(step))

        # -- load prior transcript for an existing session --
        def _reload(self):
            """Re-render the current session from the server (used after /show so
            display changes apply to the whole transcript). Resets session usage
            so the Σ totals recompute cleanly."""
            if not self.session_id:
                return
            self._log().remove_children()
            a = self.app
            a.usage_by_model, a.tok_in, a.tok_out, a.cost, a.ctx_used = {}, 0, 0, 0.0, 0
            self._iter, self._prev_at, self._cur_model = "", None, None
            self._refresh_status()
            self._load_transcript()

        def _load_transcript(self):
            def go():
                try:
                    r = requests.get(_tok_url(base_url, f"projects/agent/sessions/{self.session_id}",
                                              token, project_id), timeout=30)
                    data = r.json() if r.status_code == 200 else {}
                except requests.RequestException:
                    data = {}
                # Enrich every prior run with a computed cost (price lookup, cached)
                # so history shows $ per turn, exactly like the web UI.
                for run in data.get("runs", []):
                    pin, pout, _ctx = self._model_info(run.get("model"))
                    try:
                        c = float(run.get("cost") or 0)
                    except (TypeError, ValueError):
                        c = 0.0
                    if not c and (pin is not None or pout is not None):
                        it = run.get("input_tokens") or 0
                        ot = run.get("output_tokens") or 0
                        c = (it * (pin or 0) + ot * (pout or 0)) / 1_000_000
                    run["_computed_cost"] = c
                self.app.call_from_thread(self._render_transcript, data)
            self.run_worker(go, thread=True)

        def _render_transcript(self, data):
            last_goal = ""
            self._iter = ""
            self._prev_at = None
            self._pending_tool.clear()
            self._iter_header = None
            self._pending_ask = None
            # A question is still "open" only if it's the very last step of the very
            # last run — anything earlier was superseded by a later turn. That one
            # step renders interactive; every other ask renders compact & inert.
            runs = data.get("runs", [])
            self._in_load = True
            self._load_pending_step = None
            if runs and runs[-1].get("steps") and \
                    (runs[-1]["steps"][-1] or {}).get("kind") == "ask":
                self._load_pending_step = runs[-1]["steps"][-1]
            for run in runs:
                self._cur_model = run.get("model")
                goal = (run.get("goal") or "").strip()
                if goal:
                    last_goal = goal
                    if not self.app.history or self.app.history[-1] != goal:
                        self.app.history.append(goal)   # seed ↑ history from the loaded thread
                    self._add(Static(f"[b green]▸ you[/]  {_esc(goal)}", classes="you"))
                for st in run.get("steps", []):
                    self._add_step(st)
                if self.app.show.get("turn_total"):
                    it = run.get("input_tokens") or 0
                    ot = run.get("output_tokens") or 0
                    if it or ot:
                        self._add(self._total_line(it, ot, run.get("_computed_cost") or 0,
                                                   run.get("model") or "default"))
                self._acc_usage(run)
            self._in_load = False
            self._load_pending_step = None
            if last_goal:
                self._set_last(last_goal)
                self._last_goal = last_goal
            self._refresh_status()

        # -- input handling --
        def on_input_submitted(self, event):
            text = (event.value or "").strip()
            inp = self.query_one("#prompt", Input)
            inp.value = ""
            inp._hist_idx = None            # reset history browsing on submit
            if not text:
                return
            if not self.app.history or self.app.history[-1] != text:
                self.app.history.append(text)   # shell-style history (skip dupes)
            if text.startswith("/"):
                self._command(text)
                return
            if self.busy:
                self._add(Static("[yellow]still working on the last turn — Esc to cancel[/]",
                                 classes="status"))
                return
            # A typed message while a question is open answers it (the web's "Other").
            if self._pending_ask is not None and not self._pending_ask.answered:
                self._answer_ask(self._pending_ask, text)
                return
            self._add(Static(f"[b green]▸ you[/]  {_esc(text)}", classes="you"))
            self._set_last(text)
            self._send(text)

        def on_button_pressed(self, event):
            """A clicked (or Enter-activated) ask option → answer that card."""
            b = event.button
            if not hasattr(b, "_answer"):
                return
            card = b
            while card is not None and not isinstance(card, AskCard):
                card = card.parent
            if card is not None:
                self._answer_ask(card, b._answer)

        def _close_pending_ask(self, answer=None):
            """Retire the live question (it's no longer the latest message): collapse
            it to a compact, inert summary so its buttons can't be clicked."""
            if self._pending_ask is not None:
                self._pending_ask.resolve(answer)
                self._pending_ask = None

        def _answer_ask(self, card, text):
            """Continue the run carrying the original goal + this clarification,
            in the same session — mirrors ai.html's startRun(lastGoal + Q&A). Only
            the live pending card is answerable; stale ones are inert."""
            if card is None or card.answered or self.busy or card is not self._pending_ask:
                return
            self._add(Static(f"[b green]▸ you[/]  {_esc(text)}", classes="you"))
            self._set_last(text)
            self._close_pending_ask(text)     # collapse the card, show the choice
            if self._last_goal:
                goal = f"{self._last_goal}\n\nClarification — {card.question}\nAnswer: {text}"
            else:
                goal = text
            self._send(goal)

        def _command(self, text):
            parts = text.split()
            cmd = parts[0].lower()
            if cmd == "/help":
                self._add(Static(
                    "[b]commands[/]\n"
                    "  /mode \\[plan|manual|auto_edit|full]   set the run mode\n"
                    "  /model                                pick a favorite model (or open the selector)\n"
                    "  /usage                                token usage: context window + spend by model\n"
                    "  /show                                 choose what to display (tokens, iteration, totals)\n"
                    "  /new                                  start a fresh conversation\n"
                    "  /sessions                             back to the conversation list\n"
                    "  /quit                                 exit\n"
                    "  Esc cancels the current turn.", classes="status"))
            elif cmd == "/mode":
                if len(parts) > 1 and parts[1] in MODES:
                    self.app.mode = parts[1]
                    self._refresh_status()
                    self._add(Static(f"[dim]mode → {self.app.mode} ({MODE_HELP[self.app.mode]})[/]",
                                     classes="status"))
                else:
                    self._add(Static("[dim]usage: /mode plan|manual|auto_edit|full[/]", classes="status"))
            elif cmd == "/model":
                self._open_model_picker()
            elif cmd == "/usage":
                self.app.push_screen(UsageModal())
            elif cmd == "/show":
                self.app.push_screen(ShowModal(), lambda _=None: self._reload())
            elif cmd == "/new":
                self.app.open_chat(None)
            elif cmd == "/sessions":
                self.app.push_screen(SessionPicker())
            elif cmd in ("/quit", "/exit"):
                self.app.exit()
            else:
                self._add(Static(f"[dim]unknown command {_esc(cmd)} — /help[/]", classes="status"))

        def _open_model_picker(self):
            def go():
                favs = []
                try:
                    r = requests.get(_tok_url(base_url, "projects/agent/models/favorites",
                                              token, project_id), timeout=15)
                    if r.status_code == 200:
                        favs = (r.json() or {}).get("favorites", [])
                except requests.RequestException:
                    pass
                for f in favs:
                    if isinstance(f, dict) and f.get("id"):
                        if f.get("context_length"):
                            self.app.ctx_max_by_model[f["id"]] = f["context_length"]
                        self.app.price_by_model[f["id"]] = (
                            f.get("price_in_per_m"), f.get("price_out_per_m"), f.get("context_length"))
                self.app.call_from_thread(self._show_picker, favs)
            self.run_worker(go, thread=True)

        def _show_picker(self, favs):
            def done(result):
                if not result:
                    return
                self.app.model = result.get("model")
                self.app.reasoning_effort = result.get("effort")
                parts = [f"model → {_esc(self.app.model)}"]
                if self.app.reasoning_effort:
                    parts.append(f"🧠 {_esc(self.app.reasoning_effort)}")
                self._add(Static("[dim]" + "   ·   ".join(parts) + "[/]", classes="status"))
                self._refresh_status()
            self.app.push_screen(ModelPicker(favs, self.app.model, self.app.reasoning_effort), done)

        def action_cancel(self):
            if self.busy and self.run_id:
                rid = self.run_id

                def go():
                    try:
                        requests.post(_tok_url(base_url, f"projects/agent/runs/{rid}/cancel",
                                               token, project_id), timeout=15)
                    except requests.RequestException:
                        pass
                self.run_worker(go, thread=True)
                self._add(Static("[yellow]cancelling…[/]", classes="status"))

        def action_sessions(self):
            self.app.push_screen(SessionPicker())

        # -- send a message: create run, then poll-stream it --
        def _send(self, goal):
            self.busy = True
            self._last_goal = goal        # so a later `ask` answer can quote it
            self._iter = ""
            self._prev_at = None
            self._pending_tool.clear()
            self._iter_header = None
            self._start_thinking()

            def go():
                try:
                    body = {"goal": goal, "mode": self.app.mode}
                    if self.session_id:
                        body["session_id"] = self.session_id
                    # Send a picked model (a real provider/model id) — the server
                    # honors it only if it's in the user's favorites.
                    if self.app.model and "/" in self.app.model:
                        body["model"] = self.app.model
                    if self.app.reasoning_effort:
                        body["reasoning_effort"] = self.app.reasoning_effort
                    r = requests.post(_tok_url(base_url, "projects/agent/runs", token, project_id),
                                      json=body, timeout=30)
                    if r.status_code not in (200, 201):
                        self.app.call_from_thread(self._turn_error, f"create failed: {r.text[:200]}")
                        return
                    d = r.json()
                    rid = d.get("run_id")
                    self.app.call_from_thread(self._turn_started, rid, d.get("session_id"), d.get("model"))
                except requests.RequestException as e:
                    self.app.call_from_thread(self._turn_error, f"could not reach Viclix: {e}")
                    return

                since = -1
                while True:
                    try:
                        pr = requests.get(_tok_url(base_url, f"projects/agent/runs/{rid}",
                                                   token, project_id) + f"&since={since}", timeout=20)
                    except requests.RequestException:
                        time.sleep(1.5)
                        continue
                    if pr.status_code != 200:
                        time.sleep(1.5)
                        continue
                    data = pr.json()
                    model = data.get("model")
                    if model and "/" in model:
                        if model not in self.app.price_by_model:
                            self._model_info(model)     # prefetch price for per-thought cost
                        self._cur_model = model
                    for st in data.get("steps", []):
                        since = max(since, st.get("idx", since))
                        self.app.call_from_thread(self._add_step, st)
                    if data.get("status") in TERMINAL:
                        # run.cost is only finalized by the billing path → compute
                        # from the model's price when it's zero/missing.
                        pin, pout, _ctx = self._model_info(data.get("model"))
                        try:
                            cost = float(data.get("cost") or 0)
                        except (TypeError, ValueError):
                            cost = 0.0
                        if not cost and (pin is not None or pout is not None):
                            it = data.get("input_tokens") or 0
                            ot = data.get("output_tokens") or 0
                            cost = (it * (pin or 0) + ot * (pout or 0)) / 1_000_000
                        data["_computed_cost"] = cost
                        self.app.call_from_thread(self._turn_done, data)
                        return
                    time.sleep(1.2)
            self.run_worker(go, thread=True)

        def _turn_started(self, run_id, session_id, model):
            self.run_id = run_id
            if session_id:
                self.session_id = session_id
            if model and "/" in model:   # a real provider/model id (not "user-default")
                self.app.model = model
            self._add(Static("[dim]· working…[/]", classes="status"))
            self._refresh_status()

        def _model_info(self, model):
            """(price_in_per_m, price_out_per_m, context_length) for a model,
            fetched + cached. Safe to call from a worker thread; also seeds
            ctx_max_by_model so the context-window % works for any model."""
            if not model or "/" not in model:
                return (None, None, None)
            cache = self.app.price_by_model
            if model in cache:
                return cache[model]
            info = (None, None, None)
            try:
                url = (_tok_url(base_url, "projects/agent/model-info", token, project_id)
                       + f"&id={requests.utils.quote(model)}")
                r = requests.get(url, timeout=15)
                if r.status_code == 200:
                    d = r.json() or {}
                    info = (d.get("price_in_per_m"), d.get("price_out_per_m"), d.get("context_length"))
                    if d.get("context_length"):
                        self.app.ctx_max_by_model[model] = d["context_length"]
            except requests.RequestException:
                pass
            cache[model] = info
            return info

        def _acc_usage(self, run):
            """Fold one run's tokens/cost into the session totals + per-model table."""
            a = self.app
            model = run.get("model") or "default"
            i = run.get("input_tokens") or 0
            o = run.get("output_tokens") or 0
            c = run.get("_computed_cost")
            if c is None:
                try:
                    c = float(run.get("cost") or 0)
                except (TypeError, ValueError):
                    c = 0.0
                if not c:   # run.cost unfinalized → compute from cached price
                    pin, pout, _ = a.price_by_model.get(model, (None, None, None))
                    if pin is not None or pout is not None:
                        c = (i * (pin or 0) + o * (pout or 0)) / 1_000_000
            u = a.usage_by_model.setdefault(model, {"msgs": 0, "in": 0, "out": 0, "cost": 0.0})
            u["msgs"] += 1
            u["in"] += i
            u["out"] += o
            u["cost"] += c
            a.tok_in += i
            a.tok_out += o
            a.cost += c
            if i:
                a.ctx_used = i           # latest run's input ≈ current context size
            if model != "default":
                a.model = model

        def _total_line(self, it, ot, cost, model):
            costtxt = f" · [green]${cost:.4f}[/]" if cost else ""
            return Static(f"[b]Total[/] [cyan]{_fmtn(it)} in[/] · [yellow]{_fmtn(ot)} out[/]"
                          f"{costtxt}   [dim]{_esc(model)}[/]", classes="total")

        def _turn_done(self, data):
            self.busy = False
            self.run_id = None
            self._stop_thinking()
            self._acc_usage(data)
            it = data.get("input_tokens") or 0
            ot = data.get("output_tokens") or 0
            model = data.get("model") or self.app.model
            cost = data.get("_computed_cost")
            if cost is None:
                try:
                    cost = float(data.get("cost") or 0)
                except (TypeError, ValueError):
                    cost = 0.0
            status = data.get("status")
            if status not in ("done", "chat"):
                tag = {"error": "[red]", "cancelled": "[yellow]"}.get(status, "[dim]")
                self._add(Static(f"{tag}· {_esc(status)}[/]", classes="status"))
            if self.app.show.get("turn_total"):
                self._add(self._total_line(it, ot, cost, model))
            self._refresh_status()
            self.query_one("#prompt", Input).focus()

        def _turn_error(self, msg):
            self.busy = False
            self.run_id = None
            self._stop_thinking()
            self._add(Static(f"[b red]✗[/] {_esc(msg)}", classes="error"))

    # ── the app ────────────────────────────────────────────────────────────────
    class ViclixChatApp(App):
        CSS = """
        Screen { layout: vertical; }
        /* Softer selection highlight so colored prices stay readable on the row. */
        ListView > ListItem.-highlight { background: $primary 20%; }
        ListView:focus > ListItem.-highlight { background: $primary 35%; }
        #lastmsg { dock: top; height: auto; max-height: 4; padding: 0 1; background: $boost; border-bottom: solid $primary; }
        #log { height: 1fr; padding: 0 1; }
        #thinking { height: 1; color: $accent; padding: 0 1; display: none; }
        /* Markdown brings its own trailing block margin; keep the widget itself
           marginless so the single blank below a response comes from that, not
           doubled with ours. */
        Markdown { margin: 0; padding: 0; }
        .mdblock { margin: 0 0 0 2; }
        #prompt { height: 3; }
        #status { height: 1; color: $text-muted; padding: 0 1; background: $boost; }
        .you { margin: 1 0 0 0; }
        .reply { margin: 0 0 0 2; }
        .think { margin: 1 0 0 2; }
        .status { margin: 0 0 0 2; }
        .error { margin: 0 0 0 2; }
        .ask { margin: 1 0 0 2; }
        /* Interactive clarifying-question card (yellow accent, like the web). */
        .askcard { margin: 1 0 1 2; padding: 0 1 1 1; height: auto;
                   background: $warning 8%; border-left: thick $warning; }
        /* Superseded / answered question → compact, muted, inert (no buttons). */
        .askcard-done { padding: 0 1; background: $boost; border-left: thick $success 30%; }
        .askcard-done .askq { margin: 0; }
        .askq { margin: 0 0 1 0; }
        .askmeta { color: $text-muted; }
        .askdesc { margin: 0 0 1 0; }
        .askchosen { margin: 1 0 0 0; }
        .askopt { min-width: 8; width: auto; height: auto; margin: 0 0 1 0; }
        .tool { margin: 0 0 0 2; }
        /* Make the whole title bar clickable (not just the text) + hover feedback,
           so a tool card toggles from anywhere along its row. */
        Collapsible { width: 1fr; }
        Collapsible > CollapsibleTitle { width: 1fr; padding: 0 1; }
        Collapsible > CollapsibleTitle:hover { background: $primary 40%; }
        .tool-ok { background: $success 8%; }
        .tool-edit { background: $primary 12%; }
        .tool-err { background: $error 12%; }
        .patchbody { background: #0a0a0a; padding: 0 1; }
        .total { margin: 0 0 1 0; }
        .hint { padding: 1; color: $text-muted; }
        #sessions > ListItem { height: auto; padding: 0 0 1 0; }
        ModelPicker { align: center middle; }
        #picker { width: 96%; max-width: 120; height: auto; max-height: 90%;
                  background: $panel; border: thick $primary; padding: 1 2; }
        #picker ListView { height: auto; max-height: 30; margin-top: 1; }
        #picker ListView > ListItem { height: auto; padding: 0 0 1 0; }
        UsageModal { align: center middle; }
        #usagebox { width: 74; max-width: 90%; height: auto; background: $panel;
                    border: thick $primary; padding: 1 2; }
        ShowModal { align: center middle; }
        #showbox { width: 56; max-width: 90%; height: auto; background: $panel;
                   border: thick $primary; padding: 1 2; }
        """
        TITLE = "viclix agents"

        def __init__(self):
            super().__init__()
            self.mode = "plan"
            self.model = "account default"
            self.reasoning_effort = None      # set via /model when a reasoning model is picked
            self.cost = 0.0
            self.tok_in = 0
            self.tok_out = 0
            self.ctx_used = 0                 # latest run's input tokens ≈ context size
            self.usage_by_model = {}          # model -> {msgs, in, out, cost}
            self.ctx_max_by_model = {}        # model -> context_length (from favorites)
            self.price_by_model = {}          # model -> (price_in_per_m, price_out_per_m, ctx)
            self.history = []                 # shell-style input history (↑/↓ recall)
            # What to show (configurable via /show, like the web's display gear).
            self.show = {"thought_meta": True, "iter": True, "turn_total": True}

        def on_mount(self):
            self.push_screen(SessionPicker())

        def open_chat(self, session_id):
            # Replace the whole stack with a fresh chat screen.
            self.push_screen(ChatScreen(session_id))

    return ViclixChatApp()

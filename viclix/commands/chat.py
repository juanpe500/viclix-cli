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
import json
import time
import webbrowser

import requests

from ..console import logger
from ..api import require_auth, _tok_url, _dashboard_base, reconcile_project_data

# Statuses at which a run is finished and polling stops.
TERMINAL = {"done", "error", "cancelled", "chat", "needs_input"}
MODES = ["plan", "manual", "auto_edit", "full"]
MODE_HELP = {
    "plan": "read-only — explores & answers, never edits/deploys",
    "manual": "builds; every edit & exec waits for your approval",
    "auto_edit": "builds; edits auto-apply, execs still gated",
    "full": "builds; auto-approves all but redeploy/run-agent/delete-agent",
}


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
    from textual.screen import Screen
    from textual.containers import VerticalScroll
    from textual.widgets import (
        Header, Footer, Input, Static, Label, ListView, ListItem, Collapsible,
    )

    dash = _dashboard_base(base_url)

    # ── small render helpers (return a widget for one agent step) ────────────
    def _summarize(kind, tool, content):
        """One-line title for a collapsible tool card."""
        c = (content or "").strip().replace("\n", " ")
        if kind == "tool_call":
            try:
                args = json.loads(content)
                bits = []
                for k in ("path", "command", "url", "name"):
                    if k in args:
                        bits.append(str(args[k]))
                summary = "  ".join(bits) or c[:80]
            except Exception:
                summary = c[:80]
            return f"→ {tool or 'tool'}  {summary}"
        if kind == "tool_result":
            return f"← {tool or 'result'}  {c[:80]}"
        return c[:100]

    def step_widget(step):
        kind = step.get("kind")
        tool = step.get("tool") or step.get("tool_name")
        content = step.get("content") or ""
        if kind == "llm":
            return Static(f"[dim italic]{content.strip()}[/]", classes="think")
        if kind == "reply":
            return Static(content.strip(), classes="reply")
        if kind == "status":
            return Static(f"[dim]· {content.strip()}[/]", classes="status")
        if kind == "error":
            return Static(f"[b red]error[/] {content.strip()}", classes="error")
        if kind in ("tool_call", "tool_result"):
            body = content
            if kind == "tool_call":
                try:
                    body = json.dumps(json.loads(content), indent=2)
                except Exception:
                    body = content
            col = Collapsible(Static(body), title=_summarize(kind, tool, content),
                              collapsed=True, classes="tool")
            return col
        if kind == "ask":
            try:
                q = json.loads(content)
                txt = q.get("question", content)
                opts = q.get("options") or []
                extra = ("\n  " + "  ".join(f"[{i+1}] {o}" for i, o in enumerate(opts))) if opts else ""
                return Static(f"[b yellow]? {txt}[/]{extra}\n[dim](type your answer as the next message)[/]",
                              classes="ask")
            except Exception:
                return Static(f"[b yellow]? {content}[/]", classes="ask")
        if kind == "approval":
            try:
                a = json.loads(content)
                return Static(f"[b magenta]approval needed[/] for [b]{a.get('tool')}[/] "
                              f"[dim](interactive approvals land in a later version — "
                              f"use mode 'full' or approve in the dashboard)[/]", classes="ask")
            except Exception:
                return Static(f"[b magenta]approval needed[/] {content}", classes="ask")
        # ui_action / browser_test / restart_needed / approval_result / others
        return Static(f"[dim]· {kind}: {content.strip()[:120]}[/]", classes="status")

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
                try:
                    r = requests.get(_tok_url(base_url, "projects/agent/sessions", token, project_id),
                                     timeout=20)
                    sessions = (r.json() or {}).get("sessions", []) if r.status_code == 200 else []
                except requests.RequestException:
                    sessions = []
                self.app.call_from_thread(self._fill, sessions)
            self.run_worker(go, thread=True, exclusive=True)

        def _fill(self, sessions):
            lv = self.query_one("#sessions", ListView)
            lv.clear()
            lv.append(ListItem(Label("＋  New chat"), id="new"))
            for s in sessions:
                when = (s.get("updated_at") or "")[:16].replace("T", " ")
                title = (s.get("title") or "(untitled)").replace("\n", " ")[:70]
                item = ListItem(Label(f"{title}\n[dim]{when}[/]"))
                item.session_id = s.get("id")
                lv.append(item)
            lv.focus()

        def on_list_view_selected(self, event):
            item = event.item
            sid = None if item.id == "new" else getattr(item, "session_id", None)
            self.app.open_chat(sid)

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

        def compose(self) -> ComposeResult:
            yield Header(show_clock=False)
            yield VerticalScroll(id="log")
            yield Input(placeholder="Type a message…  (/help for commands)", id="prompt")
            yield Static(self._status_text(), id="status")
            yield Footer()

        def _status_text(self):
            m = self.app.mode
            return (f"[b]model:[/] {self.app.model}   [b]mode:[/] {m} "
                    f"[dim]({MODE_HELP[m]})[/]   [b]cost:[/] ${self.app.cost:.4f}")

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

        # -- load prior transcript for an existing session --
        def _load_transcript(self):
            def go():
                try:
                    r = requests.get(_tok_url(base_url, f"projects/agent/sessions/{self.session_id}",
                                              token, project_id), timeout=30)
                    data = r.json() if r.status_code == 200 else {}
                except requests.RequestException:
                    data = {}
                self.app.call_from_thread(self._render_transcript, data)
            self.run_worker(go, thread=True)

        def _render_transcript(self, data):
            for run in data.get("runs", []):
                goal = (run.get("goal") or "").strip()
                if goal:
                    self._add(Static(f"[b green]▸ you[/]  {goal}", classes="you"))
                for st in run.get("steps", []):
                    self._add(step_widget(st))
            self._refresh_status()

        # -- input handling --
        def on_input_submitted(self, event):
            text = (event.value or "").strip()
            self.query_one("#prompt", Input).value = ""
            if not text:
                return
            if text.startswith("/"):
                self._command(text)
                return
            if self.busy:
                self._add(Static("[yellow]still working on the last turn — Esc to cancel[/]",
                                 classes="status"))
                return
            self._add(Static(f"[b green]▸ you[/]  {text}", classes="you"))
            self._send(text)

        def _command(self, text):
            parts = text.split()
            cmd = parts[0].lower()
            if cmd == "/help":
                self._add(Static(
                    "[b]commands[/]\n"
                    "  /mode [plan|manual|auto_edit|full]   set the run mode\n"
                    "  /model                                choose model (opens dashboard for now)\n"
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
                url = f"{dash}/ai-providers"
                try:
                    webbrowser.open(url)
                except Exception:
                    pass
                self._add(Static(f"[dim]model favorites live in the dashboard for now: {url}\n"
                                 f"(a native /model picker is coming next)[/]", classes="status"))
            elif cmd == "/new":
                self.app.open_chat(None)
            elif cmd == "/sessions":
                self.app.push_screen(SessionPicker())
            elif cmd in ("/quit", "/exit"):
                self.app.exit()
            else:
                self._add(Static(f"[dim]unknown command {cmd} — /help[/]", classes="status"))

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

            def go():
                try:
                    body = {"goal": goal, "mode": self.app.mode}
                    if self.session_id:
                        body["session_id"] = self.session_id
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
                    for st in data.get("steps", []):
                        since = max(since, st.get("idx", since))
                        self.app.call_from_thread(self._add, step_widget(st))
                    if data.get("status") in TERMINAL:
                        self.app.call_from_thread(self._turn_done, data)
                        return
                    time.sleep(1.2)
            self.run_worker(go, thread=True)

        def _turn_started(self, run_id, session_id, model):
            self.run_id = run_id
            if session_id:
                self.session_id = session_id
            if model and model != "user-default (locked)":
                self.app.model = model
            self._add(Static("[dim]· working…[/]", classes="status"))
            self._refresh_status()

        def _turn_done(self, data):
            self.busy = False
            self.run_id = None
            it, ot = data.get("input_tokens") or 0, data.get("output_tokens") or 0
            cost = data.get("cost")
            if cost:
                try:
                    self.app.cost += float(cost)
                except (TypeError, ValueError):
                    pass
            status = data.get("status")
            tag = {"error": "[red]", "cancelled": "[yellow]"}.get(status, "[green]")
            self._add(Static(f"{tag}✓ {status}[/]  [dim]{it}+{ot} tok[/]", classes="status"))
            self._refresh_status()
            self.query_one("#prompt", Input).focus()

        def _turn_error(self, msg):
            self.busy = False
            self.run_id = None
            self._add(Static(f"[b red]✗[/] {msg}", classes="error"))

    # ── the app ────────────────────────────────────────────────────────────────
    class ViclixChatApp(App):
        CSS = """
        Screen { layout: vertical; }
        #log { height: 1fr; padding: 0 1; }
        #prompt { dock: bottom; }
        #status { dock: bottom; height: 1; color: $text-muted; padding: 0 1; }
        .you { margin: 1 0 0 0; }
        .reply { margin: 0 0 0 2; }
        .think { margin: 0 0 0 2; }
        .status { margin: 0 0 0 2; }
        .error { margin: 0 0 0 2; }
        .ask { margin: 1 0 0 2; }
        .tool { margin: 0 0 0 2; }
        .hint { padding: 1; color: $text-muted; }
        """
        TITLE = "viclix agents"

        def __init__(self):
            super().__init__()
            self.mode = "plan"
            self.model = "account default"
            self.cost = 0.0

        def on_mount(self):
            self.push_screen(SessionPicker())

        def open_chat(self, session_id):
            # Replace the whole stack with a fresh chat screen.
            self.push_screen(ChatScreen(session_id))

    return ViclixChatApp()

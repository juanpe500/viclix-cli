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


def _esc(s):
    """Escape Textual markup in dynamic text — agent output, tool results, code
    and titles routinely contain '[' / '[/]' which the markup parser would try to
    interpret as tags (raising MarkupError). Our own literal tags are added around
    already-escaped content, so they still render."""
    return str(s or "").replace("[", r"\[")


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
    from textual.widgets import (
        Header, Footer, Input, Static, Label, ListView, ListItem, Collapsible,
    )

    dash = _dashboard_base(base_url)

    # ── small render helpers (return a widget for one agent step) ────────────
    def _summarize(kind, tool, content):
        """One-line, markup-safe title for a collapsible tool card."""
        c = (content or "").strip().replace("\n", " ")
        if kind == "tool_call":
            try:
                targs = json.loads(content)
                bits = []
                for k in ("path", "command", "url", "name"):
                    if k in targs:
                        bits.append(str(targs[k]))
                summary = "  ".join(bits) or c[:80]
            except Exception:
                summary = c[:80]
            return _esc(f"→ {tool or 'tool'}  {summary}")
        if kind == "tool_result":
            return _esc(f"← {tool or 'result'}  {c[:80]}")
        return _esc(c[:100])

    def step_widget(step):
        kind = step.get("kind")
        tool = step.get("tool") or step.get("tool_name")
        content = step.get("content") or ""
        if kind == "llm":
            return Static(f"[dim italic]{_esc(content.strip())}[/]", classes="think")
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
                item = ListItem(Label(f"{_esc(title)}\n[dim]{when}[/]"))
                item.session_id = s.get("id")
                lv.append(item)
            lv.focus()

        def on_list_view_selected(self, event):
            item = event.item
            sid = None if item.id == "new" else getattr(item, "session_id", None)
            self.app.open_chat(sid)

    # ── model picker (modal): favorites list + "open selector" as the last item ─
    class ModelPicker(ModalScreen):
        BINDINGS = [("escape", "dismiss", "Close")]

        def __init__(self, favorites, current):
            super().__init__()
            self.favorites = favorites or []
            self.current = current

        def compose(self) -> ComposeResult:
            items = []
            for f in self.favorites:
                if isinstance(f, dict):
                    mid = f.get("id") or ""
                    name = f.get("name") or mid
                    meta = []
                    ctx = f.get("context_length")
                    if ctx:
                        try:
                            meta.append(f"{int(ctx) // 1000}k ctx")
                        except (TypeError, ValueError):
                            pass
                    pin, pout = f.get("price_in_per_m"), f.get("price_out_per_m")
                    if pin is not None or pout is not None:
                        meta.append(f"${pin if pin is not None else '?'}/${pout if pout is not None else '?'} per M")
                    tail = ("   [dim]" + _esc(" · ".join(meta)) + "[/]") if meta else ""
                    label = f"{_esc(name)}   [dim]{_esc(mid)}[/]{tail}"
                else:
                    mid = str(f)
                    label = _esc(mid)
                mark = "● " if mid == self.current else "  "
                li = ListItem(Label(f"{mark}{label}"))
                li.model_id = mid
                li.is_open = False
                items.append(li)
            openitem = ListItem(Label("🌐  Open model selector in browser…"))
            openitem.model_id = None
            openitem.is_open = True
            items.append(openitem)
            yield Vertical(
                Label("[b]Choose a model[/]   [dim](↑↓ Enter · Esc to close)[/]"),
                ListView(*items, id="models"),
                id="picker",
            )

        def on_mount(self):
            self.query_one("#models", ListView).focus()

        def on_list_view_selected(self, event):
            item = event.item
            if getattr(item, "is_open", False):
                try:
                    webbrowser.open(f"{dash}/settings/ai-providers")
                except Exception:
                    pass
                self.dismiss(None)
            else:
                self.dismiss(getattr(item, "model_id", None))

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
            yield Static("[dim]▸ your last message will pin here[/]", id="lastmsg")
            yield VerticalScroll(id="log")
            yield Input(placeholder="Type a message…  (/help for commands)", id="prompt")
            yield Static(self._status_text(), id="status")
            yield Footer()

        def _set_last(self, text):
            """Pin the user's most recent message at the top so it's always visible."""
            t = " ".join((text or "").split())
            self.query_one("#lastmsg", Static).update(f"[b green]▸ you asked:[/] {_esc(t)}")

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
            last_goal = ""
            for run in data.get("runs", []):
                goal = (run.get("goal") or "").strip()
                if goal:
                    last_goal = goal
                    self._add(Static(f"[b green]▸ you[/]  {_esc(goal)}", classes="you"))
                for st in run.get("steps", []):
                    self._add(step_widget(st))
            if last_goal:
                self._set_last(last_goal)
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
            self._add(Static(f"[b green]▸ you[/]  {_esc(text)}", classes="you"))
            self._set_last(text)
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
                self._open_model_picker()
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
                self.app.call_from_thread(self._show_picker, favs)
            self.run_worker(go, thread=True)

        def _show_picker(self, favs):
            def done(model):
                if model:
                    self.app.model = model
                    self._refresh_status()
                    self._add(Static(f"[dim]model → {_esc(model)}[/]", classes="status"))
            self.app.push_screen(ModelPicker(favs, self.app.model), done)

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
                    # Send a picked model (a real provider/model id) — the server
                    # honors it only if it's in the user's favorites.
                    if self.app.model and "/" in self.app.model:
                        body["model"] = self.app.model
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
            if model and "/" in model:   # a real provider/model id (not "user-default")
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
            self._add(Static(f"[b red]✗[/] {_esc(msg)}", classes="error"))

    # ── the app ────────────────────────────────────────────────────────────────
    class ViclixChatApp(App):
        CSS = """
        Screen { layout: vertical; }
        #lastmsg { dock: top; height: auto; max-height: 4; padding: 0 1; background: $boost; border-bottom: solid $primary; }
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
        ModelPicker { align: center middle; }
        #picker { width: 84; max-width: 90%; height: auto; max-height: 80%;
                  background: $panel; border: thick $primary; padding: 1 2; }
        #picker ListView { height: auto; max-height: 22; margin-top: 1; }
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

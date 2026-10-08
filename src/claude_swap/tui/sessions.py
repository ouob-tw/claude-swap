"""Read-only session/cache monitor. It never starts an auto-switch engine."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Button, DataTable, Footer, Static

from claude_swap.idle import observe_idle, tracked_config_dirs
from claude_swap.settings import load_settings
from claude_swap.tui.data import format_duration
from claude_swap.tui.modals import OutputModal

REASONS = {
    "all-sessions-idle-and-cold": "All sessions idle; caches expired (estimated)",
    "session-state-unknown": "Session data unreadable or incomplete",
    "session-busy-or-unknown": "A session is busy, waiting, or unknown",
    "session-cache-unknown": "A live session has no known cache deadline",
    "session-cache-warm": "A session or subagent cache is still warm",
    "session-transcript-unknown": "A live session transcript is unavailable",
    "subagent-busy-or-unknown": "A subagent is still working or cannot be verified",
    "ide-session-unknown": "An IDE is running without readable session data",
}


def service_status() -> str:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "show", "cswap-auto.service", "-p", "ActiveState", "-p", "SubState"],
            capture_output=True, text=True, timeout=2, check=True,
        )
        fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        return f"{fields.get('ActiveState', 'unknown')} / {fields.get('SubState', 'unknown')}"
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def collect_status(root: Path, now: float) -> dict:
    configs = tracked_config_dirs(root)
    observation = observe_idle(configs, root, now)
    errors = observation.unreadable
    try:
        settings = load_settings(root, strict=True)
    except (OSError, ValueError, TypeError):
        settings = None
        errors += 1
    records = {r["session_id"]: r for r in observation.records}
    live = {s.session_id: s for s in observation.sessions}
    rows = []
    for sid in sorted(set(records) | set(live), key=lambda sid: (sid not in live, sid)):
        session = live.get(sid)
        record = records.get(sid)
        status = (session.status or "unknown") if session else ("unknown" if observation.unreadable else "closed")
        deadline = None
        cache = "unknown"
        project = Path(session.cwd).name if session and session.cwd else "—"
        if record:
            try:
                transcript = Path(record["transcript_path"])
                deadline = observation.deadlines.get(transcript)
                if deadline is None or (session and record.get("known") is not True):
                    deadline = None
                else:
                    cache = "warm" if deadline > now else "expired (est.)"
                if project == "—":
                    project = Path(record["cwd"]).name if isinstance(record.get("cwd"), str) else "unknown"
            except (OSError, TypeError, KeyError, ValueError):
                errors += 1
        clock = datetime.fromtimestamp(deadline).strftime("%m-%d %H:%M:%S") if deadline else "—"
        remaining = format_duration(deadline - now) if deadline and deadline > now else ("0s" if deadline is not None else "unknown")
        rows.append((sid[:12], project, status, cache, clock, remaining))
    try:
        state = json.loads((root / "autoswitch_state.json").read_text())
        switched_at = state.get("lastSwitchAt")
        last = (f"#{state.get('lastSwitchFrom', '?')} → #{state.get('lastSwitchTo', '?')} · "
                f"{datetime.fromtimestamp(switched_at).strftime('%m-%d %H:%M:%S')} · {state.get('leftTrigger', 'unknown')}") if switched_at else "none recorded"
    except (OSError, ValueError, TypeError, AttributeError):
        last = "unavailable"
    return {"service": service_status(), "enabled": settings.idle_rebalance if settings else None,
            "ceiling": settings.target_five_hour_max_pct if settings else None, "ready": observation.ready,
            "reason": REASONS.get(observation.reason, observation.reason), "rows": rows, "errors": errors, "last": last}


class SessionsScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("h", "help", "Help"),
                Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        with Horizontal(id="sessions-header"):
            yield Static("Session / cache status", id="sessions-title")
            yield Button("Help", id="sessions-help", tooltip="Settings, last switch and cache explanations (h)")
        yield Static("Loading…", id="sessions-summary", markup=False)
        yield DataTable(id="sessions-table", zebra_stripes=True, cursor_type="row")
        yield Footer()

    def on_mount(self) -> None:
        self._refreshing = False
        self._data = None
        self._details = "Status unavailable; waiting for a successful refresh."
        self._previous_store_only = self.app._store_only
        self.app.set_store_only(True)
        self.query_one(DataTable).tooltip = "Cache expiry is estimated. Select a row and press Enter for details."
        self.set_interval(5, self._refresh_status)
        self._refresh_status()

    def on_unmount(self) -> None:
        self.app.set_store_only(self._previous_store_only)

    def _refresh_status(self) -> None:
        if self._refreshing:
            return
        self._refreshing = True
        self.run_worker(self._collect, thread=True, exit_on_error=False, group="sessions-status")

    def _collect(self) -> None:
        try:
            data = collect_status(self.app.switcher.backup_dir, time.time())
            self.app.call_from_thread(self._apply, data)
        except Exception as exc:
            self.app.call_from_thread(self._failed, type(exc).__name__)

    def _apply(self, data: dict) -> None:
        self._refreshing = False
        if not self.is_mounted:
            return
        snap = self.app.snapshot
        current = f"#{snap.active_number}" if snap and snap.active_number else "none"
        if snap is None:
            current = "unknown"
        elif self.app._last_refresh_error or time.time() - snap.taken_at >= self.app.SNAPSHOT_AGE_NOTE_S:
            current = f"unavailable (last known #{snap.active_number})"
        enabled = "enabled" if data["enabled"] is True else ("disabled" if data["enabled"] is False else "unknown")
        ceiling = f"{data['ceiling']:g}%" if data["ceiling"] is not None else "unknown"
        summary = (f"Account: {current} · Service: {data['service']}\n"
                   f"Session check {'clear' if data['ready'] else 'blocked'}: {data['reason']}")
        if data['errors']:
            summary += f"\nUnreadable records: {data['errors']}"
        self._details = (f"{summary}\nIdle preparation: {enabled} · 5h target ceiling: {ceiling}\n"
                         f"Last switch: {data['last']}\n"
                         f"Sessions: {len(data['rows'])} · unreadable records: {data['errors']}\n\n"
                         "Read-only · auto-refresh 5s\nCache deadlines are estimates, not confirmed server expiry.\n"
                         "Unknown data blocks idle preparation. A clear session check does not confirm quota eligibility.\n"
                         "On narrow screens, Session IDs are shown only in row details. Select a row and press Enter.")
        self.query_one("#sessions-summary", Static).update(summary)
        self.query_one("#sessions-summary", Static).tooltip = self._details
        self._data = data
        self._render_table()

    def on_resize(self) -> None:
        if getattr(self, "_data", None) is not None:
            self._render_table()

    def _render_table(self) -> None:
        table = self.query_one(DataTable)
        cursor, scroll_y = table.cursor_row, table.scroll_y
        show_id = self.size.width >= 80
        widths = [("Session", 12)] if show_id else []
        project_width = max(8, min(28, self.size.width - (49 if show_id else 35)))
        widths += [("Project", project_width), ("State", 7), ("Cache", 7), ("Remaining", 9)]
        table.clear(columns=True)
        for label, width in widths:
            table.add_column(label, width=width)
        for sid, project, status, cache, expiry, remaining in self._data["rows"]:
            cells = ([sid] if show_id else []) + [project, status, "expired" if cache == "expired (est.)" else cache, remaining]
            table.add_row(*(Text(cell, overflow="ellipsis", no_wrap=True) for cell in cells))
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1), animate=False, scroll=False)
            table.scroll_to(y=scroll_y, animate=False)

    def action_help(self) -> None:
        self.app.push_screen(OutputModal("Session / cache help", self._details))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "sessions-help":
            self.action_help()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if self._data is None:
            return
        sid, project, status, cache, expiry, remaining = self._data["rows"][event.cursor_row]
        self.app.push_screen(OutputModal("Session / cache details", (
            f"Session: {sid}\nProject: {project}\nState: {status}\nCache: {cache}\n"
            f"Expiry (local, estimated): {expiry}\nRemaining: {remaining}")))

    def _failed(self, error: str) -> None:
        self._refreshing = False
        if not self.is_mounted:
            return
        self._data = None
        self._details = f"Status unavailable ({error}); retrying. Idle preparation is not confirmed."
        self.query_one("#sessions-summary", Static).update(self._details)
        self.query_one("#sessions-summary", Static).tooltip = self._details
        self.query_one(DataTable).clear()

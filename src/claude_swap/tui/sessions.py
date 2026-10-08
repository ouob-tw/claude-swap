"""Read-only session/cache monitor. It never starts an auto-switch engine."""
from __future__ import annotations

import json
import subprocess
import time
from datetime import datetime
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static

from claude_swap.idle import idle_ready, tracked_config_dirs, transcript_deadline
from claude_swap.process_detection import scan_sessions
from claude_swap.settings import load_settings
from claude_swap.tui.data import format_duration

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
    ready, reason = idle_ready(configs, root, now)
    settings = load_settings(root)
    records = {}
    errors = 0
    for path in (root / "prompt-cache").glob("*.json"):
        try:
            record = json.loads(path.read_text())
            records[record["session_id"]] = record
        except (OSError, ValueError, TypeError, KeyError):
            errors += 1
    live = {}
    for config in configs:
        sessions, unreadable = scan_sessions(config)
        errors += unreadable
        live.update((s.session_id, s) for s in sessions)
    rows = []
    for sid in sorted(set(records) | set(live), key=lambda sid: (sid not in live, sid)):
        session = live.get(sid)
        record = records.get(sid)
        status = (session.status or "unknown") if session else "closed"
        deadline = None
        cache = "unknown"
        project = Path(session.cwd).name if session and session.cwd else "—"
        if record:
            try:
                transcript = Path(record["transcript_path"])
                deadline = transcript_deadline(transcript, record)
                if session and record.get("known") is not True:
                    deadline = None
                else:
                    cache = "warm" if deadline > now else "expired (est.)"
                if project == "—":
                    project = transcript.parent.name
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
    return {"service": service_status(), "enabled": settings.idle_rebalance,
            "ceiling": settings.target_five_hour_max_pct, "ready": ready,
            "reason": REASONS.get(reason, reason), "rows": rows, "errors": errors, "last": last}


class SessionsScreen(Screen):
    BINDINGS = [Binding("escape", "app.pop_screen", "Back"), Binding("q", "app.quit", "Quit")]

    def compose(self) -> ComposeResult:
        yield Static("Session / cache status", id="sessions-title")
        yield Static("Loading…", id="sessions-summary", markup=False)
        yield DataTable(id="sessions-table", zebra_stripes=True, cursor_type="row")
        yield Static("Read-only · auto-refresh 5s · deadlines are estimates; unknown data blocks idle preparation", id="sessions-note", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        self._refreshing = False
        self._previous_store_only = self.app._store_only
        self.app.set_store_only(True)
        self.query_one(DataTable).add_columns("Session", "Project", "State", "Cache", "Expiry (local)", "Remaining")
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
        current = self.app.snapshot.active_number if self.app.snapshot else "unknown"
        summary = (f"Service: {data['service']} · Current account: #{current}\n"
                   f"Idle preparation: {'enabled' if data['enabled'] else 'disabled'} · 5h target ceiling: {data['ceiling']:g}%\n"
                   f"Session check {'clear' if data['ready'] else 'blocked'}: {data['reason']}\n"
                   f"Last switch: {data['last']}\n"
                   f"Sessions: {len(data['rows'])} · unreadable records: {data['errors']}")
        self.query_one("#sessions-summary", Static).update(summary)
        table = self.query_one(DataTable)
        cursor, scroll_y = table.cursor_row, table.scroll_y
        table.clear()
        table.add_rows(data["rows"])
        if table.row_count:
            table.move_cursor(row=min(cursor, table.row_count - 1), animate=False, scroll=False)
            table.scroll_to(y=scroll_y, animate=False)

    def _failed(self, error: str) -> None:
        self._refreshing = False
        if not self.is_mounted:
            return
        self.query_one("#sessions-summary", Static).update(f"Status unavailable ({error}); retrying. Idle preparation is not confirmed.")

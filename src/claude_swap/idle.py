"""Observe prompt-cache deadlines without storing messages or credentials.

Main-session deadlines come from Claude Code's status line. Unobserved or
changed transcripts use a conservative one-hour quiet period. Live sessions
without a cache observation, or with unreadable state, never qualify.
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
import time
from pathlib import Path
from dataclasses import dataclass, field

from claude_swap.paths import get_backup_root, get_claude_config_home, get_default_claude_config_home
from claude_swap.locking import FileLock
from claude_swap.exceptions import LockError
from claude_swap.process_detection import ClaudeSession, scan_sessions, is_pid_alive
from claude_swap.settings import atomic_write_json

QUIET_FALLBACK_S = 3600


def tracked_config_dirs(root: Path) -> list[Path]:
    profiles = [get_claude_config_home(), get_default_claude_config_home()]
    profiles.extend(p for p in (root / "sessions").glob("*") if p.is_dir())
    return list(dict.fromkeys(profiles))


def transcript_deadline(transcript: Path, record: dict | None) -> float:
    mtime = transcript.stat().st_mtime
    if record and record.get("known") is True and mtime <= record["transcript_mtime"]:
        expiry = record["expires_at"]
        return 0 if expiry is None else expiry
    return mtime + QUIET_FALLBACK_S


def record_status(data: dict, root: Path, now: float) -> None:
    sid = data.get("session_id")
    transcript = data.get("transcript_path")
    if not isinstance(sid, str) or not sid or not isinstance(transcript, str):
        return
    cache = data.get("prompt_cache")
    known = isinstance(cache, dict) and "expires_at" in cache
    expiry = cache.get("expires_at") if known else None
    if expiry is not None and (
        isinstance(expiry, bool) or not isinstance(expiry, (int, float))
        or not math.isfinite(expiry) or expiry > now + QUIET_FALLBACK_S + 60
    ):
        known = False
        expiry = None
    path = root / "prompt-cache" / (hashlib.sha256(sid.encode()).hexdigest() + ".json")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with FileLock(path.with_suffix(".lock"), timeout=1):
        try:
            previous = json.loads(path.read_text())
            old_expiry = previous.get("expires_at")
            if isinstance(old_expiry, (int, float)) and old_expiry > now:
                # A compaction/no-cache response does not evict the previous prefix.
                expiry = max(expiry or 0, old_expiry)
        except (OSError, ValueError, AttributeError):
            if known and expiry is None and cache.get("caching_observed"):
                expiry = now + QUIET_FALLBACK_S
        atomic_write_json(path, {
            "session_id": sid, "transcript_path": transcript,
            "known": known, "expires_at": expiry, "observed_at": now,
            "transcript_mtime": Path(transcript).stat().st_mtime,
            "cwd": data.get("workspace", {}).get("current_dir") or data.get("cwd"),
        })


def _subagent_finished(path: Path) -> bool:
    with path.open("rb") as stream:
        stream.seek(max(0, path.stat().st_size - 262144))
        lines = stream.read().splitlines()
    for line in reversed(lines):
        try:
            row = json.loads(line)
        except ValueError:
            return False
        if row.get("type") in ("assistant", "user"):
            return row.get("type") == "assistant" and (
                row.get("message", {}).get("stop_reason") == "end_turn"
            )
    return False


@dataclass
class IdleObservation:
    records: list[dict] = field(default_factory=list)
    sessions: list[ClaudeSession] = field(default_factory=list)
    deadlines: dict[Path, float] = field(default_factory=dict)
    ready: bool = False
    reason: str = "session-state-unknown"
    unreadable: int = 0


def observe_idle(config_dirs: list[Path], root: Path, now: float) -> IdleObservation:
    """One read shared by the switching guard and the status panel."""
    result = IdleObservation()
    problem = None
    for path in (root / "prompt-cache").glob("*.json"):
        try:
            record = json.loads(path.read_text())
            if not isinstance(record.get("session_id"), str) or not isinstance(record.get("transcript_path"), str):
                raise ValueError("invalid cache record")
            result.records.append(record)
        except (OSError, ValueError, TypeError, AttributeError):
            result.unreadable += 1
            problem = "session-state-unknown"
    try:
        for config in config_dirs:
            sessions, unreadable = scan_sessions(config)
            result.sessions.extend(sessions)
            result.unreadable += unreadable
            if unreadable:
                problem = "session-state-unknown"
            for lock in (config / "ide").glob("*.lock"):
                if is_pid_alive(json.loads(lock.read_text())["pid"]) and not sessions:
                    problem = "ide-session-unknown"
        by_session = {r["session_id"]: r for r in result.records}
        by_transcript = {Path(r["transcript_path"]): r for r in result.records}
        transcripts = set(by_transcript)
        for config in config_dirs:
            transcripts.update((config / "projects").rglob("*.jsonl"))
        for transcript in transcripts:
            try:
                expiry = transcript_deadline(transcript, by_transcript.get(transcript))
                if isinstance(expiry, bool) or not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                    raise ValueError("invalid cache deadline")
                result.deadlines[transcript] = expiry
            except (OSError, ValueError, TypeError, KeyError):
                problem = "session-state-unknown"
                result.unreadable += 1
        if problem:
            result.reason = problem
            return result
        for session in result.sessions:
            if session.status != "idle":
                result.reason = "session-busy-or-unknown"
                return result
            record = by_session.get(session.session_id)
            if not record or record.get("known") is not True:
                result.reason = "session-cache-unknown"
                return result
            transcript = Path(record["transcript_path"])
            for child in (transcript.parent / session.session_id / "subagents").glob("*.jsonl"):
                if not _subagent_finished(child):
                    result.reason = "subagent-busy-or-unknown"
                    return result
        if any(expiry > now for expiry in result.deadlines.values()):
            result.reason = "session-cache-warm"
            return result
        result.ready = True
        result.reason = "all-sessions-idle-and-cold"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        result.unreadable += 1
        result.reason = "session-state-unknown"
    return result


def idle_ready(config_dirs: list[Path], root: Path, now: float) -> tuple[bool, str]:
    observation = observe_idle(config_dirs, root, now)
    return observation.ready, observation.reason


def main() -> None:
    # Best effort only: this observer must never break the existing status line.
    try:
        record_status(json.load(sys.stdin), get_backup_root(), time.time())
    except (OSError, ValueError, TypeError, AttributeError, LockError):
        pass


if __name__ == "__main__":
    main()

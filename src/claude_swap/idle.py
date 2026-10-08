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

from claude_swap.paths import get_backup_root, get_claude_config_home, get_default_claude_config_home
from claude_swap.locking import FileLock
from claude_swap.exceptions import LockError
from claude_swap.process_detection import scan_sessions, is_pid_alive
from claude_swap.settings import atomic_write_json

QUIET_FALLBACK_S = 3600


def tracked_config_dirs(root: Path) -> list[Path]:
    profiles = [get_claude_config_home(), get_default_claude_config_home()]
    profiles.extend(p for p in (root / "sessions").glob("*") if p.is_dir())
    return list(dict.fromkeys(profiles))


def transcript_deadline(transcript: Path, record: dict | None) -> float:
    mtime = transcript.stat().st_mtime
    if record and record.get("known") is True and mtime <= record["transcript_mtime"]:
        return record.get("expires_at") or 0
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


def idle_ready(config_dirs: list[Path], root: Path, now: float) -> tuple[bool, str]:
    """Fail closed on busy/unknown sessions, live subagents, or warm caches."""
    try:
        records = [json.loads(p.read_text()) for p in (root / "prompt-cache").glob("*.json")]
        by_session = {r["session_id"]: r for r in records}
        by_transcript = {Path(r["transcript_path"]): r for r in records}
        live = []
        for config in config_dirs:
            sessions, unreadable = scan_sessions(config)
            if unreadable:
                return False, "session-state-unknown"
            for lock in (config / "ide").glob("*.lock"):
                if is_pid_alive(json.loads(lock.read_text())["pid"]) and not sessions:
                    return False, "ide-session-unknown"
            live.extend(sessions)
        for session in live:
            if session.status != "idle":
                return False, "session-busy-or-unknown"
            record = by_session.get(session.session_id)
            if not record or record.get("known") is not True:
                return False, "session-cache-unknown"
            transcript = Path(record["transcript_path"])
            if not transcript.is_file():
                return False, "session-transcript-unknown"
            for child in (transcript.parent / session.session_id / "subagents").glob("*.jsonl"):
                if not _subagent_finished(child):
                    return False, "subagent-busy-or-unknown"
        # Closed sessions still have caches. Include all configured profiles
        # and subagent transcripts, including sessions predating this observer.
        transcripts = set(by_transcript)
        for config in config_dirs:
            transcripts.update((config / "projects").rglob("*.jsonl"))
        for transcript in transcripts:
            record = by_transcript.get(transcript)
            expiry = transcript_deadline(transcript, record)
            if not isinstance(expiry, (int, float)) or not math.isfinite(expiry):
                return False, "session-cache-unknown"
            if expiry > now:
                return False, "session-cache-warm"
        return True, "all-sessions-idle-and-cold"
    except (OSError, ValueError, TypeError, KeyError, AttributeError, RecursionError):
        return False, "session-state-unknown"


def main() -> None:
    # Best effort only: this observer must never break the existing status line.
    try:
        record_status(json.load(sys.stdin), get_backup_root(), time.time())
    except (OSError, ValueError, TypeError, AttributeError, LockError):
        pass


if __name__ == "__main__":
    main()

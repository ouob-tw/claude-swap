"""``list --json --cached-only``: the account list from stored data alone.

For dashboards that poll: it reads the roster (``sequence.json``), the usage
cache and the live login's identity, and serves them with the regular
``list --json`` rows. It never constructs a switcher (whose setup runs data
migrations and opens the log), never reads a stored login, fetches, refreshes,
locks or writes anything — so it is safe beside a running ``cswap auto``.

Without the stored logins, ``loginExpiresAt`` is always absent and the states
only a login can reveal (no credentials, token expired, re-login needed, ...)
are not reported; usage rows follow the cache alone.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from claude_swap import paths, plans
from claude_swap.exceptions import ConfigError
from claude_swap.fsutil import read_text_with_retry
from claude_swap.json_output import (
    SCHEMA_VERSION,
    USAGE_API_KEY,
    _timestamp,
    account_row,
)
from claude_swap.settings import load_settings, parse_model_names
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import UsageStore

# Pause before the one re-read of a file caught mid-write.
RETRY_DELAY_S = 0.1


def cached_list_payload(
    backup_dir: Path, *, clock: Callable[[], float] = time.time
) -> dict:
    """The ``list --json`` payload from stored data, plus ``cachedOnly`` and
    ``generatedAt``. A file that cannot be parsed is read once more after
    :data:`RETRY_DELAY_S`; if it still cannot, ``ConfigError`` is raised and
    no partial payload is returned."""
    try:
        return _build(backup_dir, clock)
    except ConfigError:
        time.sleep(RETRY_DELAY_S)
    return _build(backup_dir, clock)


def _read_object(path: Path) -> dict | None:
    """A JSON object file; None when absent, ``ConfigError`` when unreadable."""
    try:
        data = json.loads(read_text_with_retry(path))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"{path} could not be read ({e})") from e
    if not isinstance(data, dict):
        raise ConfigError(f"{path} holds {type(data).__name__}, not a JSON object")
    return data


def _active_slot(data: dict) -> str | None:
    """The roster slot of the live login, by (email, organizationUuid)."""
    config = _read_object(paths.get_global_config_path()) or {}
    oauth_account = config.get("oauthAccount") or {}
    email = oauth_account.get("emailAddress", "") if isinstance(oauth_account, dict) else ""
    if not email:
        return None
    org_uuid = oauth_account.get("organizationUuid", "") or ""
    return ClaudeAccountSwitcher._find_account_slot(data, email, org_uuid)


def _build(backup_dir: Path, clock: Callable[[], float]) -> dict:
    now = clock()
    payload: dict = {
        "schemaVersion": SCHEMA_VERSION,
        "activeAccountNumber": None,
        "accounts": [],
    }
    sequence_file = backup_dir / "sequence.json"
    data = _read_object(sequence_file)
    if data is not None:
        records = data.get("accounts", {})
        sequence = data.get("sequence", [])
        if not isinstance(records, dict) or not isinstance(sequence, list) or not all(
            isinstance(r, dict) for r in records.values()
        ):
            raise ConfigError(f"{sequence_file} is malformed")
        active_slot = _active_slot(data)
        roster = []
        for num in sequence:
            record = records.get(str(num), {})
            email = record.get("email", "unknown")
            org_uuid = record.get("organizationUuid", "") or ""
            roster.append((num, record, email, org_uuid))
        models = parse_model_names(load_settings(backup_dir).model)
        store = UsageStore(backup_dir / "cache", clock=lambda: now)
        try:
            entries = store.entries(
                {str(num): (email, org_uuid) for num, _, email, org_uuid in roster},
                models,
                strict=True,
            )
        except ValueError as e:
            raise ConfigError(str(e)) from e
        for num, record, email, org_uuid in roster:
            is_active = str(num) == active_slot
            if is_active:
                payload["activeAccountNumber"] = num
            entry = entries[str(num)]
            usage = (
                USAGE_API_KEY if record.get("kind") == "api_key"
                else entry.decision_value()
            )
            payload["accounts"].append(
                account_row(
                    num, email, record.get("organizationName", "") or "",
                    org_uuid, is_active, usage,
                    usage_fetched_at=entry.fetched_at,
                    usage_age_s=entry.age_s,
                    last_good_usage=entry.last_good,
                    last_error=entry.last_error,
                    backoff_until=(
                        entry.backoff_until if entry.in_backoff(now) else None
                    ),
                    alias=record.get("alias", "") or "",
                    disabled=ClaudeAccountSwitcher._disabled_from_data(data, str(num)),
                    plan=plans.plan_view(record),
                )
            )
    payload["cachedOnly"] = True
    payload["generatedAt"] = _timestamp(now)
    return payload

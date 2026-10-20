"""Subscription plan of a managed account.

The plan is read from the account's live login (``claudeAiOauth``'s
``rateLimitTier`` / ``subscriptionType``) and stored on its sequence-file
record as ``plan: {id, observedAt}`` -- the plan code only, never any token.
Records without one (written before plans were tracked, or never observed)
read as ``unknown``, which counts as the smallest plan (multiplier 1).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

UNKNOWN = "unknown"

#: plan id -> (display label, quota multiplier relative to Pro)
PLANS: dict[str, tuple[str, int]] = {
    "pro": ("Pro", 1),
    "max_5x": ("Max 5x", 5),
    "max_20x": ("Max 20x", 20),
    UNKNOWN: ("Unknown", 1),
}

#: An unchanged plan refreshes its ``observedAt`` at most this often, so a
#: per-tick observation does not rewrite the sequence file every tick.
OBSERVED_REFRESH_S = 3600

_ISO_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def plan_id_from_credentials(credentials: str) -> str:
    """Plan id for a credentials JSON string; ``unknown`` when unrecognised."""
    try:
        data = json.loads(credentials or "")
    except json.JSONDecodeError:
        return UNKNOWN
    data = data.get("claudeAiOauth") if isinstance(data, dict) else None
    if not isinstance(data, dict):
        return UNKNOWN
    tier = data.get("rateLimitTier")
    if isinstance(tier, str):
        if "max_20x" in tier:
            return "max_20x"
        if "max_5x" in tier:
            return "max_5x"
    if data.get("subscriptionType") == "pro":
        return "pro"
    return UNKNOWN


def plan_view(account: dict | None) -> dict:
    """``{id, label, multiplier, observedAt}`` for a sequence-file record."""
    stored = (account or {}).get("plan")
    plan_id = stored.get("id") if isinstance(stored, dict) else None
    if not isinstance(plan_id, str) or plan_id not in PLANS:
        return {
            "id": UNKNOWN,
            "label": PLANS[UNKNOWN][0],
            "multiplier": PLANS[UNKNOWN][1],
            "observedAt": None,
        }
    observed_at = stored.get("observedAt")
    label, multiplier = PLANS[plan_id]
    return {
        "id": plan_id,
        "label": label,
        "multiplier": multiplier,
        "observedAt": observed_at if isinstance(observed_at, str) else None,
    }


def record_plan(account: dict, plan_id: str, now: float | None = None) -> bool:
    """Store ``plan_id`` on ``account``; return whether the record changed.

    An unchanged plan only refreshes ``observedAt`` once it is older than
    :data:`OBSERVED_REFRESH_S`, so callers can skip the file write on False.
    """
    now = time.time() if now is None else now
    stored = account.get("plan")
    if isinstance(stored, dict) and stored.get("id") == plan_id:
        try:
            observed = datetime.strptime(
                stored.get("observedAt") or "", _ISO_FORMAT
            ).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass
        else:
            if 0 <= now - observed.timestamp() < OBSERVED_REFRESH_S:
                return False
    account["plan"] = {
        "id": plan_id,
        "observedAt": datetime.fromtimestamp(now, tz=timezone.utc).strftime(
            _ISO_FORMAT
        ),
    }
    return True

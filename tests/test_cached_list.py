"""``cswap list --json --cached-only``: the read-only list for dashboards.

It serves what is already stored (roster, usage cache, live identity) with
the regular ``list --json`` row shape, and must never touch the network, a
login, a lock or any file.
"""

from __future__ import annotations

import hashlib
import json
import socket
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import cli, paths
from claude_swap.cached_list import cached_list_payload
from claude_swap.credentials import ActiveCredentials
from claude_swap.json_output import SCHEMA_VERSION
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.usage_store import FetchRecord, UsageStore

NOW = 1_800_000_000.0
SECRET_ACTIVE = "sk-ant-oat01-ACTIVE-SECRET"
SECRET_BACKUP = "sk-ant-oat01-BACKUP-SECRET"
SECRET_REFRESH = "sk-ant-ort01-REFRESH-SECRET"
USAGE = {
    "five_hour": {"pct": 12.0, "resets_at": "2027-01-15T08:00:00Z"},
    "seven_day": {"pct": 40.0, "resets_at": "2027-01-20T08:00:00Z"},
}


def _seed(temp_home: Path, now: float = NOW) -> Path:
    """Three accounts: 1 active with fresh usage, 2 expired, 3 never fetched."""
    backup = paths.get_backup_root()
    backup.mkdir(parents=True)
    accounts = {
        "1": {"email": "test@example.com", "organizationUuid": "", "uuid": "u1",
              "plan": {"id": "max_5x", "observedAt": "2026-10-21T00:00:00Z"}},
        "2": {"email": "two@example.com", "organizationUuid": "", "uuid": "u2",
              "alias": "second", "plan": {"id": "pro", "observedAt": "2026-10-20T00:00:00Z"}},
        "3": {"email": "three@example.com", "organizationUuid": "", "uuid": "u3",
              "disabled": True},
    }
    (backup / "sequence.json").write_text(json.dumps({
        "activeAccountNumber": 1, "sequence": [1, 2, 3], "accounts": accounts,
    }))
    identities = {n: (a["email"], "") for n, a in accounts.items()}
    UsageStore(backup / "cache", clock=lambda: now - 100).record(
        {"1": FetchRecord(usage=USAGE)}, {"1": identities["1"]})
    UsageStore(backup / "cache", clock=lambda: now - 4000).record(
        {"2": FetchRecord(usage=USAGE)}, {"2": identities["2"]})
    (temp_home / ".claude.json").write_text(json.dumps({
        "oauthAccount": {"emailAddress": "test@example.com", "accountUuid": "u1"},
    }))
    # Logins on disk that the read-only list must never read or echo.
    (temp_home / ".claude").mkdir(exist_ok=True)
    (temp_home / ".claude" / ".credentials.json").write_text(json.dumps({
        "claudeAiOauth": {"accessToken": SECRET_ACTIVE, "refreshToken": SECRET_REFRESH},
    }))
    return backup


def _tree(root: Path) -> dict[str, tuple[int, str]]:
    """Every file and directory under ``root``: mtime and content hash."""
    out = {}
    for p in sorted(root.rglob("*")):
        st = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir"
        out[str(p.relative_to(root))] = (st.st_mtime_ns, digest)
    return out


def _payload(backup: Path) -> dict:
    return cached_list_payload(backup, clock=lambda: NOW)


class TestCachedListPayload:
    def test_top_level_fields(self, temp_home: Path):
        payload = _payload(_seed(temp_home))
        assert payload["schemaVersion"] == SCHEMA_VERSION
        assert payload["activeAccountNumber"] == 1
        assert payload["cachedOnly"] is True
        assert payload["generatedAt"] == "2027-01-15T08:00:00Z"
        assert [a["number"] for a in payload["accounts"]] == [1, 2, 3]

    def test_fresh_row(self, temp_home: Path):
        row = _payload(_seed(temp_home))["accounts"][0]
        assert row["active"] is True
        assert row["usageStatus"] == "ok"
        assert row["usage"]["fiveHour"]["pct"] == 12.0
        assert row["usageFetchedAt"] == "2027-01-15T07:58:20Z"
        assert row["usageAgeSeconds"] == 100.0
        assert "lastGoodUsage" not in row

    def test_expired_row_moves_reading_to_last_good(self, temp_home: Path):
        row = _payload(_seed(temp_home))["accounts"][1]
        assert row["usageStatus"] == "unavailable"
        assert row["usage"] is None
        assert row["lastGoodUsage"]["sevenDay"]["pct"] == 40.0
        assert row["lastGoodFetchedAt"] == "2027-01-15T06:53:20Z"
        assert row["lastGoodAgeSeconds"] == 4000.0
        assert "usageFetchedAt" not in row and "usageAgeSeconds" not in row

    def test_never_fetched_row_omits_time_fields(self, temp_home: Path):
        row = _payload(_seed(temp_home))["accounts"][2]
        assert row["usageStatus"] == "unavailable"
        assert row["usage"] is None
        for key in ("usageFetchedAt", "usageAgeSeconds", "lastGoodUsage",
                    "lastGoodFetchedAt", "lastGoodAgeSeconds", "usageError",
                    "usageRetryAt"):
            assert key not in row

    def test_plan_alias_disabled_and_no_login_expiry(self, temp_home: Path):
        rows = _payload(_seed(temp_home))["accounts"]
        assert rows[0]["plan"] == {"id": "max_5x", "label": "Max 5x",
                                   "multiplier": 5, "observedAt": "2026-10-21T00:00:00Z"}
        assert rows[1]["plan"]["id"] == "pro"
        assert rows[1]["alias"] == "second"
        assert rows[2]["plan"] == {"id": "unknown", "label": "Unknown",
                                   "multiplier": 1, "observedAt": None}
        assert rows[2]["disabled"] is True
        assert all("loginExpiresAt" not in r for r in rows)
        assert all("stale" not in r for r in rows)

    def test_failure_fields_follow_the_cache(self, temp_home: Path):
        backup = _seed(temp_home)
        UsageStore(backup / "cache", clock=lambda: NOW - 10).record(
            {"3": FetchRecord(error="http-429", retry_after_s=600)},
            {"3": ("three@example.com", "")})
        row = _payload(backup)["accounts"][2]
        assert row["usageError"] == "http-429"
        assert row["usageRetryAt"] == "2027-01-15T08:09:50Z"

    def test_api_key_account_reports_api_key(self, temp_home: Path):
        backup = _seed(temp_home)
        seq = json.loads((backup / "sequence.json").read_text())
        seq["accounts"]["3"]["kind"] = "api_key"
        (backup / "sequence.json").write_text(json.dumps(seq))
        assert _payload(backup)["accounts"][2]["usageStatus"] == "api_key"

    def test_no_roster_is_an_empty_list(self, temp_home: Path):
        payload = cached_list_payload(paths.get_backup_root(), clock=lambda: NOW)
        assert payload == {
            "schemaVersion": SCHEMA_VERSION, "activeAccountNumber": None,
            "accounts": [], "cachedOnly": True, "generatedAt": "2027-01-15T08:00:00Z",
        }
        assert not paths.get_backup_root().exists()

    def test_no_live_login_means_no_active_account(self, temp_home: Path):
        backup = _seed(temp_home)
        (temp_home / ".claude.json").unlink()
        payload = _payload(backup)
        assert payload["activeAccountNumber"] is None
        assert not any(r["active"] for r in payload["accounts"])

    def test_rows_match_regular_list_on_the_same_cache(self, temp_home: Path):
        """Same cache, no fetch: rows differ only by ``loginExpiresAt``."""
        backup = _seed(temp_home)
        usage_file = backup / "cache" / "usage.json"
        before = usage_file.read_bytes()
        cached = _payload(backup)

        switcher = ClaudeAccountSwitcher()
        switcher._usage_store.clock = lambda: NOW
        active = json.dumps({"claudeAiOauth": {"accessToken": SECRET_ACTIVE}})
        backups = {
            "2": json.dumps({"claudeAiOauth": {"accessToken": SECRET_BACKUP + "2",
                             "refreshTokenExpiresAt": 1791421596865}}),
            "3": json.dumps({"claudeAiOauth": {"accessToken": SECRET_BACKUP + "3"}}),
        }
        with patch.object(switcher, "_read_active_credentials",
                          return_value=ActiveCredentials(active, False)), \
             patch.object(switcher, "_read_account_credentials",
                          side_effect=lambda num, _email: backups[num]), \
             patch.object(UsageStore, "reserve", return_value={}):
            regular = switcher.list_accounts(json_output=True)
        assert usage_file.read_bytes() == before  # the regular list fetched nothing

        assert regular["accounts"][1].pop("loginExpiresAt") == "2026-10-08T01:06:36Z"
        assert cached["accounts"] == regular["accounts"]
        assert cached["schemaVersion"] == regular["schemaVersion"]
        assert cached["activeAccountNumber"] == regular["activeAccountNumber"]


def _run_cli(argv: list[str], capsys) -> tuple[int, str, str]:
    with patch.object(sys, "argv", ["claude-swap", *argv]):
        try:
            cli.main()
            code = 0
        except SystemExit as e:
            code = e.code
    out, err = capsys.readouterr()
    return code, out, err


class TestCachedOnlyCli:
    @pytest.mark.parametrize("argv", [
        ["list", "--cached-only"],
        ["status", "--json", "--cached-only"],
        ["switch", "--json", "--cached-only"],
    ])
    def test_rejected_outside_list_json(self, argv, capsys):
        code, _out, err = _run_cli(argv, capsys)
        assert code == 2
        assert "--cached-only can only be used with 'list --json'" in err

    def test_prints_the_cached_payload(self, temp_home: Path, capsys):
        _seed(temp_home, now=time.time())
        code, out, _err = _run_cli(["list", "--json", "--cached-only"], capsys)
        assert code == 0
        payload = json.loads(out)
        assert payload["cachedOnly"] is True
        assert payload["generatedAt"].endswith("Z")
        assert [a["usageStatus"] for a in payload["accounts"]] == [
            "ok", "unavailable", "unavailable"]

    def test_reads_only(self, temp_home: Path, capsys):
        """No network, no login refresh, no lock, no file created or changed."""
        _seed(temp_home)
        before = _tree(temp_home)

        def refuse(*_a, **_k):
            raise AssertionError("network or lock used")

        with patch.object(socket.socket, "connect", refuse), \
             patch("socket.create_connection", refuse), \
             patch("urllib.request.urlopen", refuse), \
             patch("claude_swap.oauth.try_refresh_oauth_credentials", refuse), \
             patch("claude_swap.oauth.request_usage_data", refuse), \
             patch("claude_swap.locking.FileLock.acquire", refuse), \
             patch("claude_swap.cli.ClaudeAccountSwitcher", side_effect=refuse):
            code, out, _err = _run_cli(["list", "--json", "--cached-only"], capsys)
        assert code == 0
        assert json.loads(out)["cachedOnly"] is True
        assert _tree(temp_home) == before

    def test_output_carries_no_secret(self, temp_home: Path, capsys):
        backup = _seed(temp_home)
        (backup / "credentials").mkdir()
        (backup / "credentials" / "2.enc").write_text(SECRET_BACKUP)
        _code, out, _err = _run_cli(["list", "--json", "--cached-only"], capsys)
        for secret in (SECRET_ACTIVE, SECRET_BACKUP, SECRET_REFRESH,
                       "accessToken", "refreshToken"):
            assert secret not in out

    def test_safe_while_auto_holds_its_locks(self, temp_home: Path, capsys):
        """A running ``cswap auto`` holding the store locks does not block it."""
        from claude_swap.locking import FileLock

        backup = _seed(temp_home)
        with FileLock(backup / "cache" / ".usage.lock"), FileLock(backup / ".lock"):
            code, out, _err = _run_cli(["list", "--json", "--cached-only"], capsys)
        assert code == 0
        assert json.loads(out)["accounts"][0]["usageStatus"] == "ok"

    @pytest.mark.parametrize("name", ["sequence.json", "cache/usage.json", ".claude.json"])
    def test_torn_read_retried_once(self, temp_home: Path, capsys, name):
        backup = _seed(temp_home)
        target = (temp_home if name == ".claude.json" else backup) / name
        good = target.read_text()
        target.write_text(good[: len(good) // 2])
        reads = []

        def heal(_delay):
            reads.append(1)
            target.write_text(good)

        with patch("claude_swap.cached_list.time.sleep", side_effect=heal):
            code, out, _err = _run_cli(["list", "--json", "--cached-only"], capsys)
        assert reads == [1]
        assert code == 0
        assert len(json.loads(out)["accounts"]) == 3

    @pytest.mark.parametrize("name", ["sequence.json", "cache/usage.json", ".claude.json"])
    def test_still_torn_after_retry_is_an_error_envelope(
        self, temp_home: Path, capsys, name
    ):
        backup = _seed(temp_home)
        target = (temp_home if name == ".claude.json" else backup) / name
        target.write_text("{\"accounts\": [")
        sleeps = []
        with patch("claude_swap.cached_list.time.sleep", side_effect=sleeps.append):
            code, out, _err = _run_cli(["list", "--json", "--cached-only"], capsys)
        assert len(sleeps) == 1
        assert code == 1
        payload = json.loads(out)
        assert set(payload) == {"schemaVersion", "error"}
        assert payload["error"]["type"] == "ConfigError"

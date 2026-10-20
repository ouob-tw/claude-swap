"""Account plan tracking: recorded on switch / add / auto, shown by list and TUI."""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest

from claude_swap import plans
from claude_swap.credentials import ActiveCredentials
from claude_swap.exceptions import ConfigError
from claude_swap.models import AccountSnapshot
from claude_swap.switcher import ClaudeAccountSwitcher
from claude_swap.tui.widgets import account_card_text, mini_account_text
from claude_swap.usage_store import UsageEntry

from tests.test_autoswitch import EngineHarness
from tests.test_switcher import TestPerformSwitchPostDisplay as _SwitchHelpers

ISO_UTC = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")


def _creds(token: str, *, sub=None, tier=None) -> str:
    blob = {"accessToken": f"sk-{token}", "refreshToken": f"rt-{token}"}
    if sub is not None:
        blob["subscriptionType"] = sub
    if tier is not None:
        blob["rateLimitTier"] = tier
    return json.dumps({"claudeAiOauth": blob})


def _seq_text(switcher: ClaudeAccountSwitcher) -> str:
    return switcher.sequence_file.read_text()


class TestPlanMapping:
    @pytest.mark.parametrize(
        ("sub", "tier", "expected"),
        [
            ("max", "default_claude_max_20x", "max_20x"),
            ("max", "default_claude_max_5x", "max_5x"),
            ("pro", "default_claude_ai", "pro"),
            ("team", "default_claude_ai", "unknown"),
            ("max", None, "unknown"),
            (None, None, "unknown"),
        ],
    )
    def test_credentials_map_to_plan(self, sub, tier, expected):
        assert plans.plan_id_from_credentials(_creds("x", sub=sub, tier=tier)) == expected

    @pytest.mark.parametrize("raw", ["", "not json", "sk-ant-api03-key", "[]"])
    def test_unreadable_credentials_are_unknown(self, raw):
        assert plans.plan_id_from_credentials(raw) == "unknown"

    @pytest.mark.parametrize(
        ("plan_id", "label", "multiplier"),
        [
            ("pro", "Pro", 1),
            ("max_5x", "Max 5x", 5),
            ("max_20x", "Max 20x", 20),
            ("unknown", "Unknown", 1),
        ],
    )
    def test_labels_and_multipliers(self, plan_id, label, multiplier):
        view = plans.plan_view({"plan": {"id": plan_id, "observedAt": "2026-10-21T00:00:00Z"}})
        assert (view["label"], view["multiplier"]) == (label, multiplier)

    @pytest.mark.parametrize(
        "record",
        [
            None, {}, {"plan": None}, {"plan": "pro"}, {"plan": {"id": "team"}},
            {"plan": {"id": []}}, {"plan": {"id": {}}},
        ],
    )
    def test_missing_or_malformed_record_reads_unknown(self, record):
        assert plans.plan_view(record) == {
            "id": "unknown", "label": "Unknown", "multiplier": 1, "observedAt": None,
        }


def _switch_setup(temp_home, sample_sequence_data, target_creds):
    switcher, creds_store, configs_store = (
        _SwitchHelpers()._setup_two_accounts(temp_home, sample_sequence_data)
    )
    creds_store[("2", "account2@example.com")] = target_creds
    live_state = {"creds": (temp_home / ".claude" / ".credentials.json").read_text()}
    patches = _SwitchHelpers._install_store_patches(
        switcher, creds_store, configs_store, live_state
    )
    return switcher, patches


class TestSwitchRecordsPlan:
    @pytest.mark.parametrize("force", [False, True])
    def test_switch_records_target_plan(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict, force
    ):
        switcher, patches = _switch_setup(
            temp_home, sample_sequence_data,
            _creds("two", sub="max", tier="default_claude_max_5x"),
        )
        try:
            switcher.switch_to("2", json_output=True, force=force)
        finally:
            for p in patches:
                p.stop()
        plan = switcher._get_sequence_data()["accounts"]["2"]["plan"]
        assert plan["id"] == "max_5x"
        assert ISO_UTC.match(plan["observedAt"])
        assert switcher.account_plan_multiplier("2") == 5
        text = _seq_text(switcher)
        assert "sk-" not in text and "rt-" not in text

    def test_unrecognised_plan_is_stored_unknown(
        self, temp_home: Path, mock_claude_config: Path, sample_sequence_data: dict
    ):
        switcher, patches = _switch_setup(
            temp_home, sample_sequence_data, _creds("two", sub="team")
        )
        try:
            switcher.switch_to("2", json_output=True)
        finally:
            for p in patches:
                p.stop()
        assert switcher._get_sequence_data()["accounts"]["2"]["plan"]["id"] == "unknown"


class TestAddRecordsPlan:
    def _switcher(self):
        switcher = ClaudeAccountSwitcher()
        switcher._setup_directories()
        switcher._init_sequence_file()
        return switcher

    OWNER = {"uuid": "test-uuid-1234", "email": "test@example.com", "organizationUuid": ""}

    def _add(self, switcher, creds, profile=OWNER):
        stored: dict = {}
        with patch(
            "claude_swap.oauth.fetch_oauth_profile", return_value=profile
        ), patch.object(
            switcher, "_read_active_credentials",
            return_value=ActiveCredentials(creds, False),
        ), patch.object(
            switcher, "_write_account_credentials",
            side_effect=lambda n, e, c: stored.update(creds=c),
        ):
            switcher.add_account()

    def test_add_new_and_readd_record_plan(
        self, temp_home: Path, mock_claude_config: Path, capsys
    ):
        switcher = self._switcher()
        self._add(switcher, _creds("a", sub="pro", tier="default_claude_ai"))
        plan = switcher._get_sequence_data()["accounts"]["1"]["plan"]
        assert plan["id"] == "pro" and ISO_UTC.match(plan["observedAt"])

        self._add(switcher, _creds("b", sub="max", tier="default_claude_max_20x"))
        assert switcher._get_sequence_data()["accounts"]["1"]["plan"]["id"] == "max_20x"
        text = _seq_text(switcher)
        assert "sk-" not in text and "rt-" not in text

    def test_foreign_credential_records_nothing(
        self, temp_home: Path, mock_claude_config: Path, capsys
    ):
        switcher = self._switcher()
        with pytest.raises(ConfigError):
            self._add(
                switcher, _creds("a", sub="pro"),
                profile={"uuid": "someone-else", "email": "x@example.com"},
            )
        assert "plan" not in _seq_text(switcher)

    @pytest.mark.parametrize(
        "profile",
        [None, {"uuid": "test-uuid-1234", "email": "test@example.com"}],
        ids=["lookup-failed", "org-unverifiable"],
    )
    def test_unconfirmed_owner_adds_without_plan(
        self, temp_home: Path, mock_claude_config: Path, capsys, profile
    ):
        switcher = self._switcher()
        self._add(switcher, _creds("a", sub="pro"), profile=profile)
        assert "1" in switcher._get_sequence_data()["accounts"]
        self._add(switcher, _creds("b", sub="pro"), profile=profile)
        assert "plan" not in _seq_text(switcher)


class TestAutoRecordsPlan:
    PRO = _creds("1", sub="pro", tier="default_claude_ai")

    @pytest.fixture
    def h(self, temp_home: Path) -> EngineHarness:
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        h.switcher._write_account_credentials("1", "a@example.com", self.PRO)
        (temp_home / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "a@example.com", "accountUuid": "uuid-1"},
        }))
        self.live = temp_home / ".claude" / ".credentials.json"
        self.live.write_text(self.PRO)
        h.clock.now = 1_792_540_800.0  # 2026-10-21T00:00:00Z
        return h

    def _tick(self, h):
        h.tick_with_usage({"1": {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}},
                           "2": {"five_hour": {"pct": 10.0}, "seven_day": {"pct": 10.0}}})

    def test_tick_records_active_plan(self, h):
        self._tick(h)
        plan = h.switcher._get_sequence_data()["accounts"]["1"]["plan"]
        assert plan == {"id": "pro", "observedAt": "2026-10-21T00:00:00Z"}
        assert "plan" not in h.switcher._get_sequence_data()["accounts"]["2"]
        text = _seq_text(h.switcher)
        assert "sk-" not in text and "rt-" not in text

    def test_foreign_live_credential_is_not_recorded(self, h):
        self.live.write_text(_creds("other", sub="max", tier="default_claude_max_5x"))
        self._tick(h)
        assert "plan" not in h.switcher._get_sequence_data()["accounts"]["1"]

    def test_login_replaced_before_lock_records_nothing(self, h):
        """The live login is read under the lock, not before it: a foreign
        login landing just before the lock is taken is never filed."""
        import claude_swap.switcher as switcher_mod

        real_lock = switcher_mod.FileLock
        live = self.live

        class SwapOnEnter:
            def __init__(self, *a, **kw):
                self._lock = real_lock(*a, **kw)

            def __enter__(self):
                live.write_text(_creds("other", sub="max", tier="default_claude_max_20x"))
                return self._lock.__enter__()

            def __exit__(self, *exc):
                return self._lock.__exit__(*exc)

        with patch.object(switcher_mod, "FileLock", SwapOnEnter):
            assert h.switcher.record_active_plan("1") is False
        assert "plan" not in h.switcher._get_sequence_data()["accounts"]["1"]

    def test_dry_run_records_nothing(self, h):
        h.engine = h._make_engine(dry_run=True)
        self._tick(h)
        assert "plan" not in h.switcher._get_sequence_data()["accounts"]["1"]

    def test_unchanged_plan_rewrites_at_most_hourly(self, h):
        self._tick(h)
        writes = []
        real_write = h.switcher._write_json

        def spy(path, data):
            if path == h.switcher.sequence_file:
                writes.append(data)
            return real_write(path, data)

        with patch.object(h.switcher, "_write_json", side_effect=spy):
            for _ in range(5):
                h.clock.advance(60)
                self._tick(h)
            assert writes == []
            h.clock.advance(3600)
            self._tick(h)
        assert len(writes) == 1
        assert writes[0]["accounts"]["1"]["plan"]["observedAt"] == "2026-10-21T01:05:00Z"

    def test_plan_change_is_recorded_on_next_tick(self, h):
        self._tick(h)
        upgraded = _creds("1", sub="max", tier="default_claude_max_5x")
        h.switcher._write_account_credentials("1", "a@example.com", upgraded)
        self.live.write_text(upgraded)
        h.clock.advance(60)
        self._tick(h)
        assert h.switcher.account_plan("1")["id"] == "max_5x"


class TestPlanDisplay:
    def _seeded(self, temp_home: Path) -> ClaudeAccountSwitcher:
        h = EngineHarness(temp_home)
        h.seed(1, "a@example.com")
        h.seed(2, "b@example.com")
        data = h.switcher._get_sequence_data()
        # Account 2 is a legacy record with no plan field at all.
        data["accounts"]["1"]["plan"] = {"id": "max_5x", "observedAt": "2026-10-21T00:00:00Z"}
        h.switcher._write_json(h.switcher.sequence_file, data)
        return h.switcher

    def _no_usage(self, switcher):
        return patch.object(
            switcher, "_collect_usage_entries",
            side_effect=lambda info, **kw: {str(i[0]): UsageEntry() for i in info},
        )

    def test_list_json_carries_plan(self, temp_home: Path):
        switcher = self._seeded(temp_home)
        with self._no_usage(switcher):
            payload = switcher.list_accounts(json_output=True)
        rows = {row["number"]: row for row in payload["accounts"]}
        assert rows[1]["plan"] == {
            "id": "max_5x", "label": "Max 5x", "multiplier": 5,
            "observedAt": "2026-10-21T00:00:00Z",
        }
        assert rows[2]["plan"] == {
            "id": "unknown", "label": "Unknown", "multiplier": 1, "observedAt": None,
        }
        assert switcher.account_plan_multiplier("2") == 1
        assert "sk-" not in json.dumps(payload)

    def test_list_header_shows_plan(self, temp_home: Path, capsys):
        switcher = self._seeded(temp_home)
        with self._no_usage(switcher), patch(
            "claude_swap.switcher.get_running_instances", return_value=([], [])
        ):
            switcher.list_accounts()
        out = capsys.readouterr().out
        header = {line.split(":")[0].strip(): line for line in out.splitlines() if ": " in line}
        assert "Max 5x" in header["1"]
        assert "Unknown" in header["2"]

    def test_tui_snapshot_and_rows_show_plan(self, temp_home: Path):
        switcher = self._seeded(temp_home)
        with self._no_usage(switcher):
            snap = switcher.accounts_snapshot()
        by_num = {a.number: a for a in snap.accounts}
        assert by_num["1"].plan == "max_5x"
        assert by_num["2"].plan == "unknown"
        assert "Max 5x" in account_card_text(by_num["1"], 100, now=0).plain
        assert "Unknown" in mini_account_text(by_num["2"], 0).plain

    def test_snapshot_default_plan_is_unknown(self):
        acc = AccountSnapshot(
            number="3", email="c@example.com", org_name="", org_uuid="",
            is_active=False, kind="oauth", switchable=True, usage=UsageEntry(),
        )
        assert "Unknown" in mini_account_text(acc, 0).plain

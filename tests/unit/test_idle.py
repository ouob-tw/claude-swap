"""Local metadata only; PID liveness is mocked, no Claude/provider requests."""
import json
import os

import pytest

from claude_swap.idle import idle_ready, record_status


@pytest.fixture
def observation(tmp_path, monkeypatch):
    monkeypatch.setattr("claude_swap.process_detection.is_pid_alive", lambda pid: True)
    monkeypatch.setattr("claude_swap.idle.is_pid_alive", lambda pid: True)
    config = tmp_path / "config"
    sessions = config / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "123.json").write_text(json.dumps({"pid": 123, "sessionId": "s1", "status": "idle"}))
    transcript = config / "projects" / "project" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    os.utime(transcript, (1000, 1000))
    root = tmp_path / "store"
    data = {"session_id": "s1", "transcript_path": str(transcript),
            "prompt_cache": {"expires_at": 2000, "ttl": "1h"}}
    record_status(data, root, 1000)
    return config, root, transcript, data


def test_open_idle_session_qualifies_only_after_expiry(observation):
    config, root, _, _ = observation
    assert idle_ready([config], root, 1999) == (False, "session-cache-warm")
    assert idle_ready([config], root, 2000)[0] is True


@pytest.mark.parametrize("status", ["busy", "waiting", None])
def test_busy_and_unknown_block(observation, status):
    config, root, _, _ = observation
    (config / "sessions" / "123.json").write_text(json.dumps({"pid": 123, "sessionId": "s1", "status": status}))
    assert idle_ready([config], root, 2000)[0] is False


def test_missing_cache_observation_blocks_even_after_hours(observation):
    config, root, _, data = observation
    data.pop("prompt_cache")
    record_status(data, root, 1000)
    assert idle_ready([config], root, 9000) == (False, "session-cache-unknown")


def test_later_activity_restarts_conservative_wait(observation):
    config, root, transcript, _ = observation
    os.utime(transcript, (1900, 1900))
    assert idle_ready([config], root, 2000)[0] is False
    assert idle_ready([config], root, 5500)[0] is True


def test_closed_session_still_has_warm_cache(observation, monkeypatch):
    config, root, _, _ = observation
    monkeypatch.setattr("claude_swap.process_detection.is_pid_alive", lambda pid: False)
    assert idle_ready([config], root, 1500)[0] is False
    assert idle_ready([config], root, 2000)[0] is True


def test_no_cache_response_does_not_erase_old_prefix(observation):
    config, root, _, data = observation
    data["prompt_cache"]["expires_at"] = None
    record_status(data, root, 1500)
    assert idle_ready([config], root, 1900)[0] is False


def test_unreadable_session_record_blocks(observation):
    config, root, _, _ = observation
    (config / "sessions" / "bad.json").write_text("broken")
    assert idle_ready([config], root, 9000) == (False, "session-state-unknown")


def test_live_subagent_blocks_after_its_cache_expires(observation):
    config, root, transcript, _ = observation
    child = transcript.parent / "s1" / "subagents" / "agent-abc.jsonl"
    child.parent.mkdir(parents=True)
    child.write_text(json.dumps({"type": "assistant", "message": {"stop_reason": "tool_use"}}) + "\n")
    os.utime(child, (1000, 1000))
    assert idle_ready([config], root, 9000) == (False, "subagent-busy-or-unknown")
    child.write_text(json.dumps({"type": "assistant", "message": {"stop_reason": "end_turn"}}) + "\n")
    os.utime(child, (1000, 1000))
    assert idle_ready([config], root, 2000)[0] is False
    assert idle_ready([config], root, 4600)[0] is True


def test_observer_does_not_store_messages_or_credentials(observation):
    _, root, _, data = observation
    data["message"] = "private prompt"
    data["token"] = "private token"
    record_status(data, root, 1000)
    text = next((root / "prompt-cache").glob("*.json")).read_text()
    assert "private" not in text


def test_first_observation_without_deadline_preserves_possible_old_cache(observation):
    config, root, _, data = observation
    data["session_id"] = "new-session"
    data["prompt_cache"] = {"expires_at": None, "caching_observed": True}
    record_status(data, root, 1500)
    assert idle_ready([config], root, 2000)[0] is False

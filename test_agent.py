from unittest.mock import patch

import pytest

import agent
import flagsmith_live as fl
from test_flagsmith_live import BASE_WEIGHTS, EXPERIMENT_1_DETAIL, EXPERIMENT_1_RESULTS


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setattr(fl, "GITHUB_TOKEN", "test-github-token")
    monkeypatch.setattr(fl, "GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(fl, "GITHUB_ISSUE_NUMBER", "7")
    monkeypatch.setattr(fl, "FLAGSMITH_FLAG", "checkout_button")


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "agent_state.json")


def _report():
    return fl.evaluate_gate(EXPERIMENT_1_DETAIL, EXPERIMENT_1_RESULTS, BASE_WEIGHTS)


def _comment(cid, body, login="maint"):
    return {"id": cid, "body": body, "user": {"login": login}, "created_at": f"2026-11-27T01:00:{cid:02d}Z"}


def _fake_get(comments, permissions):
    def fake_get(url, **kwargs):
        if url.endswith("/comments"):
            return FakeResponse(200, comments)
        if "/collaborators/" in url:
            login = url.split("/collaborators/")[1].split("/")[0]
            return FakeResponse(200, {"permission": permissions.get(login, "none")})
        raise AssertionError(f"unexpected GET {url}")
    return fake_get


def _state(handled=(), posted=None):
    return {"handled": list(handled), "posted_hashes": dict(posted or {}), "bootstrapped": True}


def test_check_experiment_1_posts_one_card_and_records_hash(config, state_path):
    report = _report()
    state = _state()
    comments = [_comment(10, "check experiment 1")]
    with patch.object(fl.requests, "get", side_effect=_fake_get(comments, {"maint": "write"})), \
         patch.object(fl, "current_gate", return_value=report), \
         patch.object(fl, "explain_with_guard", return_value=("Explanation text.", "m/x:free")), \
         patch.object(fl, "post_card", return_value=555) as post_card:
        agent.poll_once(state, state_path)
    assert post_card.call_count == 1
    card = post_card.call_args.args[0]
    assert "## Experiment Doctor verdict: DO NOT SHIP" in card
    assert "### Explanation" in card
    assert state["posted_hashes"]["1"] == fl.verdict_hash(report)
    assert 10 in state["handled"]


def test_comment_already_in_state_is_ignored(config, state_path):
    state = _state(handled=[10])
    comments = [_comment(10, "check experiment 1")]
    with patch.object(fl.requests, "get", side_effect=_fake_get(comments, {"maint": "write"})), \
         patch.object(fl, "current_gate") as current_gate, \
         patch.object(fl, "post_card") as post_card:
        agent.poll_once(state, state_path)
    current_gate.assert_not_called()
    post_card.assert_not_called()


def test_pause_from_user_without_write_access_is_refused(config, state_path):
    state = _state(posted={"1": fl.verdict_hash(_report())})
    h = fl.verdict_hash(_report())
    comments = [_comment(11, f"pause checkout_button with {h}", login="reader")]
    with patch.object(fl.requests, "get", side_effect=_fake_get(comments, {"reader": "read"})), \
         patch.object(fl, "approve_and_pause") as approve, \
         patch.object(fl, "post_card", return_value=1) as post_card:
        agent.poll_once(state, state_path)
    approve.assert_not_called()
    assert post_card.call_count == 1
    assert "REFUSED" in post_card.call_args.args[0]


def test_pause_with_wrong_hash_is_refused(config, state_path):
    h = fl.verdict_hash(_report())
    state = _state(posted={"1": h})
    comments = [_comment(12, "pause checkout_button with " + "0" * 64)]
    with patch.object(fl.requests, "get", side_effect=_fake_get(comments, {"maint": "write"})), \
         patch.object(fl, "approve_and_pause") as approve, \
         patch.object(fl, "post_card", return_value=1) as post_card:
        agent.poll_once(state, state_path)
    approve.assert_not_called()
    assert "does not match" in post_card.call_args.args[0]


def test_error_in_one_step_posts_error_and_polling_continues(config, state_path):
    report = _report()
    state = _state()
    comments = [_comment(1, "check experiment 1"), _comment(2, "check experiment 2")]

    def current_gate(experiment_id):
        if experiment_id == 1:
            raise RuntimeError("boom")
        return report

    with patch.object(fl.requests, "get", side_effect=_fake_get(comments, {"maint": "write"})), \
         patch.object(fl, "current_gate", side_effect=current_gate) as cg, \
         patch.object(fl, "explain_with_guard", return_value=("t", "m:free")), \
         patch.object(fl, "post_card", return_value=1) as post_card:
        agent.poll_once(state, state_path)
    # experiment 1: one try plus one retry; experiment 2: one try.
    assert cg.call_count == 3
    bodies = [c.args[0] for c in post_card.call_args_list]
    assert any(b == "Agent error: RuntimeError: boom" for b in bodies)
    assert "2" in state["posted_hashes"]
    assert set(state["handled"]) == {1, 2}

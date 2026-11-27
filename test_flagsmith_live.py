from flagsmith_live import (
    DO_NOT_SHIP,
    NOT_CHECKED,
    PASS,
    check_rollout_vs_base,
    evaluate_gate,
    render_card,
)

# Numbers from experiment 1 (as read earlier from the live API), reproduced as a mocked response.
EXPERIMENT_1_DETAIL = {
    "id": 1,
    "status": "running",
    "feature": {"id": 1, "name": "checkout_button"},
    "experiment_rollout": {
        "enabled": True,
        "rollout_percentage": 100.0,
        "multivariate_feature_state_values": [
            {"multivariate_feature_option": 2, "percentage_allocation": 50.0}
        ],
    },
}

EXPERIMENT_1_RESULTS = {
    "results": {
        "as_of": "2026-11-26T14:59:00.271136Z",
        "is_final": False,
        "payload": {
            "metrics": [
                {
                    "variants": {
                        "control": {"n": 1924, "sum": 171.0, "sum_squares": 171.0},
                        "V_2": {"n": 1611, "sum": 161.0, "sum_squares": 161.0},
                    },
                    "conversions_timeseries": {"points": []},
                }
            ],
            "exposures_timeseries": {"points": []},
        },
    }
}

# Base allocation for option 2 in the environment is 0% (mismatch with the 50% rollout).
BASE_WEIGHTS = {2: 0.0}


def test_gate_experiment_1_is_do_not_ship():
    report = evaluate_gate(EXPERIMENT_1_DETAIL, EXPERIMENT_1_RESULTS, BASE_WEIGHTS)
    checks = {name: res for name, res, _ in report["checks"]}
    # Split check: 1924 vs 1611 against a 50/50 plan is a sample-ratio mismatch.
    assert checks["split (chi-square)"] == "FAIL"
    assert checks["rollout vs base"] == NOT_CHECKED
    assert checks["seen"] == NOT_CHECKED
    assert report["gate"] == DO_NOT_SHIP


def test_split_check_alone_passes_when_balanced():
    balanced = {
        "results": {
            "as_of": "x",
            "is_final": True,
            "payload": {
                "metrics": [
                    {
                        "variants": {
                            "control": {"n": 1000, "sum": 1.0, "sum_squares": 1.0},
                            "V_2": {"n": 1000, "sum": 1.0, "sum_squares": 1.0},
                        },
                        "conversions_timeseries": {"points": []},
                    }
                ],
                "exposures_timeseries": {"points": []},
            },
        }
    }
    report = evaluate_gate(EXPERIMENT_1_DETAIL, balanced, {2: 50.0})
    checks = {name: res for name, res, _ in report["checks"]}
    assert checks["split (chi-square)"] == PASS
    assert checks["rollout vs base"] == PASS


def _rollout(*allocations):
    return {
        "multivariate_feature_state_values": [
            {"multivariate_feature_option": opt, "percentage_allocation": pct}
            for opt, pct in allocations
        ]
    }


def test_rollout_matches_base_returns_pass():
    status, reason = check_rollout_vs_base(_rollout((2, 50.0)), {2: 50.0})
    assert status == PASS
    assert reason is None


def test_rollout_differs_from_base_returns_not_checked_not_fail():
    # Numbers from experiment 1: rollout 50%, base 0%.
    status, reason = check_rollout_vs_base(_rollout((2, 50.0)), {2: 0.0})
    assert status == NOT_CHECKED
    assert reason == "rollout says 50.0%, base says 0.0%; which one applies is unverified"


def test_missing_allocation_returns_not_checked():
    status, reason = check_rollout_vs_base(_rollout((2, 50.0)), {})
    assert status == NOT_CHECKED
    assert reason == "missing allocation"

    status, reason = check_rollout_vs_base(
        {"multivariate_feature_state_values": [{"multivariate_feature_option": 2}]},
        {2: 50.0},
    )
    assert status == NOT_CHECKED
    assert reason == "missing allocation"


def test_render_card_experiment_1_title_and_three_rows():
    report = evaluate_gate(EXPERIMENT_1_DETAIL, EXPERIMENT_1_RESULTS, BASE_WEIGHTS)
    card = render_card(report)
    assert card.splitlines()[0] == "## Experiment Doctor verdict: DO NOT SHIP"
    table_rows = [
        line for line in card.splitlines()
        if line.startswith("| ") and not line.startswith("| Check") and not line.startswith("| ---")
    ]
    assert len(table_rows) == 3
    assert "/approve" in card


# ---- GitHub posting and approval (all HTTP mocked) ----

import copy  # noqa: E402
from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402

import flagsmith_live as fl  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def github_config(monkeypatch):
    monkeypatch.setattr(fl, "GITHUB_TOKEN", "test-github-token")
    monkeypatch.setattr(fl, "GITHUB_REPO", "owner/repo")
    monkeypatch.setattr(fl, "GITHUB_ISSUE_NUMBER", "7")
    monkeypatch.setattr(fl, "FLAGSMITH_EXPERIMENT_ID", "1")


def _experiment_1_report():
    return evaluate_gate(EXPERIMENT_1_DETAIL, EXPERIMENT_1_RESULTS, BASE_WEIGHTS)


def test_post_card_builds_url_and_bearer_header(github_config):
    with patch.object(fl.requests, "post", return_value=FakeResponse(201, {"id": 99})) as post:
        comment_id = fl.post_card("card body")
    url = post.call_args.args[0]
    headers = post.call_args.kwargs["headers"]
    assert url == "https://api.github.com/repos/owner/repo/issues/7/comments"
    assert headers["Authorization"] == "Bearer test-github-token"
    assert post.call_args.kwargs["json"] == {"body": "card body"}
    assert comment_id == 99


def test_approval_refused_when_commenter_lacks_write_access(github_config):
    report = _experiment_1_report()
    h = fl.verdict_hash(report)
    comments = [
        {"body": f"/approve {h}", "user": {"login": "reader"}, "created_at": "2026-11-26T15:00:00Z"}
    ]

    def fake_get(url, **kwargs):
        if url.endswith("/comments"):
            return FakeResponse(200, comments)
        if "/collaborators/reader/permission" in url:
            return FakeResponse(200, {"permission": "read"})
        raise AssertionError(f"unexpected GET {url}")

    with patch.object(fl.requests, "get", side_effect=fake_get):
        assert fl.check_approval(h) is False


def test_approval_accepted_for_write_access(github_config):
    report = _experiment_1_report()
    h = fl.verdict_hash(report)
    comments = [
        {"body": f"/approve {h}", "user": {"login": "maintainer"}, "created_at": "2026-11-26T15:00:00Z"}
    ]

    def fake_get(url, **kwargs):
        if url.endswith("/comments"):
            return FakeResponse(200, comments)
        if "/collaborators/maintainer/permission" in url:
            return FakeResponse(200, {"permission": "write"})
        raise AssertionError(f"unexpected GET {url}")

    with patch.object(fl.requests, "get", side_effect=fake_get):
        assert fl.check_approval(h) is True


def test_approval_refused_when_verdict_hash_changed(github_config):
    original = _experiment_1_report()
    posted_hash = fl.verdict_hash(original)
    changed = copy.deepcopy(original)
    changed["gate"] = fl.SHIP  # verdict now differs from the posted card

    with patch.object(fl, "current_gate", return_value=changed), \
         patch.object(fl, "set_flag") as set_flag, \
         patch.object(fl, "_post_comment") as post_comment, \
         patch.object(fl, "check_approval") as check:
        result = fl.approve_and_pause(posted_hash)

    assert result == "REFUSED: verdict changed"
    set_flag.assert_not_called()
    check.assert_not_called()
    post_comment.assert_called_once()
    assert "Verdict changed" in post_comment.call_args.args[0]


def test_approval_accepted_calls_set_flag_once_with_false(github_config):
    report = _experiment_1_report()
    h = fl.verdict_hash(report)
    comments = [
        {"body": f"/approve {h}", "user": {"login": "maintainer"}, "created_at": "2026-11-26T15:00:00Z"}
    ]

    def fake_get(url, **kwargs):
        if url.endswith("/comments"):
            return FakeResponse(200, comments)
        if "/collaborators/maintainer/permission" in url:
            return FakeResponse(200, {"permission": "admin"})
        raise AssertionError(f"unexpected GET {url}")

    with patch.object(fl, "current_gate", return_value=report), \
         patch.object(fl.requests, "get", side_effect=fake_get), \
         patch.object(fl, "set_flag", return_value=(200, {"enabled": False})) as set_flag, \
         patch.object(fl, "_create_issue") as create_issue:
        result = fl.approve_and_pause(h)

    assert result == "PAUSED"
    set_flag.assert_called_once_with(False)
    create_issue.assert_not_called()


def test_set_flag_failure_opens_fallback_issue(github_config):
    report = _experiment_1_report()
    h = fl.verdict_hash(report)
    comments = [
        {"body": f"/approve {h}", "user": {"login": "maintainer"}, "created_at": "2026-11-26T15:00:00Z"}
    ]

    def fake_get(url, **kwargs):
        if url.endswith("/comments"):
            return FakeResponse(200, comments)
        return FakeResponse(200, {"permission": "write"})

    with patch.object(fl, "current_gate", return_value=report), \
         patch.object(fl.requests, "get", side_effect=fake_get), \
         patch.object(fl, "set_flag", return_value=(500, "boom")), \
         patch.object(fl, "_create_issue") as create_issue:
        result = fl.approve_and_pause(h)

    assert result.startswith("PAUSE FAILED")
    create_issue.assert_called_once()
    title, body = create_issue.call_args.args
    assert title == "Pause checkout_button: manual action needed"
    assert "Experiment Doctor verdict: DO NOT SHIP" in body
    assert "HTTP 500: boom" in body


def test_verdict_hash_ignores_as_of_but_tracks_check_result():
    import flagsmith_live as fl_mod

    base = _experiment_1_report()
    h = fl_mod.verdict_hash(base)

    refreshed = copy.deepcopy(base)
    refreshed["as_of"] = "2099-01-01T00:00:00Z"
    assert fl_mod.verdict_hash(refreshed) == h

    changed = copy.deepcopy(base)
    name, _, reason = changed["checks"][0]
    changed["checks"][0] = (name, "PASS", reason)
    assert fl_mod.verdict_hash(changed) != h

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
    card = render_card(report, "Explanation text.", "test/model:free")
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


def test_check_approval_normalises_whitespace_and_checks_hash(github_config):
    h = fl.verdict_hash(_experiment_1_report())
    other = "0" * 64

    def run(body):
        comments = [{"body": body, "user": {"login": "maintainer"}, "created_at": "2026-11-26T15:00:00Z"}]

        def fake_get(url, **kwargs):
            if url.endswith("/comments"):
                return FakeResponse(200, comments)
            return FakeResponse(200, {"permission": "write"})

        with patch.object(fl.requests, "get", side_effect=fake_get):
            return fl.check_approval(h)

    assert run(f"/approve\n{h}") is True
    assert run(f"/approve {other}") is False


# ---- OpenRouter explanation step (all HTTP mocked) ----

OR_ENV_KEY = "test-openrouter-key"
OR_MODEL = "test/model:free"


OR_BACKUP = "test/backup:free"


@pytest.fixture
def openrouter_config(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_ENV_KEY)
    monkeypatch.setenv("OPENROUTER_MODEL", OR_MODEL)
    monkeypatch.setenv("OPENROUTER_BACKUP_MODEL", OR_BACKUP)


def _route_by_model(responses_by_model):
    """Fake requests.post that answers from a per-model queue of responses."""
    def fake_post(url, json, **kwargs):
        return responses_by_model[json["model"]].pop(0)
    return fake_post


def _or_response(text):
    return FakeResponse(200, {"choices": [{"message": {"content": text}}]})


VALID_TEXT = "Experiment 1 is running. The split check failed with chi-square 27.71 and p 1.41e-07."
INVENTED_TEXT = "Experiment 1 is running. The lift was 12.34 percent."


def test_missing_openrouter_key_stops_with_clear_message(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    monkeypatch.setenv("OPENROUTER_MODEL", OR_MODEL)
    with patch.object(fl.requests, "post") as post:
        with pytest.raises(fl.ConfigError, match="OPENROUTER_API_KEY is empty"):
            fl.explain_with_guard(_experiment_1_report())
    post.assert_not_called()


def test_model_without_free_suffix_stops_with_clear_message(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_ENV_KEY)
    monkeypatch.setenv("OPENROUTER_MODEL", "vendor/paid-model")
    with patch.object(fl.requests, "post") as post:
        with pytest.raises(fl.ConfigError, match=":free"):
            fl.explain_with_guard(_experiment_1_report())
    post.assert_not_called()


def test_invented_number_rejected_then_retry_succeeds(openrouter_config):
    with patch.object(fl.requests, "post", side_effect=[
        _or_response(INVENTED_TEXT),
        _or_response(VALID_TEXT),
    ]) as post:
        text, model = fl.explain_with_guard(_experiment_1_report())
    assert post.call_count == 2
    assert text == VALID_TEXT
    assert model == OR_MODEL


def test_two_failures_stop_before_posting(openrouter_config):
    report = _experiment_1_report()
    queues = {
        OR_MODEL: [_or_response(INVENTED_TEXT), _or_response(INVENTED_TEXT)],
        OR_BACKUP: [_or_response(INVENTED_TEXT), _or_response(INVENTED_TEXT)],
    }
    with patch.object(fl.requests, "post", side_effect=_route_by_model(queues)), \
         patch.object(fl, "post_card") as post_card:
        with pytest.raises(fl.ExplanationUnavailable) as excinfo:
            fl.explain_with_guard(report)
        message = str(excinfo.value)
        assert f"{OR_MODEL}: attempt 1 rejected" in message
        assert f"{OR_BACKUP}: attempt 1 rejected" in message
        assert message.count("attempt 2 rejected") == 2
        # The card cannot be rendered without an explanation, so nothing is posted.
        with pytest.raises(fl.ExplanationUnavailable):
            fl.render_card(report)
    post_card.assert_not_called()


def test_valid_explanation_appears_in_card_with_model_name(openrouter_config):
    report = _experiment_1_report()
    with patch.object(fl.requests, "post", return_value=_or_response(VALID_TEXT)):
        text, model = fl.explain_with_guard(report)
    card = fl.render_card(report, text, model)
    assert "### Explanation" in card
    assert VALID_TEXT in card
    assert f"_Model used: {OR_MODEL}_" in card


def test_number_guard_exempts_100_and_50_only():
    report = _experiment_1_report()
    assert fl.number_guard("Rollout is 100 percent at 50 percent.", report) == (True, None)
    assert fl.number_guard("Lift was 12.34.", report) == (False, "12.34")


def test_prompt_includes_copy_exactly_instruction(openrouter_config):
    with patch.object(fl.requests, "post", return_value=_or_response(VALID_TEXT)) as post:
        fl.explain_llm(_experiment_1_report())
    messages = post.call_args.kwargs["json"]["messages"]
    system = next(m["content"] for m in messages if m["role"] == "system")
    assert "Copy every number exactly as it appears in the facts" in system
    assert "no rounding" in system
    assert "no thousands separators" in system
    assert "no converting between percentages and decimals" in system


def test_card_shows_conversion_rates_rounded_to_three_decimals():
    import re as _re

    report = _experiment_1_report()
    card = fl.render_card(report, "Explanation text.", "test/model:free")
    assert "9.994%" in card
    assert "8.888%" in card
    # Every percentage in the card has at most 3 decimal places.
    for value in _re.findall(r"\d+\.(\d+)%", card):
        assert len(value) <= 3, value


def test_primary_succeeds_backup_not_called(openrouter_config):
    queues = {OR_MODEL: [_or_response(VALID_TEXT)], OR_BACKUP: []}
    with patch.object(fl.requests, "post", side_effect=_route_by_model(queues)) as post:
        text, model = fl.explain_with_guard(_experiment_1_report())
    assert model == OR_MODEL
    assert post.call_count == 1
    assert all(c.kwargs["json"]["model"] == OR_MODEL for c in post.call_args_list)


def test_primary_fails_backup_succeeds_card_shows_backup(openrouter_config):
    report = _experiment_1_report()
    queues = {
        OR_MODEL: [_or_response(INVENTED_TEXT), _or_response(INVENTED_TEXT)],
        OR_BACKUP: [_or_response(VALID_TEXT)],
    }
    with patch.object(fl.requests, "post", side_effect=_route_by_model(queues)):
        text, model = fl.explain_with_guard(report)
    assert model == OR_BACKUP
    card = fl.render_card(report, text, model)
    assert f"_Model used: {OR_BACKUP}_" in card
    assert f"_Model used: {OR_MODEL}_" not in card


def test_backup_model_empty_stops_with_clear_message(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_ENV_KEY)
    monkeypatch.setenv("OPENROUTER_MODEL", OR_MODEL)
    monkeypatch.setenv("OPENROUTER_BACKUP_MODEL", "")
    with patch.object(fl.requests, "post") as post:
        with pytest.raises(fl.ConfigError, match="OPENROUTER_BACKUP_MODEL is empty"):
            fl.explain_with_guard(_experiment_1_report())
    post.assert_not_called()


def test_backup_model_without_free_stops_with_clear_message(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_ENV_KEY)
    monkeypatch.setenv("OPENROUTER_MODEL", OR_MODEL)
    monkeypatch.setenv("OPENROUTER_BACKUP_MODEL", "vendor/paid-backup")
    with patch.object(fl.requests, "post") as post:
        with pytest.raises(fl.ConfigError, match="OPENROUTER_BACKUP_MODEL must end in ':free'"):
            fl.explain_with_guard(_experiment_1_report())
    post.assert_not_called()


def test_backup_equal_to_primary_stops_with_clear_message(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", OR_ENV_KEY)
    monkeypatch.setenv("OPENROUTER_MODEL", OR_MODEL)
    monkeypatch.setenv("OPENROUTER_BACKUP_MODEL", OR_MODEL)
    with patch.object(fl.requests, "post") as post:
        with pytest.raises(fl.ConfigError, match="equals OPENROUTER_MODEL"):
            fl.explain_with_guard(_experiment_1_report())
    post.assert_not_called()

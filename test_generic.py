"""Generic workflow tests: a different flag, control name, project, environment, experiment, repo and
issue. Everything comes from configuration set in these tests; no demo value is used in the code.
All HTTP calls are mocked. The seen log is a temporary file.
"""

import json
from unittest.mock import MagicMock, patch

import pytest

import agent
import checkout_app
import flagsmith_live as fl

FLAG = "example_flag"
CONTROL = "baseline"
TREATMENT = "variant_b"
EXPERIMENT_ID = 42
ENV_ID = 7
PROJECT_ID = 9
FEATURE_ID = 5
OPTION_ID = 8
STARTED = "2026-05-01T00:00:00Z"


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
def generic_config(monkeypatch, tmp_path):
    monkeypatch.setattr(fl, "FLAGSMITH_URL", "https://flags.example.test")
    monkeypatch.setattr(fl, "FLAGSMITH_TOKEN", "test-token")
    monkeypatch.setattr(fl, "FLAGSMITH_PROJECT_ID", str(PROJECT_ID))
    monkeypatch.setattr(fl, "FLAGSMITH_ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setattr(fl, "FLAGSMITH_FLAG", FLAG)
    monkeypatch.setattr(fl, "FLAGSMITH_CONTROL_VARIANT", CONTROL)
    monkeypatch.setattr(fl, "GITHUB_TOKEN", "test-github-token")
    monkeypatch.setattr(fl, "GITHUB_REPO", "acme/checkout-tests")
    monkeypatch.setattr(fl, "GITHUB_ISSUE_NUMBER", "23")
    seen = tmp_path / "seen.jsonl"
    monkeypatch.setattr(fl, "SEEN_LOG_PATH", str(seen))
    monkeypatch.setattr(checkout_app, "SEEN_PATH", str(seen))
    monkeypatch.setenv("FLAGSMITH_FLAG", FLAG)
    monkeypatch.setenv("FLAGSMITH_CONTROL_VARIANT", CONTROL)
    return seen


def _detail():
    return {
        "id": EXPERIMENT_ID,
        "status": "stopped",
        "started_at": STARTED,
        "feature": {"id": FEATURE_ID, "name": FLAG},
        "experiment_rollout": {
            "enabled": True,
            "rollout_percentage": 100.0,
            "multivariate_feature_state_values": [
                {"multivariate_feature_option": OPTION_ID, "percentage_allocation": 50.0}
            ],
        },
    }


def _results(control_n=1000, treatment_n=1000):
    return {
        "results": {
            "as_of": "2026-06-01T00:00:00Z",
            "is_final": True,
            "payload": {
                "metrics": [
                    {
                        "variants": {
                            CONTROL: {"n": control_n, "sum": 100.0, "sum_squares": 100.0},
                            TREATMENT: {"n": treatment_n, "sum": 120.0, "sum_squares": 120.0},
                        },
                        "conversions_timeseries": {"points": []},
                    }
                ],
                "exposures_timeseries": {"points": []},
            },
        }
    }


def _route(detail, results, api_key="generic-env-key"):
    def fake_get(url, **kwargs):
        if url.endswith("/api/v1/environments/"):
            return FakeResponse(200, [{"id": ENV_ID, "api_key": api_key}])
        if f"/experiments/{EXPERIMENT_ID}/results/" in url:
            return FakeResponse(200, results)
        if f"/experiments/{EXPERIMENT_ID}/" in url:
            return FakeResponse(200, detail)
        if "/featurestates/" in url:
            return FakeResponse(200, {"results": [{
                "feature": FEATURE_ID,
                "multivariate_feature_state_values": [
                    {"multivariate_feature_option": OPTION_ID, "percentage_allocation": 50.0}
                ],
            }]})
        raise AssertionError(f"unexpected GET {url}")
    return fake_get


def _write_seen(path, control, treatment, pre_start=True):
    rows = []
    if pre_start:
        rows.append({"user_id": "before", "variant": CONTROL, "ts": "2026-04-30T23:00:00Z"})
    rows += [{"user_id": f"c{i}", "variant": CONTROL, "ts": "2026-05-02T00:00:00Z"} for i in range(control)]
    rows += [{"user_id": f"t{i}", "variant": TREATMENT, "ts": "2026-05-02T00:00:00Z"} for i in range(treatment)]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


def test_generic_gate_and_card_with_example_flag_ship(generic_config):
    _write_seen(generic_config, control=1000, treatment=1000)
    with patch.object(fl.requests, "get", side_effect=_route(_detail(), _results())) as get:
        report = fl.current_gate(EXPERIMENT_ID)
    urls = [c.args[0] for c in get.call_args_list]
    assert any(f"/experiments/{EXPERIMENT_ID}/results/" in u for u in urls)
    assert report["gate"] == "SHIP"
    checks = {name: res for name, res, _ in report["checks"]}
    assert checks == {"split (chi-square)": "PASS", "rollout vs base": "PASS", "seen": "PASS"}

    card = fl.render_card(report, "Explanation text.", "model/x:free")
    assert card.splitlines()[0] == "## Experiment Doctor verdict: SHIP"
    assert f"To pause {FLAG}, comment /approve {fl.verdict_hash(report)} on this issue." in card
    assert f"- {CONTROL}:" in card and f"- {TREATMENT}:" in card
    for leftover in ("checkout_button", "new_checkout", "V_2"):
        assert leftover not in card


def test_generic_seen_fail_with_covering_log_is_do_not_ship(generic_config):
    _write_seen(generic_config, control=400, treatment=1000)
    with patch.object(fl.requests, "get", side_effect=_route(_detail(), _results())):
        report = fl.current_gate(EXPERIMENT_ID)
    checks = {name: (res, reason) for name, res, reason in report["checks"]}
    assert checks["seen"][0] == "FAIL"
    assert f"{CONTROL}: seen 400 < counted 1000" in checks["seen"][1]
    assert report["gate"] == "DO NOT SHIP"


def test_generic_seen_not_checked_when_log_starts_after_start(generic_config):
    _write_seen(generic_config, control=1000, treatment=1000, pre_start=False)
    with patch.object(fl.requests, "get", side_effect=_route(_detail(), _results())):
        report = fl.current_gate(EXPERIMENT_ID)
    checks = {name: (res, reason) for name, res, reason in report["checks"]}
    assert checks["seen"] == ("NOT_CHECKED", "seen log starts after the experiment started")
    assert report["gate"] == "NEEDS A LOOK"


def test_experiment_id_is_an_argument_not_a_constant(generic_config):
    _write_seen(generic_config, control=1000, treatment=1000)
    with patch.object(fl.requests, "get", side_effect=_route(_detail(), _results())) as get:
        fl.current_gate(EXPERIMENT_ID)
    urls = [c.args[0] for c in get.call_args_list]
    assert all("/experiments/42/" in u for u in urls if "/experiments/" in u)


def test_fallback_title_is_built_from_the_flag_name(generic_config):
    assert fl.pause_fallback_title() == f"Pause {FLAG}: manual action needed"


def test_approve_and_pause_fallback_issue_uses_flag_name(generic_config):
    _write_seen(generic_config, control=1000, treatment=1000)
    with patch.object(fl.requests, "get", side_effect=_route(_detail(), _results())):
        report = fl.current_gate(EXPERIMENT_ID)
    h = fl.verdict_hash(report)
    comments = [{"body": f"/approve {h}", "user": {"login": "owner"}, "created_at": "2026-05-02T00:00:00Z"}]

    def fake_get(url, **kwargs):
        if url.endswith("/comments"):
            return FakeResponse(200, comments)
        if "/collaborators/" in url:
            return FakeResponse(200, {"permission": "admin"})
        return _route(_detail(), _results())(url, **kwargs)

    with patch.object(fl, "current_gate", return_value=report), \
         patch.object(fl.requests, "get", side_effect=fake_get), \
         patch.object(fl, "set_flag", return_value=(500, "down")), \
         patch.object(fl, "_create_issue") as create_issue:
        outcome = fl.approve_and_pause(h, EXPERIMENT_ID)
    assert outcome.startswith("PAUSE FAILED")
    title, body = create_issue.call_args.args
    assert title == f"Pause {FLAG}: manual action needed"
    assert f"To pause {FLAG}" in body


def test_agent_pause_uses_the_experiment_matched_by_hash(generic_config):
    report_hash = "a" * 64
    state = {"handled": [], "posted_hashes": {str(EXPERIMENT_ID): report_hash}, "bootstrapped": True}
    with patch.object(fl, "approve_and_pause", return_value="PAUSED") as approve:
        outcome = agent.step_pause(FLAG, report_hash, state)
    assert outcome == "PAUSED"
    approve.assert_called_once_with(report_hash, EXPERIMENT_ID)


def test_agent_pause_refuses_other_flag_names(generic_config):
    state = {"handled": [], "posted_hashes": {str(EXPERIMENT_ID): "a" * 64}, "bootstrapped": True}
    with patch.object(fl, "approve_and_pause") as approve:
        outcome = agent.step_pause("checkout_button", "a" * 64, state)
    assert outcome.startswith("REFUSED")
    approve.assert_not_called()


def test_checkout_app_records_configured_flag_and_any_returned_value(generic_config):
    from fastapi.testclient import TestClient

    client = MagicMock()
    client.get_identity_flags.return_value.get_feature_value.return_value = TREATMENT
    with patch.object(checkout_app, "get_client", return_value=client):
        resp = TestClient(checkout_app.app).get("/checkout", params={"user_id": "u1"})
    assert resp.status_code == 200
    assert f"<strong>{TREATMENT}</strong>" in resp.text
    client.get_identity_flags.return_value.get_feature_value.assert_called_once_with(FLAG)
    record = json.loads(generic_config.read_text(encoding="utf-8").splitlines()[0])
    assert record["variant"] == TREATMENT

    client.get_identity_flags.return_value.get_feature_value.return_value = None
    with patch.object(checkout_app, "get_client", return_value=client):
        TestClient(checkout_app.app).get("/checkout", params={"user_id": "u2"})
    last = json.loads(generic_config.read_text(encoding="utf-8").splitlines()[-1])
    assert last["variant"] == CONTROL


def test_missing_flag_name_fails_with_clear_message(monkeypatch):
    monkeypatch.delenv("FLAGSMITH_FLAG", raising=False)
    with pytest.raises(fl.ConfigError, match="FLAGSMITH_FLAG is not set in .env"):
        fl._required("FLAGSMITH_FLAG")
    with pytest.raises(RuntimeError, match="FLAGSMITH_FLAG is not set in .env"):
        checkout_app.flag_name()

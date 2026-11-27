import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import checkout_app


@pytest.fixture
def seen_file(tmp_path, monkeypatch):
    path = tmp_path / "seen.jsonl"
    monkeypatch.setattr(checkout_app, "SEEN_PATH", str(path))
    monkeypatch.setenv("FLAGSMITH_FLAG", "checkout_button")
    return path


def _mock_client(value):
    client = MagicMock()
    client.get_identity_flags.return_value.get_feature_value.return_value = value
    return client


def test_checkout_writes_exactly_one_line_with_the_variant(seen_file):
    client = _mock_client("new_checkout")
    with patch.object(checkout_app, "get_client", return_value=client):
        resp = TestClient(checkout_app.app).get("/checkout", params={"user_id": "test1"})
    assert resp.status_code == 200
    lines = seen_file.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    assert record["user_id"] == "test1"
    assert record["variant"] == "new_checkout"
    assert record["ts"]
    client.get_identity_flags.assert_called_once_with("test1")


def test_checkout_page_shows_the_variant(seen_file):
    client = _mock_client("new_checkout")
    with patch.object(checkout_app, "get_client", return_value=client):
        resp = TestClient(checkout_app.app).get("/checkout", params={"user_id": "test1"})
    assert resp.headers["content-type"].startswith("text/html")
    assert "You were shown the <strong>new_checkout</strong> checkout." in resp.text


def test_empty_flag_value_is_recorded_as_control(seen_file):
    client = _mock_client(None)
    with patch.object(checkout_app, "get_client", return_value=client):
        TestClient(checkout_app.app).get("/checkout", params={"user_id": "test2"})
    record = json.loads(seen_file.read_text(encoding="utf-8").splitlines()[0])
    assert record["variant"] == "control"


def test_seen_log_counts_per_variant_without_user_ids(seen_file):
    seen_file.write_text(
        "\n".join(
            json.dumps(r)
            for r in [
                {"user_id": "a", "variant": "new_checkout", "ts": "t"},
                {"user_id": "b", "variant": "new_checkout", "ts": "t"},
                {"user_id": "c", "variant": "control", "ts": "t"},
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    resp = TestClient(checkout_app.app).get("/seen-log")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {"lines": 3, "per_variant": {"new_checkout": 2, "control": 1}}
    assert "user_id" not in resp.text
    for uid in ("a", "b", "c"):
        assert f'"{uid}"' not in resp.text


def test_seen_log_empty_when_no_file(seen_file):
    resp = TestClient(checkout_app.app).get("/seen-log")
    assert resp.json() == {"lines": 0, "per_variant": {}}

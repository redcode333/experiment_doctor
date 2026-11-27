"""Minimal live client for a self-hosted Flagsmith instance.

Config is read from .env (never hardcoded). Requires python-dotenv and requests.
"""

import hashlib
import json
import os
import re
from datetime import datetime

import requests
from dotenv import load_dotenv

# .env is the config source; override=True so it wins over any stale OS-level variable.
load_dotenv(override=True)

class ConfigError(RuntimeError):
    """A required setting is missing or invalid. The step that needs it cannot run."""


def pause_fallback_title() -> str:
    """Title of the issue opened when a pause fails. Built from the configured flag name."""
    return f"Pause {FLAGSMITH_FLAG}: manual action needed"


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set in .env; set it before running")
    return value


GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_ISSUE_NUMBER = os.environ.get("GITHUB_ISSUE_NUMBER", "")

FLAGSMITH_URL = _required("FLAGSMITH_URL").rstrip("/")
FLAGSMITH_TOKEN = _required("FLAGSMITH_TOKEN")
FLAGSMITH_PROJECT_ID = _required("FLAGSMITH_PROJECT_ID")
FLAGSMITH_ENVIRONMENT_ID = int(_required("FLAGSMITH_ENVIRONMENT_ID"))
FLAGSMITH_FLAG = _required("FLAGSMITH_FLAG")
FLAGSMITH_CONTROL_VARIANT = os.environ.get("FLAGSMITH_CONTROL_VARIANT", "").strip() or "control"

HEADERS = {"Authorization": f"Api-Key {FLAGSMITH_TOKEN}"}
TIMEOUT = 15


def _results(payload):
    """Management list endpoints return {"results": [...]} (paginated)."""
    if isinstance(payload, list):
        return payload
    return payload.get("results", [])


def get_env_api_key() -> str:
    """Return the api_key of the environment whose id is FLAGSMITH_ENVIRONMENT_ID."""
    resp = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/",
        params={"project": FLAGSMITH_PROJECT_ID},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    for env in _results(resp.json()):
        if env["id"] == FLAGSMITH_ENVIRONMENT_ID:
            return env["api_key"]
    raise LookupError(
        f"Environment id {FLAGSMITH_ENVIRONMENT_ID} not found in project {FLAGSMITH_PROJECT_ID}"
    )


def _get_feature_id(flag_name: str) -> int:
    resp = requests.get(
        f"{FLAGSMITH_URL}/api/v1/projects/{FLAGSMITH_PROJECT_ID}/features/",
        params={"page_size": 100},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    for feature in _results(resp.json()):
        if feature["name"] == flag_name:
            return feature["id"]
    raise LookupError(f"Flag {flag_name!r} not found in project {FLAGSMITH_PROJECT_ID}")


def _get_feature_state_id(api_key: str, feature_id: int) -> int:
    resp = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/featurestates/",
        params={"page_size": 100},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    for fs in _results(resp.json()):
        if fs["feature"] == feature_id:
            return fs["id"]
    raise LookupError(f"No feature state for feature id {feature_id} in this environment")


def set_flag(enabled: bool):
    """Set FLAGSMITH_FLAG enabled/disabled in the configured environment.

    Returns (status_code, body) where body is the parsed JSON response, or raw text.
    """
    api_key = get_env_api_key()
    feature_id = _get_feature_id(FLAGSMITH_FLAG)
    state_id = _get_feature_state_id(api_key, feature_id)
    resp = requests.patch(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/featurestates/{state_id}/",
        json={"enabled": enabled},
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    try:
        body = resp.json()
    except ValueError:
        body = resp.text
    return resp.status_code, body


def get_experiments():
    """Fetch experiments for the configured environment and print first-item key names only."""
    api_key = get_env_api_key()
    resp = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/experiments/",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    payload = resp.json()
    print("top-level keys:", sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__)
    items = _results(payload)
    if items:
        print("first item keys:", sorted(items[0].keys()))
    else:
        print("first item keys: (no experiments returned)")
    return payload


def get_experiment_detail(experiment_id: int):
    """Fetch one experiment. Prints top-level key names; for 'metrics', its first item's key names. No values."""
    api_key = get_env_api_key()
    resp = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/experiments/{experiment_id}/",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    print("detail status:", resp.status_code)
    resp.raise_for_status()
    payload = resp.json()
    print("detail top-level keys:", sorted(payload.keys()))
    metrics = payload.get("metrics")
    if isinstance(metrics, list) and metrics:
        print("detail metrics[0] keys:", sorted(metrics[0].keys()))
    else:
        print("detail metrics[0] keys: (no metrics)")
    return payload


def get_experiment_results(experiment_id: int):
    """Fetch results for one experiment. Prints status code and key names only."""
    api_key = get_env_api_key()
    resp = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/experiments/{experiment_id}/results/",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    print("results status:", resp.status_code)
    if resp.status_code == 200:
        payload = resp.json()
        if isinstance(payload, dict):
            print("results top-level keys:", sorted(payload.keys()))
        else:
            print("results payload type:", type(payload).__name__)
    else:
        print("results body keys: (not JSON or error response; no values printed)")
    return resp.status_code


PASS = "PASS"
NOT_CHECKED = "NOT_CHECKED"


def check_rollout_vs_base(experiment_rollout, base_weights):
    """Compare planned experiment allocations with the environment's base allocations.

    experiment_rollout: the experiment's experiment_rollout dict.
    base_weights: dict mapping multivariate option id -> base percentage_allocation.

    Returns (status, reason). A disagreement is NOT_CHECKED, not FAIL, because which
    allocation applies at runtime has not been verified.
    """
    rollout_values = (experiment_rollout or {}).get("multivariate_feature_state_values") or []
    for planned in rollout_values:
        option_id = planned.get("multivariate_feature_option")
        planned_pct = planned.get("percentage_allocation")
        base_pct = base_weights.get(option_id)
        if planned_pct is None or base_pct is None:
            return NOT_CHECKED, "missing allocation"
        if planned_pct != base_pct:
            return NOT_CHECKED, (
                f"rollout says {planned_pct}%, base says {base_pct}%; "
                "which one applies is unverified"
            )
    if not rollout_values:
        return NOT_CHECKED, "missing allocation"
    return PASS, None


SHIP = "SHIP"
DO_NOT_SHIP = "DO NOT SHIP"
NEEDS_A_LOOK = "NEEDS A LOOK"
SRM_CRITICAL = 10.83  # chi-square, 1 df, p = 0.001


def get_base_weights(feature_id: int) -> dict:
    """Option id -> percentage_allocation for the environment's feature state of this flag."""
    api_key = get_env_api_key()
    rows = _results(
        requests.get(
            f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/featurestates/",
            params={"page_size": 100},
            headers=HEADERS,
            timeout=TIMEOUT,
        ).json()
    )
    fs = [r for r in rows if r["feature"] == feature_id][0]
    return {
        mv["multivariate_feature_option"]: mv["percentage_allocation"]
        for mv in fs.get("multivariate_feature_state_values", [])
    }


SEEN_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.jsonl")


def _parse_ts(value: str) -> datetime:
    """ISO timestamp to an aware datetime. Accepts a trailing 'Z'."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def load_seen_records(path: str = SEEN_LOG_PATH):
    """Records from seen.jsonl as dicts {variant, ts (datetime)}, or None if missing or empty."""
    if not os.path.exists(path):
        return None
    records = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                row = json.loads(raw)
                records.append({"variant": row["variant"], "ts": _parse_ts(row["ts"])})
    return records or None


def check_seen(counted: dict, records, started_at):
    """Seen check: every counted user needs a matching seen record, inside the experiment window.

    counted: experiment variant name -> counted users, e.g. {<control>: 1924, <treatment>: 1611}.
    records: seen records from load_seen_records (dicts with variant and ts).
    started_at: datetime the experiment started, or None if unknown.
    Roles: FLAGSMITH_CONTROL_VARIANT matches the control variant; every other seen value is the treatment.

    NOT_CHECKED if there are no records, the start time is unknown, or the log starts after
    the experiment started (the log does not cover the window). FAIL only when the log covers
    the window and seen counts inside it are below the counted users. Otherwise PASS.
    Returns (PASS | FAIL | NOT_CHECKED, reason).
    """
    if not records:
        return NOT_CHECKED, "seen.jsonl missing or empty"
    if started_at is None:
        return NOT_CHECKED, "experiment start time unknown"
    if min(r["ts"] for r in records) > started_at:
        return NOT_CHECKED, "seen log starts after the experiment started"
    control = FLAGSMITH_CONTROL_VARIANT
    treatments = [name for name in counted if name != control]
    if len(treatments) != 1:
        return NOT_CHECKED, f"cannot map variants to seen values: {sorted(counted)}"
    in_window = [r for r in records if r["ts"] >= started_at]
    seen_control = sum(1 for r in in_window if r["variant"] == control)
    seen_treatment = sum(1 for r in in_window if r["variant"] != control)
    lows, details = [], []
    for name, n in counted.items():
        seen_n = seen_control if name == control else seen_treatment
        details.append(f"{name}: seen {seen_n}, counted {n}")
        if seen_n < n:
            lows.append(f"{name}: seen {seen_n} < counted {n}")
    if lows:
        return "FAIL", "; ".join(lows)
    return PASS, "; ".join(details)


def evaluate_gate(detail: dict, results: dict, base_weights: dict, seen_records=None):
    """Pure gate logic. detail = experiment detail JSON, results = results endpoint JSON.

    seen_records: records from load_seen_records (None means the seen log is missing or empty).
    The experiment start time comes from detail["started_at"].
    """
    from scipy.stats import chisquare

    r = results["results"]
    payload = r["payload"]
    metric0 = payload["metrics"][0]
    variants = metric0["variants"]
    rollout = detail["experiment_rollout"]

    # Planned shares: listed allocations; control takes the remainder (assumption, see report).
    planned_pct = {}
    for mv in rollout.get("multivariate_feature_state_values", []):
        planned_pct[mv["multivariate_feature_option"]] = mv["percentage_allocation"]
    listed_total = sum(planned_pct.values())
    observed, expected_share, labels = [], [], []
    for name, v in variants.items():
        labels.append(name)
        observed.append(v["n"])
        if name == FLAGSMITH_CONTROL_VARIANT:
            expected_share.append((100.0 - listed_total) / 100.0)
        else:
            # Only one non-control allocation is supported: the single listed rollout entry.
            if len(planned_pct) != 1:
                raise ValueError("expected exactly one listed non-control allocation")
            expected_share.append(next(iter(planned_pct.values())) / 100.0)
    total_n = sum(observed)
    expected = [s * total_n for s in expected_share]
    stat, p = chisquare(f_obs=observed, f_exp=expected)
    srm_result = PASS if stat < SRM_CRITICAL else "FAIL"
    srm_reason = f"chi-square={stat:.2f}, p={p:.3g} (variants {labels}, n {observed}, expected {[round(e, 1) for e in expected]})"

    rollout_result, rollout_reason = check_rollout_vs_base(rollout, base_weights)

    counted = {name: v["n"] for name, v in variants.items()}
    started = detail.get("started_at")
    started_at = _parse_ts(started) if started else None
    seen_result, seen_reason = check_seen(counted, seen_records, started_at)

    checks = [
        ("split (chi-square)", srm_result, srm_reason),
        ("rollout vs base", rollout_result, rollout_reason or ""),
        ("seen", seen_result, seen_reason),
    ]
    status = detail["status"]
    if any(res == "FAIL" for _, res, _ in checks):
        gate = DO_NOT_SHIP
    elif any(res == NOT_CHECKED for _, res, _ in checks) or status != "stopped":
        gate = NEEDS_A_LOOK
    else:
        gate = SHIP

    conversions = metric0["conversions_timeseries"]
    rates = {
        name: (v["sum"] / v["n"] if v["n"] else None) for name, v in variants.items()
    }
    return {
        "experiment_id": detail["id"],
        "status": status,
        "is_final": r["is_final"],
        "as_of": r["as_of"],
        "checks": checks,
        "conversion_rate": rates,
        "gate": gate,
    }


def current_gate(experiment_id: int) -> dict:
    """Read detail and results and evaluate the gate. Read-only, prints nothing."""
    api_key = get_env_api_key()
    detail = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/experiments/{experiment_id}/",
        headers=HEADERS, timeout=TIMEOUT,
    ).json()
    results = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/experiments/{experiment_id}/results/",
        headers=HEADERS, timeout=TIMEOUT,
    ).json()
    base_weights = get_base_weights(detail["feature"]["id"])
    return evaluate_gate(detail, results, base_weights, load_seen_records(SEEN_LOG_PATH))


def run_gate(experiment_id: int):
    """Evaluate the gate for an experiment and print the report. Read-only."""
    report = current_gate(experiment_id)

    print("experiment id:", report["experiment_id"])
    print("status:", report["status"])
    print("is_final:", report["is_final"])
    print("as_of:", report["as_of"])
    for name, res, reason in report["checks"]:
        print(f"check [{name}]: {res}" + (f" - {reason}" if reason else ""))
    for name, rate in report["conversion_rate"].items():
        print(f"conversion rate [{name}]: {rate:.4%}" if rate is not None else f"conversion rate [{name}]: n/a")
    print("GATE:", report["gate"])
    return report


def _gh_headers() -> dict:
    return {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}


def _issue_comments_url() -> str:
    return f"{GITHUB_API}/repos/{GITHUB_REPO}/issues/{GITHUB_ISSUE_NUMBER}/comments"


def post_card(card_md: str) -> int:
    """Post the verdict card as a comment on the configured issue. Returns the comment id."""
    resp = requests.post(
        _issue_comments_url(),
        json={"body": card_md},
        headers=_gh_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["id"]


def _post_comment(body: str) -> int:
    return post_card(body)


def verdict_hash(gate_result: dict) -> str:
    """SHA-256 over the verdict only: experiment id, gate, and each check's name, result and reason.

    as_of, status, is_final and conversion rates are deliberately excluded, so the approval
    code does not change when results refresh without changing the verdict.
    """
    covered = {
        "experiment_id": gate_result["experiment_id"],
        "gate": gate_result["gate"],
        "checks": [
            {"name": name, "result": result, "reason": reason}
            for name, result, reason in gate_result["checks"]
        ],
    }
    blob = json.dumps(covered, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _has_write_access(login: str) -> bool:
    resp = requests.get(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/collaborators/{login}/permission",
        headers=_gh_headers(),
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        return False
    return resp.json().get("permission") in ("admin", "write")


def check_approval(approval_hash: str) -> bool:
    """True only if a comment with body exactly '/approve <hash>' exists from an author with write access.

    Checks matching comments newest first. Returns True only if the matching comment's author
    has admin or write permission on the repo.
    """
    resp = requests.get(
        _issue_comments_url(),
        params={"per_page": 100},
        headers=_gh_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    expected = f"/approve {approval_hash}"
    comments = sorted(resp.json(), key=lambda c: c["created_at"], reverse=True)
    for comment in comments:
        # Collapse all whitespace runs (including newlines) to one space, then trim.
        if " ".join(comment["body"].split()) != expected:
            continue
        if _has_write_access(comment["user"]["login"]):
            return True
    return False


def _create_issue(title: str, body: str) -> int:
    resp = requests.post(
        f"{GITHUB_API}/repos/{GITHUB_REPO}/issues",
        json={"title": title, "body": body},
        headers=_gh_headers(),
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["number"]


def approve_and_pause(posted_hash: str, experiment_id) -> str:
    """Pause FLAGSMITH_FLAG only if the verdict is unchanged and an authorised approval exists.

    Returns a short status string: 'PAUSED', 'REFUSED: ...', or 'PAUSE FAILED: ...'.
    """
    report = current_gate(int(experiment_id))
    new_hash = verdict_hash(report)
    if new_hash != posted_hash:
        _post_comment(
            f"Verdict changed since it was posted (new hash {new_hash}). "
            f"Refusing to pause {FLAGSMITH_FLAG}. Re-run the gate and post a new card."
        )
        return "REFUSED: verdict changed"
    if not check_approval(posted_hash):
        return "REFUSED: no approval from a user with write access"
    try:
        status, body = set_flag(False)
        error = None if 200 <= status < 300 else f"HTTP {status}: {body}"
    except Exception as exc:  # noqa: BLE001 - any failure opens the fallback issue
        error = f"{type(exc).__name__}: {exc}"
    if error is None:
        return "PAUSED"
    _create_issue(
        pause_fallback_title(),
        _render_card_body(report) + f"\n\n**set_flag error:**\n\n```\n{error}\n```\n",
    )
    return f"PAUSE FAILED: fallback issue opened ({error})"


def _md_cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


class ExplanationUnavailable(RuntimeError):
    """No valid LLM explanation could be produced. The card must not be posted."""


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
EXPLANATION_SYSTEM_PROMPT = (
    "You explain an A/B experiment verdict using only the JSON facts the user provides. "
    "Do not add any number that does not appear in the facts. "
    "Copy every number exactly as it appears in the facts: no rounding, no thousands separators, "
    "and no converting between percentages and decimals. "
    "Do not recommend changing the verdict, the flag, or the experiment. "
    "Answer in four sentences or fewer."
)
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")
_EXEMPT_NUMBERS = {100.0, 50.0}


def _openrouter_config():
    """Validate all OpenRouter settings. Returns (key, primary_model, backup_model)."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise ConfigError("OPENROUTER_API_KEY is empty in .env; explanation step cannot run")
    primary = os.environ.get("OPENROUTER_MODEL", "").strip()
    if not primary.endswith(":free"):
        raise ConfigError(
            f"OPENROUTER_MODEL must end in ':free' (got {primary!r}); explanation step cannot run"
        )
    backup = os.environ.get("OPENROUTER_BACKUP_MODEL", "").strip()
    if not backup:
        raise ConfigError("OPENROUTER_BACKUP_MODEL is empty in .env; explanation step cannot run")
    if not backup.endswith(":free"):
        raise ConfigError(
            f"OPENROUTER_BACKUP_MODEL must end in ':free' (got {backup!r}); explanation step cannot run"
        )
    if backup == primary:
        raise ConfigError(
            "OPENROUTER_BACKUP_MODEL equals OPENROUTER_MODEL; choose a different backup model"
        )
    return key, primary, backup


def _facts(gate_result: dict) -> dict:
    """The gate result plus per-variant conversion rates in percent (3 dp): the only facts allowed."""
    facts = dict(gate_result)
    facts["conversion_rate_percent"] = {
        name: (round(rate * 100, 3) if rate is not None else None)
        for name, rate in gate_result["conversion_rate"].items()
    }
    return facts


def explain_llm(gate_result: dict, model=None):
    """Ask a free OpenRouter model to explain the gate facts. Returns (text, model). Timeout 30s.

    model defaults to OPENROUTER_MODEL; pass the backup model name to use the backup.
    """
    key, primary, _ = _openrouter_config()
    model = model or primary
    resp = requests.post(
        OPENROUTER_URL,
        json={
            "model": model,
            "messages": [
                {"role": "system", "content": EXPLANATION_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(_facts(gate_result), sort_keys=True, default=str)},
            ],
        },
        headers={"Authorization": f"Bearer {key}"},
        timeout=30,
    )
    resp.raise_for_status()
    text = resp.json()["choices"][0]["message"]["content"].strip()
    return text, model


def number_guard(text: str, gate_result: dict):
    """(True, None) if every number in text appears in the facts (100 and 50 exempt).
    Otherwise (False, offending_number_text)."""
    facts_text = json.dumps(_facts(gate_result), sort_keys=True, default=str)
    allowed = {float(n) for n in _NUMBER_RE.findall(facts_text)} | _EXEMPT_NUMBERS
    for token in _NUMBER_RE.findall(text):
        if float(token) not in allowed:
            return False, token
    return True, None


def explain_with_guard(gate_result: dict):
    """Explanation from the primary model, then the backup model. Returns (text, model_used).

    Each model gets two attempts (one retry) on request failure or number_guard rejection.
    ConfigError propagates before any request is made. If both models fail, raises
    ExplanationUnavailable with the reasons for each model.
    """
    _, primary, backup = _openrouter_config()
    reasons_by_model = {}
    for model in (primary, backup):
        model_reasons = []
        for attempt in (1, 2):
            try:
                text, used = explain_llm(gate_result, model)
            except ConfigError:
                raise
            except Exception as exc:  # network, HTTP status, or response-shape errors
                model_reasons.append(f"attempt {attempt} request failed: {type(exc).__name__}: {exc}")
                continue
            ok, offending = number_guard(text, gate_result)
            if ok:
                return text, used
            model_reasons.append(f"attempt {attempt} rejected: number {offending} is not in the gate facts")
        reasons_by_model[model] = model_reasons
    raise ExplanationUnavailable(
        "; ".join(f"{m}: {' / '.join(r)}" for m, r in reasons_by_model.items())
    )


def render_card(gate_result: dict, explanation=None, model=None) -> str:
    """Render the verdict card. Blocked unless an explanation and its model name are supplied."""
    if not explanation or not model:
        raise ExplanationUnavailable("no explanation supplied, so the card was not rendered")
    return _render_card_body(gate_result, explanation, model)


def _render_card_body(gate_result: dict, explanation=None, model=None) -> str:
    """Render the card body. The Explanation section appears only when an explanation is given."""
    lines = [
        f"## Experiment Doctor verdict: {gate_result['gate']}",
        "",
        f"- **Experiment id:** {gate_result['experiment_id']}",
        f"- **Status:** {gate_result['status']}",
        f"- **is_final:** {gate_result['is_final']}",
        f"- **as_of:** {gate_result['as_of']}",
        "",
        "| Check | Result | Reason |",
        "| --- | --- | --- |",
    ]
    for name, result, reason in gate_result["checks"]:
        lines.append(f"| {_md_cell(name)} | {_md_cell(result)} | {_md_cell(reason or '')} |")
    lines += ["", "**Conversion rate per variant**", ""]
    for name, pct in _facts(gate_result)["conversion_rate_percent"].items():
        value = f"{pct:.3f}%" if pct is not None else "n/a"
        lines.append(f"- {_md_cell(name)}: {value}")
    if explanation is not None:
        lines += ["", "### Explanation", "", explanation, "", f"_Model used: {model}_"]
    lines += [
        "",
        f"To pause {FLAGSMITH_FLAG}, comment /approve {verdict_hash(gate_result)} on this issue. Nothing changes until then.",
        "",
    ]
    return "\n".join(lines)


def _type_name(value) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, dict):
        return "dict"
    if isinstance(value, list):
        return "list"
    return type(value).__name__


def print_results_shape(experiment_id: int, max_depth: int = 3):
    """Print the structure (key names and types only) of the results payload, two levels deep."""
    api_key = get_env_api_key()
    resp = requests.get(
        f"{FLAGSMITH_URL}/api/v1/environments/{api_key}/experiments/{experiment_id}/results/",
        headers=HEADERS,
        timeout=TIMEOUT,
    )
    print("results status:", resp.status_code)
    resp.raise_for_status()
    payload = resp.json()

    def walk(node, depth, indent):
        if not isinstance(node, dict):
            return
        for key, child in node.items():
            print(f"{indent}{key}: {_type_name(child)}")
            if isinstance(child, list):
                if child:
                    first = child[0]
                    print(f"{indent}    [first item]: {_type_name(first)}")
                    if isinstance(first, dict):
                        print(f"{indent}        keys: {sorted(first.keys())}")
                else:
                    print(f"{indent}    [first item]: (empty list)")
            elif isinstance(child, dict) and depth < max_depth:
                walk(child, depth + 1, indent + "    ")

    if isinstance(payload, dict):
        walk(payload, 1, "")
    else:
        print("top-level type:", _type_name(payload))
    return payload


if __name__ == "__main__":
    # Read-only: python flagsmith_live.py <experiment_id>
    # Runs the gate, generates the explanation, prints the card, and writes it to a file.
    # Does not post to GitHub and does not call set_flag or any PATCH.
    import sys

    if len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("usage: python flagsmith_live.py <experiment_id>")
    experiment_id = int(sys.argv[1])
    report = run_gate(experiment_id)
    try:
        explanation, model = explain_with_guard(report)
    except ConfigError as exc:
        print(f"config error: {exc}")
        raise SystemExit(1)
    except ExplanationUnavailable as exc:
        print(f"explanation unavailable: {exc}")
        raise SystemExit(1)
    card = render_card(report, explanation, model)
    print()
    print(card)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"verdict_{experiment_id}.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(card)
    print("written:", out_path)

"""Minimal live client for a self-hosted Flagsmith instance.

Config is read from .env (never hardcoded). Requires python-dotenv and requests.
"""

import hashlib
import json
import os

import requests
from dotenv import load_dotenv

load_dotenv()

GITHUB_API = "https://api.github.com"
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_ISSUE_NUMBER = os.environ.get("GITHUB_ISSUE_NUMBER", "")
FLAGSMITH_EXPERIMENT_ID = os.environ.get("FLAGSMITH_EXPERIMENT_ID", "")
PAUSE_FALLBACK_TITLE = "Pause checkout_button: manual action needed"

FLAGSMITH_URL = os.environ["FLAGSMITH_URL"].rstrip("/")
FLAGSMITH_TOKEN = os.environ["FLAGSMITH_TOKEN"]
FLAGSMITH_PROJECT_ID = os.environ["FLAGSMITH_PROJECT_ID"]
FLAGSMITH_ENVIRONMENT_ID = int(os.environ["FLAGSMITH_ENVIRONMENT_ID"])
FLAGSMITH_FLAG = os.environ["FLAGSMITH_FLAG"]

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


def evaluate_gate(detail: dict, results: dict, base_weights: dict):
    """Pure gate logic. detail = experiment detail JSON, results = results endpoint JSON."""
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
        if name == "control":
            expected_share.append((100.0 - listed_total) / 100.0)
        else:
            # Only one non-control allocation is listed (option 2 = V_2 for this flag).
            if len(planned_pct) != 1:
                raise ValueError("expected exactly one listed non-control allocation")
            expected_share.append(next(iter(planned_pct.values())) / 100.0)
    total_n = sum(observed)
    expected = [s * total_n for s in expected_share]
    stat, p = chisquare(f_obs=observed, f_exp=expected)
    srm_result = PASS if stat < SRM_CRITICAL else "FAIL"
    srm_reason = f"chi-square={stat:.2f}, p={p:.3g} (variants {labels}, n {observed}, expected {[round(e, 1) for e in expected]})"

    rollout_result, rollout_reason = check_rollout_vs_base(rollout, base_weights)

    seen_result, seen_reason = NOT_CHECKED, "no seen log on this instance"

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
    return evaluate_gate(detail, results, base_weights)


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
        if comment["body"] != expected:
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


def approve_and_pause(posted_hash: str, experiment_id=None) -> str:
    """Pause checkout_button only if the verdict is unchanged and an authorised approval exists.

    Returns a short status string: 'PAUSED', 'REFUSED: ...', or 'PAUSE FAILED: ...'.
    """
    exp_id = int(experiment_id if experiment_id is not None else FLAGSMITH_EXPERIMENT_ID)
    report = current_gate(exp_id)
    new_hash = verdict_hash(report)
    if new_hash != posted_hash:
        _post_comment(
            f"Verdict changed since it was posted (new hash {new_hash}). "
            "Refusing to pause checkout_button. Re-run the gate and post a new card."
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
        PAUSE_FALLBACK_TITLE,
        render_card(report) + f"\n\n**set_flag error:**\n\n```\n{error}\n```\n",
    )
    return f"PAUSE FAILED: fallback issue opened ({error})"


def _md_cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def render_card(gate_result: dict) -> str:
    """Render a gate result (from evaluate_gate / run_gate) as a Markdown verdict card."""
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
    for name, rate in gate_result["conversion_rate"].items():
        value = f"{rate:.3%}" if rate is not None else "n/a"
        lines.append(f"- {_md_cell(name)}: {value}")
    lines += [
        "",
        f"To pause checkout_button, comment /approve {verdict_hash(gate_result)} on this issue. Nothing changes until then.",
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
    # Read-only: runs the gate for experiment 1, prints the card, and writes it to a file.
    # Does not call set_flag or any PATCH.
    report = run_gate(1)
    card = render_card(report)
    print()
    print(card)
    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "verdict_experiment_1.md")
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(card)
    print("written:", out_path)

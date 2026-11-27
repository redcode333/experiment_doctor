"""Experiment Doctor agent: polls one GitHub issue and runs fixed steps for three commands.

    check experiment <id>          -> gate, explanation, post card, record the posted hash
    pause <flag> with <hash>       -> refused unless the hash matches the posted card;
                                      then approve_and_pause (which still requires /approve)
    anything else                  -> ignored

The LLM never chooses a step. Every action is decided in code below.
Run with:  python agent.py      (stop with Ctrl+C)
"""

import json
import os
import re
import time
from datetime import datetime

import flagsmith_live as fl

STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent_state.json")
CHECK_RE = re.compile(r"^check experiment (\d+)$")
PAUSE_RE = re.compile(r"^pause (\S+) with ([0-9a-fA-F]{64})$")
MAX_ATTEMPTS = 2  # one try, then one retry; after that the error is posted and polling continues


def poll_seconds() -> int:
    raw = os.environ.get("AGENT_POLL_SECONDS", "").strip()
    return int(raw) if raw else 60


def log(command: str, outcome: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} | {command} | {outcome}", flush=True)


# ---- state -----------------------------------------------------------------

def load_state(path: str = STATE_PATH) -> dict:
    if not os.path.exists(path):
        return {"handled": [], "posted_hashes": {}, "bootstrapped": False}
    with open(path, encoding="utf-8") as fh:
        state = json.load(fh)
    state.setdefault("handled", [])
    state.setdefault("posted_hashes", {})
    state.setdefault("bootstrapped", True)
    return state


def save_state(state: dict, path: str = STATE_PATH) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# ---- GitHub comments -------------------------------------------------------

def list_comments() -> list:
    """Latest 100 comments on the configured issue, oldest first."""
    resp = fl.requests.get(
        fl._issue_comments_url(),
        params={"per_page": 100},
        headers=fl._gh_headers(),
        timeout=fl.TIMEOUT,
    )
    resp.raise_for_status()
    return sorted(resp.json(), key=lambda c: c["created_at"])


def bootstrap(state: dict, path: str = STATE_PATH) -> None:
    """On first start, mark existing comments as seen so old commands are not replayed."""
    if state.get("bootstrapped"):
        return
    existing = list_comments()
    state["handled"] = sorted(set(state["handled"]) | {c["id"] for c in existing})
    state["bootstrapped"] = True
    save_state(state, path)
    log("bootstrap", f"recorded {len(existing)} existing comments as seen; only new comments are processed")


def parse_command(body: str):
    """Commands must be the first line of the comment."""
    lines = (body or "").strip().splitlines()
    if not lines:
        return None
    first = lines[0].strip()
    m = CHECK_RE.match(first)
    if m:
        return ("check", int(m.group(1)))
    m = PAUSE_RE.match(first)
    if m:
        return ("pause", m.group(1), m.group(2).lower())
    return None


def _safe_comment(text: str) -> None:
    try:
        fl._post_comment(text)
    except Exception as exc:  # the comment itself failed; record it and keep polling
        log("post comment", f"ERROR: {type(exc).__name__}: {exc}")


# ---- steps (fixed, in code) -------------------------------------------------

def step_check(experiment_id: int, state: dict) -> str:
    report = fl.current_gate(experiment_id)
    text, model = fl.explain_with_guard(report)
    card = fl.render_card(report, text, model)
    fl.post_card(card)
    state["posted_hashes"][str(experiment_id)] = fl.verdict_hash(report)
    return f"posted card for experiment {experiment_id} (model {model})"


def step_pause(flag: str, claimed_hash: str, state: dict) -> str:
    if flag != fl.FLAGSMITH_FLAG:
        return f"REFUSED: flag {flag!r} is not the configured flag"
    # The hash must match a card this agent posted. The experiment is the one that card was for.
    matches = [exp for exp, posted in state["posted_hashes"].items() if posted == claimed_hash]
    if len(matches) != 1:
        return "REFUSED: hash does not match any card posted by this agent"
    # approve_and_pause re-runs the gate, compares the hash, and checks for a /approve comment.
    return fl.approve_and_pause(claimed_hash, int(matches[0]))


def run_step(fn):
    """Run a step, retrying once on error. Returns (ok, result_or_error_text)."""
    last = None
    for _ in range(MAX_ATTEMPTS):
        try:
            return True, fn()
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
    return False, last


# ---- dispatch ----------------------------------------------------------------

def handle_comment(comment: dict, state: dict):
    """Returns (command_label, outcome) for a recognised command, or None if the comment is ignored."""
    cmd = parse_command(comment.get("body", ""))
    if cmd is None:
        return None
    label = comment["body"].strip().splitlines()[0].strip()
    if not fl._has_write_access(comment["user"]["login"]):
        outcome = "REFUSED: only users with write or admin access can run agent commands"
        _safe_comment(f"Agent: {outcome}")
        return label, outcome
    if cmd[0] == "check":
        ok, result = run_step(lambda: step_check(cmd[1], state))
    else:
        ok, result = run_step(lambda: step_pause(cmd[1], cmd[2], state))
    if ok:
        if cmd[0] == "pause":
            _safe_comment(f"Pause outcome: {result}")
        return label, result
    _safe_comment(f"Agent error: {result}")
    return label, f"ERROR: {result}"


def poll_once(state: dict, path: str = STATE_PATH) -> None:
    handled = set(state["handled"])
    for comment in list_comments():
        if comment["id"] in handled:
            continue
        try:
            outcome = handle_comment(comment, state)
        except Exception as exc:
            outcome = (comment.get("body", "").strip()[:80], f"ERROR: {type(exc).__name__}: {exc}")
            _safe_comment(f"Agent error: {outcome[1]}")
        handled.add(comment["id"])
        state["handled"] = sorted(handled)
        save_state(state, path)
        if outcome is not None:
            log(*outcome)


def main() -> None:
    state = load_state()
    bootstrap(state)
    interval = poll_seconds()
    log("start", f"polling issue every {interval}s; Ctrl+C to stop")
    try:
        while True:
            try:
                poll_once(state)
            except Exception as exc:
                log("poll", f"ERROR: {type(exc).__name__}: {exc}")
            time.sleep(interval)
    except KeyboardInterrupt:
        log("stop", "stopped by Ctrl+C")


if __name__ == "__main__":
    main()

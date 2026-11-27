# Experiment Doctor: integration guide

Experiment Doctor checks whether a Flagsmith experiment's result can be trusted. It posts a verdict card on a GitHub issue. It pauses the flag only after a person with write or admin access comments `/approve <hash>` on that issue.

Verdicts are indicative. The tool does not prove that a variant is a winner.

Everything project-specific comes from configuration: the flag name, the control variant name, the Flagsmith project and environment IDs, the GitHub repo and issue, and the models. No flag name, variant name, or ID is hardcoded.

## What a team must provide

1. **A Flagsmith instance**, self-hosted or hosted, with:
   - one project and one environment;
   - one multivariate flag, with a control value and one treatment value;
   - one experiment on that flag, with traffic.
2. **Keys**:
   - a Flagsmith admin (master) API key, used as `FLAGSMITH_TOKEN`;
   - the environment's server-side key, used by the app's SDK as `FLAGSMITH_ENV_KEY`;
   - a GitHub token with permission to read and write issue comments on the repo, and to read collaborator permissions;
   - an OpenRouter API key and two free models whose names end in `:free`.
3. **An app that serves the flag and writes `seen.jsonl`** in the format below. `checkout_app.py` in this repo is a reference implementation.

**Current limits:** the gate supports one control variant and one treatment variant. The split check and the rollout check expect a single non-control allocation. Experiments with more treatments return NOT_CHECKED for those checks rather than guessing.

## The seen log: `seen.jsonl`

The seen check reads an append-only file with one JSON object per line. Each record is written when a user is shown a variant.

| Field | Type | Meaning |
| --- | --- | --- |
| `user_id` | string | The identity the flag was evaluated for. |
| `variant` | string | The value Flagsmith returned for `FLAGSMITH_FLAG`. If the value is empty, write `FLAGSMITH_CONTROL_VARIANT` (default `control`). |
| `ts` | string | UTC time the variant was shown, in ISO 8601 with an offset, for example `2026-05-02T00:00:00+00:00`. |

Example line:

```
{"user_id": "u1042", "variant": "variant_b", "ts": "2026-05-02T00:00:00+00:00"}
```

How the seen check uses it:
- Only records at or after the experiment's `started_at` count.
- The earliest record must be at or before `started_at`. If the log starts later, the check returns NOT_CHECKED, because the log does not cover the experiment.
- Records whose `variant` equals `FLAGSMITH_CONTROL_VARIANT` count as control. Every other value counts as treatment.
- FAIL means some variant has fewer records inside the window than the experiment has counted users for that variant.

## Configuration

Copy `.env.example` to `.env` and fill in every value. Never commit `.env`; it is listed in `.gitignore`. Check with `git check-ignore -v .env`.

| Name | Required | Purpose |
| --- | --- | --- |
| `FLAGSMITH_URL` | yes | Base URL of the Flagsmith instance. |
| `FLAGSMITH_TOKEN` | yes | Admin API key, sent as `Api-Key`. |
| `FLAGSMITH_ENV_KEY` | yes | Environment server-side key for the app's SDK. |
| `FLAGSMITH_PROJECT_ID` | yes | Project that holds the flag. |
| `FLAGSMITH_ENVIRONMENT_ID` | yes | Environment to read experiments from and pause in. |
| `FLAGSMITH_FLAG` | yes | Flag name. No default. |
| `FLAGSMITH_CONTROL_VARIANT` | no | Control variant name. Default `control`. |
| `GITHUB_TOKEN` | yes | Reads and writes issue comments; reads collaborator permissions. |
| `GITHUB_REPO` | yes | `owner/repo` that holds the verdict issue. |
| `GITHUB_ISSUE_NUMBER` | yes | Issue where cards are posted and approvals are read. |
| `OPENROUTER_API_KEY` | yes | Explanation step. |
| `OPENROUTER_MODEL` | yes | Primary free model; must end in `:free`. |
| `OPENROUTER_BACKUP_MODEL` | yes | Backup free model; must end in `:free` and differ from the primary. |
| `AGENT_POLL_SECONDS` | no | Agent poll interval. Default 60. |

Experiment IDs are not configured. You pass the experiment ID on the command line, or the agent takes it from the card a person approved.

`.env` wins over variables already set in the OS environment (`load_dotenv(override=True)`).

## Install

```
pip install -r requirements.txt
```

The pinned versions are `requests==2.32.3`, `scipy==1.13.1`, `python-dotenv==1.0.1`, `pytest==8.3.3`, `fastapi==0.165.0`, `uvicorn==0.54.0` and `flagsmith==6.7.0`. `httpx` is installed with the Flagsmith SDK.

## Run the gate

Read-only. It prints the gate, generates the explanation, and writes `verdict_<experiment_id>.md`. It posts nothing and changes no flag.

```
python flagsmith_live.py <experiment_id>
```

The gate has three checks:
- **Split (chi-square):** compares observed variant counts with the planned split. PASS if the statistic is below 10.83 (1 degree of freedom, p = 0.001). Otherwise FAIL. Control's planned share is the remainder after the listed allocation.
- **Rollout vs base:** compares the experiment's planned allocation with the environment's base allocation. PASS if they match. NOT_CHECKED if they differ, because which one applies is unverified. NOT_CHECKED if a value is missing.
- **Seen:** described above.

Gate rules:
- Any FAIL gives **DO NOT SHIP**.
- Otherwise, any NOT_CHECKED, or an experiment status other than `stopped`, gives **NEEDS A LOOK**.
- **SHIP** needs every check to pass and the experiment to be `stopped`.

## Post, approve, pause

From Python:

```python
import flagsmith_live as fl

experiment_id = 42  # your experiment
report = fl.current_gate(experiment_id)
text, model = fl.explain_with_guard(report)
card = fl.render_card(report, text, model)   # refused without an explanation
fl.post_card(card)                           # posts to GITHUB_ISSUE_NUMBER
print("approval code:", fl.verdict_hash(report))
```

A person with admin or write access to `GITHUB_REPO` then comments, on `GITHUB_ISSUE_NUMBER`:

```
/approve <approval code>
```

Whitespace is normalised, so a line break after `/approve` is fine. Then:

```python
import flagsmith_live as fl

print(fl.approve_and_pause("<approval code>", 42))
```

`approve_and_pause` re-runs the gate and refuses if the verdict has changed since the card was posted. It also refuses unless a matching `/approve` comment exists from a user with admin or write access. Only then does it set `FLAGSMITH_FLAG` to off. Possible outcomes:
- `PAUSED`
- `REFUSED: verdict changed`
- `REFUSED: no approval from a user with write access`
- `PAUSE FAILED: fallback issue opened (...)`, with a new issue titled `Pause <FLAGSMITH_FLAG>: manual action needed`

**`approve_and_pause` will pause the flag even when the verdict is DO NOT SHIP**, because it checks only that the verdict is unchanged and that an approval exists. The tool does not require a SHIP verdict.

## The agent

`agent.py` polls `GITHUB_ISSUE_NUMBER` and runs fixed steps. The model never chooses a step.

```
python agent.py
```

Commands, which must be the first line of a comment:
- `check experiment <id>`: runs the gate, generates the explanation, posts the card, and records the approval code in `agent_state.json`. Refused without an explanation.
- `pause <flag> with <code>`: refused unless the flag is `FLAGSMITH_FLAG`, the code matches a card this agent posted, and the commenter has write or admin access. If all pass, it calls `approve_and_pause`, which still needs `/approve <code>`.
- Anything else is ignored.

Rules:
- Each comment is handled once. Handled IDs are stored in `agent_state.json`, which is in `.gitignore`.
- On first start, comments already on the issue are recorded as seen and not replayed. Delete `agent_state.json` to change that.
- A failing step is retried once, then the error is posted as a comment and polling continues.
- One line is printed per action: time, command, outcome.

## The checkout app

`checkout_app.py` is a FastAPI reference app that writes `seen.jsonl`.

```
uvicorn checkout_app:app --port 8010
```

- `GET /checkout?user_id=<id>` looks up the flag for that identity, appends one record to `seen.jsonl` at render time, and returns an HTML page showing the value Flagsmith returned.
- `GET /seen-log` returns the line count and counts per variant, without user IDs.

It reads `FLAGSMITH_URL`, `FLAGSMITH_ENV_KEY`, `FLAGSMITH_FLAG` and `FLAGSMITH_CONTROL_VARIANT` from `.env`. A request fails with a clear error if `FLAGSMITH_FLAG` is not set. Any lookup creates the identity in the Flagsmith environment.

## Reproduce

### a. Offline check (no keys, no network)

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
python -m pytest -q
```

**Expected:** every test passes. All HTTP is mocked. The generic workflow tests (`test_generic.py`) use `example_flag`, a `baseline` control, and different project, environment, experiment, repo and issue values, so they pass without any real-instance configuration.

### b. Flagsmith

Run a self-hosted Flagsmith with Docker by following the official guide at https://docs.flagsmith.com/ (look for self-hosting). Then create a project, an environment, a multivariate flag with a control and one treatment value, and an experiment on that flag with traffic.

### c. Configuration

Fill in `.env` as described in the Configuration table.

### d. Live gate

```
python flagsmith_live.py <experiment_id>
```

Expected output: the gate, one line per check, the conversion rate per variant, and the explanation's model name. Read it before you post anything.

### e. Post a card to a test issue

Set `GITHUB_ISSUE_NUMBER` to a test issue, then run the Python snippet in "Post, approve, pause".

### f. Approve and pause

Comment `/approve <code>` as a user with write or admin access, then call `approve_and_pause`. Only do this on a test environment until you have read the card.

## Tested and not tested

**Tested against a live self-hosted Flagsmith (development environment):**
- reading the environment, the experiment and its results;
- the split check, the rollout-vs-base check, and the gate;
- rendering and posting a card to a GitHub issue;
- approval refusal (missing and malformed `/approve` comments) and acceptance;
- pausing and restoring the flag in the development environment;
- the explanation through OpenRouter with the primary model;
- the checkout app, with 20 requests recorded in `seen.jsonl`.

**Tested with mocks only:**
- the seen check's PASS, FAIL and NOT_CHECKED outcomes, including the time-window rule;
- the OpenRouter backup model after a primary failure;
- the agent's command handling;
- the generic workflow with other names and IDs.

**Not tested:**
- approval from a second human user;
- the backup model in a live failure;
- the agent running against a live issue;
- more than one treatment variant.

## Known limits

- **Rollout conflicts.** If the experiment's planned allocation and the environment's base allocation differ, the rollout check returns NOT_CHECKED. Which allocation applies at runtime has not been verified.
- **Snapshot counts.** Experiment counts are read at one time and change as results refresh. Verdicts are tied to the time they were read; `as_of` is shown on the card.
- **Approval comment window.** The approval check reads the latest 100 comments on the issue. An older approval comment will not be found.
- **Verdict hash.** The approval code covers the experiment ID, the gate verdict, and each check's name, result and reason. It does not cover `as_of`. A change to any check's reason changes the code, so approvals made on an older card stop working.
- **Rate limits.** Free OpenRouter models may be rate-limited. If both models fail, no card is posted.
- **Seen log coverage.** The seen check returns NOT_CHECKED until the log covers the experiment's start time. Records written after the start do not make up for a log that starts later.

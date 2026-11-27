# Experiment Doctor

Experiment Doctor checks whether a Flagsmith experiment's result can be trusted. It posts a verdict card on a GitHub issue. It pauses the flag only after a person comments `/approve <hash>` on that issue with the matching code.

Verdicts are indicative. This tool does not prove that a variant is a winner.

## What it does

1. Reads an experiment and its results from a self-hosted Flagsmith.
2. Runs three checks on the results.
3. Turns the checks into a verdict: DO NOT SHIP, NEEDS A LOOK, or SHIP.
4. Asks a free OpenRouter model to explain the verdict, using only the facts it was given.
5. Posts a card with the verdict, the checks and the explanation to a GitHub issue.
6. Waits for a person with write or admin access to comment `/approve <hash>`.
7. Turns off the flag through the Flagsmith API, but only if the verdict is unchanged and the approval is present.

## The flow, step by step

1. **Read the experiment.** `current_gate(experiment_id)` fetches the experiment detail, the results, and the flag's base allocations.
2. **Run the checks.** The split check, the rollout-vs-base check and the seen check each return PASS, FAIL or NOT_CHECKED, with a reason.
3. **Apply the gate rules.** The gate is computed from the checks and the experiment status.
4. **Explain.** `explain_with_guard` asks the primary model, then the backup model. Each gets one retry. A number guard rejects any number that isn't in the facts.
5. **Post the card.** `render_card` refuses to build a card without an explanation. `post_card` then posts it to the issue.
6. **Human approval.** A person with write or admin access comments `/approve <hash>` on the issue.
7. **Pause.** `approve_and_pause(posted_hash)` re-runs the gate, checks the hash, checks the approval, and calls the Flagsmith API to set the flag to off. If the pause call fails, it opens a new issue titled "Pause checkout_button: manual action needed".

Nothing runs automatically after the card is posted. A person has to call `approve_and_pause`.

## The checks

- **Split check (chi-square).** Compares observed variant counts with the planned split. The result is PASS if the statistic is below 10.83 (1 degree of freedom, p = 0.001). Otherwise it is FAIL. Control's planned share is taken as the remainder after the listed allocations.
- **Rollout vs base.** Compares the experiment's planned allocation for each variant with the environment's base allocation. If they match, the result is PASS. If they differ, the result is NOT_CHECKED, because which one applies has not been verified. If a value is missing, the result is also NOT_CHECKED.
- **Seen check.** Always NOT_CHECKED on this instance, because there is no seen log.

## Gate rules

- Any FAIL gives **DO NOT SHIP**.
- Otherwise, any NOT_CHECKED or an experiment status other than `stopped` gives **NEEDS A LOOK**.
- **SHIP** needs every check to pass and the experiment to be stopped.

On this instance the seen check is always NOT_CHECKED, so SHIP cannot be reached.

## Approval

- The approval code is `verdict_hash(gate_result)`, a SHA-256 over the experiment id, the gate verdict, and each check's name, result and reason. It does not cover `as_of`, so the code does not change when results refresh without changing the verdict.
- The approval comment must read `/approve <hash>` after whitespace is normalised. Runs of spaces and newlines collapse to a single space, and the text is trimmed.
- The author must have admin or write permission on the repo. The check uses the collaborator permission endpoint.
- `approve_and_pause` refuses if the verdict has changed since the card was posted. It posts a comment saying so.
- Nothing is automatic. A person has to comment, and a person has to call `approve_and_pause`.
- **`approve_and_pause` will pause a flag even when the verdict is DO NOT SHIP**, because it checks only the hash and the approval. It does not require the verdict to be SHIP.

## Explanation

- The explanation comes from an LLM through OpenRouter.
- `OPENROUTER_MODEL` is required, and it must end in `:free`. `OPENROUTER_BACKUP_MODEL` is also required, must end in `:free`, and must differ from the primary.
- The model gets the gate facts as JSON. It is told to copy every number exactly, and to add no numbers and no recommendations.
- The number guard checks every number in the explanation against the facts. The numbers 100 and 50 are exempt.
- Each model gets one retry. If both models fail, `explain_with_guard` raises an error that lists the reason for each model. The card is not built and nothing is posted.
- The card shows which model produced the explanation.

## Setup

### 1. Config file

Copy `.env.example` to `.env` and fill in the values. Never commit `.env`. It is listed in `.gitignore`.

| Name | What it is |
| --- | --- |
| `FLAGSMITH_URL` | Base URL of your Flagsmith instance, for example `http://localhost:8000` |
| `FLAGSMITH_TOKEN` | Flagsmith admin (master) API key, sent as `Api-Key` |
| `FLAGSMITH_ENV_KEY` | Environment server-side key (used for the environment-document read) |
| `FLAGSMITH_PROJECT_ID` | Project ID that holds the flag |
| `FLAGSMITH_ENVIRONMENT_ID` | Environment ID to read and pause in |
| `FLAGSMITH_FLAG` | Flag name, for example `checkout_button` |
| `FLAGSMITH_EXPERIMENT_ID` | Experiment ID to evaluate |
| `GITHUB_TOKEN` | GitHub token that can read and write issue comments on the repo |
| `GITHUB_REPO` | Repo in `owner/repo` form |
| `GITHUB_ISSUE_NUMBER` | Issue number where cards are posted |
| `OPENROUTER_API_KEY` | OpenRouter API key |
| `OPENROUTER_MODEL` | Primary free model name, ending in `:free` |
| `OPENROUTER_BACKUP_MODEL` | Backup free model name, ending in `:free`, different from the primary |

`load_dotenv(override=True)` makes `.env` win over variables already set in the OS environment.

### 2. Creating the tokens

- **Flagsmith:** create an admin (master) API key in your Flagsmith account settings. Use it as `FLAGSMITH_TOKEN`. Requests use the `Authorization: Api-Key <token>` header.
- **GitHub:** create a token with permission to read and write issue comments on the target repo. Approvals only count from users who have admin or write access to that repo.
- **OpenRouter:** create an API key at openrouter.ai, and choose two models whose names end in `:free`.

### 3. Dependencies and tests

```
pip install -r requirements.txt
python -m pytest test_flagsmith_live.py -q
```

The tests mock all HTTP calls and `set_flag`. They do not need network access or real credentials.

### 4. Running it

Generate the gate and explanation for experiment 1 and write the card to a local file. This does not post to GitHub and does not change any flag:

```
python flagsmith_live.py
```

Post the card and get the approval code from Python:

```python
import flagsmith_live as fl

report = fl.current_gate(int(fl.FLAGSMITH_EXPERIMENT_ID))
text, model = fl.explain_with_guard(report)
card = fl.render_card(report, text, model)
fl.post_card(card)
print("approval code:", fl.verdict_hash(report))
```

After a person has commented `/approve <code>` on the issue, pause the flag:

```python
import flagsmith_live as fl

print(fl.approve_and_pause("<code from the card>"))
```

There is no command-line interface for posting or approval yet.

## Reproduce

Follow these steps in order. Steps a and b are separate: step a needs no keys and no running Flagsmith.

### a. Offline check (no keys needed)

`requirements.txt` pins the versions the tests were run against:
`requests==2.32.3`, `scipy==1.13.1`, `python-dotenv==1.0.1`, `pytest==8.3.3`.

```
python -m venv .venv
.venv\Scripts\activate          # Windows
source .venv/bin/activate       # macOS / Linux
pip install -r requirements.txt
python -m pytest test_flagsmith_live.py -q
```

**Expected result:** all tests pass, with no network calls. HTTP is mocked in every test, so you don't need a Flagsmith instance, GitHub access, or OpenRouter access. At the last run, 27 tests passed.

### b. Flagsmith setup

Run a self-hosted Flagsmith with Docker by following the official self-hosting guide: https://docs.flagsmith.com/ (look for the self-hosting section). This README does not repeat those commands, because they change between Flagsmith releases.

Then, in the Flagsmith dashboard:
1. Create a project.
2. Create an environment in that project. Note its ID.
3. Create a multivariate flag, for example `checkout_button`, with a control variant and a `new_checkout` variant.
4. Create an experiment on that flag, and send it traffic. Note its ID.

### c. Config

1. Copy `.env.example` to `.env`.
2. Fill in the values:
   - Flagsmith: `FLAGSMITH_URL`, `FLAGSMITH_TOKEN`, `FLAGSMITH_ENV_KEY`, `FLAGSMITH_PROJECT_ID`, `FLAGSMITH_ENVIRONMENT_ID`, `FLAGSMITH_FLAG`, `FLAGSMITH_EXPERIMENT_ID`
   - GitHub: `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_ISSUE_NUMBER`
   - OpenRouter: `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `OPENROUTER_BACKUP_MODEL` (both model names must end in `:free`, and must differ)
3. Give each token only the permissions listed in the Setup section above:
   - Flagsmith admin key: read and write access to the project and environment.
   - GitHub token: read and write issue comments on the repo, and read collaborator permissions.
   - OpenRouter key: used for free-model requests only.

**Never commit `.env`.** It is listed in `.gitignore`. Before committing, check that it is still ignored with `git check-ignore -v .env`.

### d. Live run

Read the gate for your experiment. Nothing is posted and no flag is changed:

```python
import flagsmith_live as fl

report = fl.current_gate(int(fl.FLAGSMITH_EXPERIMENT_ID))
print(report["gate"], report["status"], report["is_final"])
```

**Expected output shape.** `report` is a dictionary with these keys:

```
{
  'experiment_id': <int>,
  'status': 'running' or 'stopped',
  'is_final': <bool>,
  'as_of': '<timestamp>',
  'checks': [
      ('split (chi-square)', '<PASS|FAIL>', '<reason>'),
      ('rollout vs base', '<PASS|NOT_CHECKED>', '<reason>'),
      ('seen', 'NOT_CHECKED', 'no seen log on this instance'),
  ],
  'conversion_rate': {'<variant>': <fraction>, ...},
  'gate': '<DO NOT SHIP|NEEDS A LOOK|SHIP>',
}
```

Then generate the explanation and post the card to a test issue. Set `GITHUB_ISSUE_NUMBER` to a test issue first:

```python
text, model = fl.explain_with_guard(report)
card = fl.render_card(report, text, model)
comment_id = fl.post_card(card)
print("comment id:", comment_id)
print("approval code:", fl.verdict_hash(report))
```

`render_card` refuses to build a card without an explanation, so a failed explanation means nothing is posted.

### e. Approval

1. A person with admin or write access to the repo comments `/approve <hash>` on the issue. Use the approval code printed in step d. Whitespace in the comment is normalised, so a line break after `/approve` is fine.
2. That person, or you, then calls the approval function in Python:

```python
import flagsmith_live as fl

print(fl.approve_and_pause("<hash>"))
```

The function returns one of these outcomes:
- `PAUSED`: the flag was set to off.
- `REFUSED: verdict changed`: the verdict no longer matches the posted card. A comment saying so is posted to the issue.
- `REFUSED: no approval from a user with write access`: no matching approval comment was found.
- `PAUSE FAILED: fallback issue opened (...)`: the Flagsmith call failed, and a new issue titled "Pause checkout_button: manual action needed" was opened.

**`approve_and_pause` will pause a flag even when the verdict is DO NOT SHIP**, because it checks only the hash and the approval.

## Tested and not tested

**Tested against the live instance:**
- Reading the experiment, its results and the environment's feature states.
- The split check, the rollout-vs-base check and the gate on experiment 1.
- Rendering the card and posting it to a GitHub issue.
- Approval refusal, when the approval comment was missing and then when it had the wrong format.
- Approval acceptance, which paused `checkout_button` in Development. It was restored afterwards.
- The explanation through OpenRouter, using the primary model.

**Not tested:**
- The seen check. It is always NOT_CHECKED here.
- The backup model in a live failure. The fallback is covered by mocked tests only.
- Approval from a second user. Only the repo owner's approval has been tested.

## Known limits

- **Rollout conflict on experiment 1.** The experiment rollout gives V_2 a 50% allocation, but the environment's base setting gives it 0%. The check reports NOT_CHECKED. Which allocation applies at runtime has not been verified.
- **Experiment counts are a snapshot.** They were read while experiment 1 was still running, so they may change as results refresh.
- **Approval comment window.** The approval check reads only the latest 100 comments on the issue. An older approval comment will not be found.
- **Rate limits.** Free OpenRouter models may be rate-limited. In that case the explanation step fails, and no card is posted.
- **Pause window on `checkout_button`.** The flag was off from the pause until 06:50:17 UTC on 2026-11-27. Experiment counts from that window are not clean.

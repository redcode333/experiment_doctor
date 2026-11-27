"""Checkout page that records which variant a user was shown.

GET /checkout?user_id=<id>  -> asks Flagsmith for the identity's flag value, appends one line to
                               seen.jsonl at render time, and returns an HTML page showing the variant.
GET /seen-log               -> total lines and counts per variant (no user ids).

Run with:  uvicorn checkout_app:app
"""

import html
import json
import os
from collections import Counter
from datetime import datetime, timezone

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from flagsmith import Flagsmith

load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), override=True)

SEEN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.jsonl")

app = FastAPI()
_client = None


def flag_name() -> str:
    """FLAGSMITH_FLAG from .env. No default: a missing value is an error."""
    value = os.environ.get("FLAGSMITH_FLAG", "").strip()
    if not value:
        raise RuntimeError("FLAGSMITH_FLAG is not set in .env")
    return value


def control_variant() -> str:
    """FLAGSMITH_CONTROL_VARIANT from .env, default 'control'."""
    return os.environ.get("FLAGSMITH_CONTROL_VARIANT", "").strip() or "control"


def get_client() -> Flagsmith:
    """Flagsmith SDK client, created on first use from .env (FLAGSMITH_URL, FLAGSMITH_ENV_KEY)."""
    global _client
    if _client is None:
        base = os.environ["FLAGSMITH_URL"].rstrip("/")
        _client = Flagsmith(
            environment_key=os.environ["FLAGSMITH_ENV_KEY"],
            api_url=f"{base}/api/v1/",
        )
    return _client


def variant_for(user_id: str) -> str:
    """The value Flagsmith returns for FLAGSMITH_FLAG for this identity.

    Whatever value comes back is recorded as is. An empty value means the control variant.
    """
    flags = get_client().get_identity_flags(user_id)
    value = flags.get_feature_value(flag_name())
    return str(value) if value else control_variant()


def append_seen(user_id: str, variant: str) -> None:
    line = {
        "user_id": user_id,
        "variant": variant,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with open(SEEN_PATH, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(line) + "\n")


@app.get("/checkout", response_class=HTMLResponse)
def checkout(user_id: str = Query(..., min_length=1)):
    try:
        flag_name()
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    try:
        variant = variant_for(user_id)
    except Exception as exc:  # Flagsmith unreachable or misconfigured
        raise HTTPException(status_code=502, detail=f"flag lookup failed: {type(exc).__name__}")
    append_seen(user_id, variant)  # written at render time, after the variant is known
    body = (
        "<!doctype html><html><head><meta charset='utf-8'><title>Checkout</title></head>"
        "<body><h1>Checkout</h1>"
        f"<p>You were shown the <strong>{html.escape(variant)}</strong> checkout.</p>"
        "</body></html>"
    )
    return HTMLResponse(body)


@app.get("/seen-log")
def seen_log():
    counts = Counter()
    total = 0
    if os.path.exists(SEEN_PATH):
        with open(SEEN_PATH, encoding="utf-8") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                total += 1
                counts[json.loads(raw)["variant"]] += 1
    return {"lines": total, "per_variant": dict(counts)}

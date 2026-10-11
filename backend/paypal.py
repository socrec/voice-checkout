"""Thin PayPal REST client: a cached OAuth token and one call() helper.

Check your sandbox credentials: uv run --env-file ../.env python paypal.py
"""
import json
import os
import time

import httpx

BASE = os.environ.get("PAYPAL_BASE", "https://api-m.sandbox.paypal.com")
_token = {"value": "", "expires": 0.0}


def token() -> str:
    if time.time() > _token["expires"] - 60:  # refresh a minute early
        r = httpx.post(
            f"{BASE}/v1/oauth2/token",
            auth=(os.environ["PAYPAL_CLIENT_ID"], os.environ["PAYPAL_CLIENT_SECRET"]),
            data={"grant_type": "client_credentials"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        _token.update(value=data["access_token"], expires=time.time() + data["expires_in"])
    return _token["value"]


def call(method: str, path: str, body: dict | str | None = None, request_id: str | None = None) -> dict:
    """body is a dict, or a JSON string that must reach PayPal byte for byte.
    request_id makes a POST idempotent: PayPal returns the first result for a repeated id."""
    if body is not None and not isinstance(body, str):
        body = json.dumps(body)
    headers = {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}
    if request_id:
        headers["PayPal-Request-Id"] = request_id
    r = httpx.request(method, BASE + path, content=body, headers=headers, timeout=30)
    if r.is_error:
        raise RuntimeError(f"PayPal {method} {path} -> {r.status_code}: {r.text}")
    return r.json() if r.content else {}


def verify_webhook(headers, raw_body: str) -> bool:
    """True if PayPal really sent this webhook. raw_body must be the request body exactly as received:
    PayPal checks a CRC of the event, so re-serialising the JSON can fail verification."""
    # Spliced in raw below, so it must be exactly one JSON object, or a body like
    # '{}, "webhook_id": "..."' could swap in an attacker's own webhook id.
    try:
        if not isinstance(json.loads(raw_body), dict):
            return False
    except ValueError:
        return False
    fields = {
        "auth_algo": headers.get("paypal-auth-algo"),
        "cert_url": headers.get("paypal-cert-url"),
        "transmission_id": headers.get("paypal-transmission-id"),
        "transmission_sig": headers.get("paypal-transmission-sig"),
        "transmission_time": headers.get("paypal-transmission-time"),
        "webhook_id": os.environ["PAYPAL_WEBHOOK_ID"],
    }
    if not all(fields.values()):
        return False
    body = json.dumps(fields)[:-1] + ', "webhook_event": ' + raw_body + "}"
    return call("POST", "/v1/notifications/verify-webhook-signature", body)["verification_status"] == "SUCCESS"


if __name__ == "__main__":
    print(json.dumps(call("GET", "/v1/notifications/webhooks"), indent=2))

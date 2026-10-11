import hmac
import json
import os
import re
import sqlite3
import statistics
import threading
import time
import uuid
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

import llm
import paypal
import scamcheck


def words(text: str) -> set[str]:
    """Lowercase word set with a trailing 's' dropped, so 'eggs' matches 'egg' but 'tea' never matches 'steak'."""
    return {w.removesuffix("s") for w in re.findall(r"[a-z0-9]+", text.lower())}


CATALOG = json.loads((Path(__file__).parent / "catalog.json").read_text())
for p in CATALOG:
    p["words"] = words(p["name"] + " " + " ".join(p["tags"]))
BY_ID = {p["id"]: p for p in CATALOG}
USUALS = ["m1", "b1", "t1"]
APP_URL = os.environ.get("APP_URL", "http://localhost:5174")
CONTACT = os.environ.get("TRUSTED_CONTACT", "your family")  # who approves held payments, as the shopper hears it
CONTACT_PHONE = os.environ.get("TRUSTED_CONTACT_PHONE", "")  # optional; offered to the shopper while an order is held
# Optional ntfy topic, such as https://ntfy.sh/<long random name>, that pushes to the family's phones.
# Anyone who knows the topic can read it, so keep the name long and random, or run your own ntfy server.
NTFY_URL = os.environ.get("FAMILY_NTFY_URL", "")
# Part of every PayPal-Request-Id: order ids restart when Render wipes the database, and a reused id would
# get PayPal's earlier answer back, so a new order would read as paid without being charged.
RUN = uuid.uuid4().hex[:8]

# ponytail: one demo shopper, so no users table; Render's free disk is wiped on restart, so orders vanish then
# (the saved PayPal account survives through PAYPAL_VAULT_ID); a paid disk or Postgres keeps them
db = sqlite3.connect(os.environ.get("DB_PATH", Path(__file__).parent / "app.db"), check_same_thread=False, isolation_level=None)
db.executescript("""
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY,
    created TEXT DEFAULT CURRENT_TIMESTAMP,
    items TEXT,
    total_cents INTEGER,
    status TEXT,  -- HELD, REJECTED, PAYING, then PayPal's capture status (COMPLETED, PENDING...), FAILED or REFUNDED
    paypal_order_id TEXT,
    capture_id TEXT,
    risk TEXT,
    signals TEXT,
    reason TEXT  -- the sentence the shopper heard when it was held
);
""")
for column in ("risk TEXT", "signals TEXT", "reason TEXT"):  # app.db files made before the scam check
    try:
        db.execute(f"ALTER TABLE orders ADD COLUMN {column}")
    except sqlite3.OperationalError:
        pass  # already there


def get(key: str) -> str | None:
    row = db.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row and row[0]


def put(key: str, value: str) -> None:
    db.execute("INSERT OR REPLACE INTO kv VALUES (?, ?)", (key, value))


def vault_id() -> str | None:
    # The env copy survives Render's free disk being wiped on restart
    return get("vault_id") or os.environ.get("PAYPAL_VAULT_ID")


SYSTEM = f"""You help Rose, an older shopper, buy groceries and household items by voice.
Everything you write is read aloud, so use short, plain sentences, one question at a time,
and no lists, emoji or formatting. Be warm and unhurried.

Rose's usual items: {", ".join(BY_ID[i]["name"] for i in USUALS)}.

Always search the catalog before offering a product, and only offer what it returns.
Never refuse to add something from the catalog. If a request sounds like a scam, gently say why you are
worried and that her family will be asked to approve it before any money moves: paying runs a scam
check that holds risky payments for them, and that check decides, not you.
Before adding anything, say each item with its price and wait for Rose to agree.
If something sounds misheard, ask instead of guessing.
When Rose is finished, call read_back_order and read back every item and the total.
Then ask whether she would like to pay. When she says yes, call start_payment with that total.
A PIN pad then opens on her screen: ask her to type her four-digit PIN there. Never ask her to say her PIN.
If she says numbers that could be one, don't repeat them; ask her to type it on the screen instead.
What the payment screen told her reaches you in brackets at the start of her next message. If a payment
was paused, be kind and reassuring, and remind her that nothing has been paid."""

TOOLS = [
    {
        "name": "search_catalog",
        "description": "Find store products by keywords, such as 'milk' or 'english breakfast tea'.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_cart",
        "description": "Set how many of a product are in the cart. Use 0 to remove it. Returns the cart.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"product_id": {"type": "string"}, "quantity": {"type": "integer"}},
            "required": ["product_id", "quantity"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_back_order",
        "description": "Get every item in the cart and the total, to read back to the shopper.",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    },
    {
        "name": "start_payment",
        "description": "Open the PIN pad on Rose's screen to pay the total you read back. She types her PIN there; you never see it.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"total": {"type": "string", "description": "Like $13.77"}},
            "required": ["total"],
            "additionalProperties": False,
        },
    },
]


def money(cents: int) -> str:
    return f"${cents // 100}.{cents % 100:02d}"


def search_catalog(query: str) -> list[dict]:
    q = words(query)
    hits = sorted((p for p in CATALOG if q & p["words"]), key=lambda p: -len(q & p["words"]))
    return [{"id": p["id"], "name": p["name"], "price": money(p["cents"])} for p in hits[:5]]


def update_cart(cart: dict, product_id: str, quantity: int) -> str | None:
    """Returns an error for the model, or None on success."""
    if product_id not in BY_ID:
        return f"No product with id {product_id}. Search the catalog first."
    if not 0 <= quantity <= 20:
        return "Quantity must be between 0 and 20."
    if quantity:
        cart[product_id] = quantity
    else:
        cart.pop(product_id, None)
    return None


def order(cart: dict) -> dict:
    lines = [
        {"name": BY_ID[i]["name"], "quantity": q, "price": money(BY_ID[i]["cents"] * q)} for i, q in cart.items()
    ]
    return {"lines": lines, "total": money(sum(BY_ID[i]["cents"] * q for i, q in cart.items()))}


def in_background(fn, *args) -> None:
    threading.Thread(target=fn, args=args, daemon=True).start()


def push(title: str, message: str) -> None:
    try:  # no Click link: it would have to carry the family key through the ntfy server
        httpx.post(NTFY_URL, content=message.encode(), headers={"Title": title, "Priority": "high", "Tags": "warning"}, timeout=10).raise_for_status()
    except httpx.HTTPError as e:
        print("Family notification failed:", e)  # the order still waits on the family page


def tell_family(title: str, message: str) -> None:
    """Reach the family with their page closed. Sent in the background so the shopper never waits on it.
    title must be plain ASCII, since it travels as an HTTP header."""
    if NTFY_URL:
        in_background(push, title, message)


def usual_spend() -> int | None:
    """Her typical shop: the median of her last 10 orders that were paid without a hold, once there are enough."""
    rows = db.execute("SELECT total_cents FROM orders WHERE status = 'COMPLETED' AND risk = 'low' ORDER BY id DESC LIMIT 10")
    totals = [cents for (cents,) in rows]
    return int(statistics.median(totals)) if len(totals) >= scamcheck.USUAL_MIN_ORDERS else None


def minutes_since_last_paid() -> float | None:
    row = db.execute(
        "SELECT (julianday('now') - julianday(created)) * 1440 FROM orders WHERE status = 'COMPLETED' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return row and row[0]


PIN_TRIES, PIN_LOCK_MINUTES = 5, 15
LOCKED = f"That PIN was wrong too many times, so paying is paused for {PIN_LOCK_MINUTES} minutes. Nothing was paid."


def pin_locked() -> bool:
    return float(get("pin_locked_until") or 0) > time.time()


def wrong_pin() -> bool:
    """Count a wrong PIN. The fifth in a row pauses paying; returns True when it does."""
    # ponytail: one counter for the one demo shopper, so a stranger's wrong guesses also pause her; per-device limits fix that
    fails = int(get("pin_fails") or 0) + 1
    if fails < PIN_TRIES:
        put("pin_fails", str(fails))
        return False
    put("pin_fails", "0")
    put("pin_locked_until", str(time.time() + PIN_LOCK_MINUTES * 60))
    tell_family(
        "Voice Checkout: wrong PIN 5 times",
        f"Someone typed the wrong PIN {PIN_TRIES} times in a row, so paying is paused for {PIN_LOCK_MINUTES} minutes. "
        "Nothing was paid. If it wasn't her, check in with her.",
    )
    return True


def start_payment(s: dict, total: str) -> str:
    """The model's only part in paying: check the order, then open the PIN pad. Returns what the model is told."""
    o = order(s["cart"])
    if not s["cart"]:
        return "The cart is empty."
    if total != o["total"]:
        return f"The total is now {o['total']}. Read the order back to Rose again before paying."
    if not vault_id():
        return "PayPal is not connected yet. Her family needs to connect it on the family page first."
    if pin_locked():
        return f"Paying is paused for up to {PIN_LOCK_MINUTES} minutes after too many wrong PINs. Ask her to try again later."
    s["paying"] = total
    return f"The PIN pad is on Rose's screen now, with a button to pay {total}. Ask her to type her four-digit PIN there."


def pay(s: dict, pin: str) -> str:
    """The PIN pad's Pay button: PIN, then the scam check, then PayPal or a hold. Returns what the shopper is told.
    The PIN arrives here from the keypad, never through the model, and is never stored."""
    total = s.get("paying")
    if not total:
        return "There's nothing waiting to be paid."
    if pin_locked():
        s.pop("paying")
        return LOCKED
    if not hmac.compare_digest(re.sub(r"[^0-9]", "", pin), os.environ["PAY_PIN"]):
        if wrong_pin():
            s.pop("paying")
            return LOCKED
        return "That PIN isn't right. Nothing was paid. Please try again."
    put("pin_fails", "0")
    s.pop("paying")
    cart = s["cart"]
    o = order(cart)
    if not cart or o["total"] != total:
        return f"Your order changed, so nothing was paid. It is now {o['total']}. Let's check it again."
    if not vault_id():
        return "PayPal isn't set up yet, so nothing was paid. Please ask your family to connect it."

    cents = sum(BY_ID[i]["cents"] * q for i, q in cart.items())
    items = [{**BY_ID[i], "quantity": q} for i, q in cart.items()]
    signals = scamcheck.check(items, cents, " ".join(s["said"]), minutes_since_last_paid(), usual_spend())
    risk = scamcheck.risk(signals)
    held = risk != "low"
    reason = scamcheck.explain(signals) if held else None
    order_id = db.execute(
        "INSERT INTO orders (items, total_cents, status, risk, signals, reason) VALUES (?, ?, ?, ?, ?, ?)",
        (json.dumps(o["lines"]), cents, "HELD" if held else "PAYING", risk, json.dumps(signals), reason),
    ).lastrowid
    if held:
        cart.clear()
        s["waiting"] = order_id  # the shopper's page polls it and says the family's answer out loud
        s.setdefault("held", []).append(order_id)  # and the model hears it with her next message
        tell_family(
            f"Voice Checkout: order {order_id} needs your OK",
            f"Order {order_id} for {o['total']} is paused, and nothing is paid. Why: "
            + " ".join(sig["reason"] for sig in signals)
            + " Open your family link to approve or reject it.",
        )
        call = f" You can call {CONTACT} on {CONTACT_PHONE}." if CONTACT_PHONE else ""
        return f"{reason} Nothing is paid yet. Order {order_id} is now waiting for {CONTACT} to check it.{call}"

    status = charge(order_id)
    if status == "FAILED":
        return "PayPal turned the payment down. Nothing was paid."
    if status != "COMPLETED":
        return f"PayPal hasn't finished the payment yet. Its status is {status}."
    cart.clear()
    return f"Paid {o['total']} with PayPal. Your order number is {order_id}."


def charge(order_id: int) -> str:
    """Charge the saved PayPal account, with no buyer present, for an order marked PAYING. Returns its new status."""
    (cents,) = db.execute("SELECT total_cents FROM orders WHERE id = ?", (order_id,)).fetchone()
    try:
        r = paypal.call(
            "POST",
            "/v2/checkout/orders",
            {
                "intent": "CAPTURE",
                "purchase_units": [{"description": "Voice Checkout groceries", "amount": {"currency_code": "USD", "value": money(cents)[1:]}}],
                "payment_source": {"paypal": {"vault_id": vault_id()}},
            },
            request_id=f"voice-checkout-{RUN}-order-{order_id}",
        )
    except RuntimeError as e:
        db.execute("UPDATE orders SET status = 'FAILED' WHERE id = ?", (order_id,))
        print(e)
        return "FAILED"
    # With a saved PayPal account and intent CAPTURE, PayPal captures straight away; no separate capture call.
    captures = r["purchase_units"][0].get("payments", {}).get("captures", [])
    status = captures[0]["status"] if captures else r["status"]
    db.execute(
        "UPDATE orders SET status = ?, paypal_order_id = ?, capture_id = ? WHERE id = ?",
        (status, r["id"], captures[0]["id"] if captures else None, order_id),
    )
    return status


def run_tool(s: dict, name: str, args: dict) -> str:
    cart = s["cart"]
    if name == "start_payment":
        return start_payment(s, args["total"])
    if name == "search_catalog":
        return json.dumps(search_catalog(args["query"]) or "No matches. Try one simple word.")
    if name == "update_cart":
        if error := update_cart(cart, args["product_id"], args["quantity"]):
            return error
        s.pop("paying", None)  # a changed order closes the PIN pad until it is read back again
    return json.dumps(order(cart))


app = FastAPI()
# ponytail: in-memory sessions, lost on restart and never evicted; fine for one demo shopper
sessions: dict[str, dict] = {}


class SessionIn(BaseModel):
    session_id: str = Field(min_length=1, max_length=64)


class ChatIn(SessionIn):
    text: str = Field(min_length=1, max_length=1000)


class PayIn(SessionIn):
    pin: str = Field(min_length=1, max_length=20)


def view(s: dict) -> dict:
    """What the shopper's page shows: the cart, the open PIN pad's total, and a newly held order to watch."""
    return {"cart": order(s["cart"]), "paying": s.get("paying"), "waiting": s.pop("waiting", None)}


def new_session() -> dict:
    # said: only the shopper's own words, for the scam check, never the payment notes added to her messages
    return {"messages": [], "cart": {}, "said": []}


@app.post("/api/chat")
def chat(body: ChatIn):
    s = sessions.setdefault(body.session_id, new_session())
    s["said"].append(body.text)
    for order_id in list(s.get("held", [])):
        (status,) = db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
        if answer := family_answer(status):
            s["held"].remove(order_id)
            s.setdefault("notes", []).append(answer)
    text = body.text
    if notes := s.get("notes"):
        text = f"(The payment screen told Rose: {' '.join(notes)})\n{text}"
    answer = llm.reply(SYSTEM, TOOLS, s["messages"], text, lambda name, args: run_tool(s, name, args))
    s.pop("notes", None)
    return {"reply": answer, **view(s)}


@app.get("/api/session/{session_id}")
def session(session_id: str):
    """For the shopper's page after a reload."""
    s = sessions.get(session_id) or new_session()
    return {"cart": order(s["cart"]), "paying": s.get("paying"), "contact": {"name": CONTACT, "phone": CONTACT_PHONE or None}}


@app.post("/api/pay")
def pay_with_pin(body: PayIn):
    s = sessions.get(body.session_id)
    if not s or not s.get("paying"):
        raise HTTPException(409, "There's nothing waiting to be paid.")
    message = pay(s, body.pin)
    s.setdefault("notes", []).append(message)
    return {"message": message, **view(s)}


@app.post("/api/pay/cancel")
def cancel_payment(body: SessionIn):
    s = sessions.get(body.session_id)
    if s and s.pop("paying", None):
        s.setdefault("notes", []).append("She closed the PIN pad without paying. Nothing was paid.")
    return {"ok": True}


def family_only(x_family_key: str = Header("")):
    """The family's link carries FAMILY_KEY and the shopper's page never has it, so someone coaching her
    over the phone can't talk her into approving her own held payment."""
    key = os.environ.get("FAMILY_KEY", "")
    if not key or not hmac.compare_digest(x_family_key.encode(), key.encode()):
        raise HTTPException(401, "This family link is not valid.")


@app.get("/api/orders", dependencies=[Depends(family_only)])
def orders():
    """Every order, newest first, with its scam check, for the caregiver view."""
    rows = db.execute("SELECT id, created, items, total_cents, status, risk, signals, reason FROM orders ORDER BY id DESC")
    return [
        {
            "id": order_id,
            "created": created.replace(" ", "T") + "Z",  # SQLite's CURRENT_TIMESTAMP is UTC with no zone
            "items": json.loads(items),
            "total_cents": cents,
            "status": status,
            "risk": risk,  # None for orders placed before the scam check existed
            "signals": json.loads(signals or "[]"),
            "reason": reason,
        }
        for order_id, created, items, cents, status, risk, signals, reason in rows
    ]


@app.post("/api/orders/{order_id}/approve", dependencies=[Depends(family_only)])
def approve(order_id: int):
    # Checking and changing the status in one statement means a double click can't charge twice.
    if not db.execute("UPDATE orders SET status = 'PAYING' WHERE id = ? AND status = 'HELD'", (order_id,)).rowcount:
        raise HTTPException(409, "This order is not waiting for approval.")
    return {"status": charge(order_id)}


@app.post("/api/orders/{order_id}/reject", dependencies=[Depends(family_only)])
def reject(order_id: int):
    if not db.execute("UPDATE orders SET status = 'REJECTED' WHERE id = ? AND status = 'HELD'", (order_id,)).rowcount:
        raise HTTPException(409, "This order is not waiting for approval.")
    return {"status": "REJECTED"}


@app.post("/api/orders/{order_id}/refund", dependencies=[Depends(family_only)])
def refund(order_id: int):
    row = db.execute("SELECT capture_id FROM orders WHERE id = ? AND status = 'COMPLETED'", (order_id,)).fetchone()
    if not row:
        raise HTTPException(409, "Only paid orders can be refunded.")
    try:  # an empty body refunds the whole capture
        paypal.call("POST", f"/v2/payments/captures/{row[0]}/refund", {}, request_id=f"voice-checkout-{RUN}-refund-{order_id}")
    except RuntimeError as e:
        print(e)
        raise HTTPException(502, "PayPal did not accept the refund.")
    db.execute("UPDATE orders SET status = 'REFUNDED' WHERE id = ?", (order_id,))
    return {"status": "REFUNDED"}


def family_answer(status: str) -> str | None:
    """What the shopper hears once the family has decided on a held order; None while it is still undecided."""
    messages = {
        "HELD": None,
        "PAYING": None,
        "COMPLETED": f"Your order was approved by {CONTACT}, and it is paid.",
        "REJECTED": f"Your order was stopped by {CONTACT}. Nothing was paid.",
        "FAILED": "PayPal turned the payment down. Nothing was paid.",
        "REFUNDED": "That order was refunded.",
    }
    return messages.get(status, f"PayPal has not finished the payment. Its status is {status}.")


@app.get("/api/orders/{order_id}/status")
def order_status(order_id: int):
    """Polled by the shopper's page while an order is held. message is what to say once the family decides."""
    row = db.execute("SELECT status FROM orders WHERE id = ?", (order_id,)).fetchone()
    if not row:
        raise HTTPException(404, "No such order.")
    return {"status": row[0], "message": family_answer(row[0])}


@app.get("/api/wallet")
def wallet():
    return {"connected": bool(vault_id())}


@app.post("/api/vault/setup", dependencies=[Depends(family_only)])
def vault_setup():
    """Step 1 of saving PayPal: the family member opens the returned link and approves on PayPal."""
    r = paypal.call("POST", "/v3/vault/setup-tokens", {"payment_source": {"paypal": {
        "usage_type": "MERCHANT",  # we charge later, with no buyer present
        "usage_pattern": "IMMEDIATE",
        "customer_type": "CONSUMER",
        "permit_multiple_payment_tokens": False,
        "description": "Voice Checkout pays for Rose's groceries after she confirms with her PIN",
        "experience_context": {
            "brand_name": "Voice Checkout",
            "shipping_preference": "NO_SHIPPING",
            "return_url": f"{APP_URL}/api/vault/return",
            "cancel_url": f"{APP_URL}/caregiver.html",
        },
    }}})
    put("setup_token", r["id"])  # PayPal's docs don't promise the id comes back on return_url, so keep it
    return {"approve_url": next(link["href"] for link in r["links"] if link["rel"] == "approve")}


@app.get("/api/vault/return")
def vault_return():
    """Step 2: PayPal sends the approver back here; swap the approved setup token for a reusable vault id."""
    r = paypal.call("POST", "/v3/vault/payment-tokens", {"payment_source": {"token": {"id": get("setup_token"), "type": "SETUP_TOKEN"}}})
    put("vault_id", r["id"])
    print(f"Saved PayPal vault id {r['id']}. Set PAYPAL_VAULT_ID to it so a restart doesn't lose it.")
    return RedirectResponse(f"{APP_URL}/caregiver.html")  # the family page reads its key from this tab's storage


@app.post("/api/paypal/webhook")
async def paypal_webhook(request: Request):
    raw = (await request.body()).decode()
    if not await run_in_threadpool(paypal.verify_webhook, request.headers, raw):
        raise HTTPException(400, "Webhook signature did not verify")
    event = json.loads(raw)
    print("PayPal webhook:", event.get("event_type"))
    # A capture that finishes later (PENDING to COMPLETED) or is denied. Refund events carry a refund id, so they match nothing.
    if event.get("event_type", "").startswith("PAYMENT.CAPTURE."):
        r = event.get("resource", {})
        db.execute("UPDATE orders SET status = ? WHERE capture_id = ? AND status != 'REFUNDED'", (r.get("status"), r.get("id")))
    return {"ok": True}

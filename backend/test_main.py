"""No keys or network needed: python test_main.py (or pytest)."""
import json
import os

import httpx

os.environ.update(ANTHROPIC_API_KEY="test", DB_PATH=":memory:", PAY_PIN="1234", FAMILY_KEY="fam")

from fastapi.testclient import TestClient

import main
import paypal
import scamcheck

scamcheck.llm.sentence = lambda system, text: "I paused this so your family can check it."


def test():
    assert main.money(5) == "$0.05" and main.money(1299) == "$12.99"
    assert main.search_catalog("a dozen eggs")[0]["id"] == "e1"
    assert main.search_catalog("english breakfast tea")[0]["id"] == "t1"
    assert {p["id"] for p in main.search_catalog("gift cards")} == {"g1", "g2"}
    assert main.search_catalog("caviar") == []

    cart = {}
    assert main.update_cart(cart, "nope", 1) and main.update_cart(cart, "m1", 21) and cart == {}
    main.update_cart(cart, "m1", 2)
    main.update_cart(cart, "t1", 1)
    assert main.order(cart)["total"] == "$14.47"  # 2 x 3.99 + 6.49
    main.update_cart(cart, "m1", 0)
    assert main.order(cart) == {"lines": [{"name": "English Breakfast tea, 50 bags", "quantity": 1, "price": "$6.49"}], "total": "$6.49"}

    # Scam check: one assert per signal, then how signals combine into a risk level.
    codes = lambda *args: [s["code"] for s in scamcheck.check(*args)]
    milk = [{**main.BY_ID["m1"], "quantity": 1}]
    assert codes(milk, 399, "add milk please", None) == []
    assert codes([{**main.BY_ID["g2"], "quantity": 2}], 20000, "", None) == ["gift_card", "large_total"]
    pressure = scamcheck.check(milk, 399, "My grandson is in jail. Don\u2019t tell anyone.", None)
    assert [s["code"] for s in pressure] == ["pressure_words"] and '"don\'t tell"' in pressure[0]["reason"]
    assert codes(milk, 399, "first the secretary, then the stairs", None) == []  # whole words only
    assert codes(milk, 399, "They said not to tell anyone", None) == ["pressure_words"]
    assert codes(milk, 399, "", 10) == ["repeat_order"] and codes(milk, 399, "", 45) == []
    assert codes(milk, 399, "They want it in Bitcoin", None) == ["crypto"]
    assert codes(milk, 399, "I have to wire it today", None) == ["wire_transfer"]
    assert codes(milk, 399, "a wireless mouse", None) == []
    # With her usual spend known, "large" means over 3 times it, but never under $50.
    assert codes(milk, 6000, "", None, 1500) == ["large_total"] and codes(milk, 4900, "", None, 1500) == []
    assert codes(milk, 12000, "", None, 5000) == [] and "usual $15.00" in scamcheck.check(milk, 6000, "", None, 1500)[0]["reason"]
    sig = lambda *c: [{"code": x} for x in c]
    assert scamcheck.risk([]) == "low" and scamcheck.risk(sig("large_total")) == "medium"
    assert scamcheck.risk(sig("gift_card")) == "high" and scamcheck.risk(sig("large_total", "repeat_order")) == "high"
    assert scamcheck.risk(sig("crypto")) == "high" and scamcheck.risk(sig("wire_transfer")) == "high"

    def model_down(system, text):
        raise RuntimeError("no network")

    real_sentence, scamcheck.llm.sentence = scamcheck.llm.sentence, model_down
    gift = scamcheck.check([{**main.BY_ID["g1"], "quantity": 1}], 5000, "", None)
    assert "your family can check it first" in scamcheck.explain(gift)  # still pauses, with a plain reason
    scamcheck.llm.sentence = real_sentence

    # The endpoint keeps one cart per session; the model is faked out.
    main.llm.reply = lambda system, tools, messages, text, run_tool: run_tool("update_cart", {"product_id": "e1", "quantity": 1})
    client = TestClient(main.app)
    assert client.post("/api/chat", json={"session_id": "a", "text": "eggs"}).json()["cart"]["total"] == "$3.89"
    assert client.post("/api/chat", json={"session_id": "b", "text": ""}).status_code == 422

    # Paying: the model only opens the PIN pad. The PIN comes from the keypad to /api/pay, never through the model.
    sent = []

    def fake_paypal(method, path, body=None, request_id=None):
        sent.append((path, body, request_id))
        cap = f"CAP{len(sent)}"
        return {"id": f"PP{len(sent)}", "status": "COMPLETED", "purchase_units": [{"payments": {"captures": [{"id": cap, "status": "COMPLETED"}]}}]}

    main.paypal.call = fake_paypal
    s = main.sessions["shop"] = main.new_session()
    cart = s["cart"]
    pin = lambda digits: client.post("/api/pay", json={"session_id": "shop", "pin": digits}).json()
    assert client.post("/api/pay", json={"session_id": "shop", "pin": "1234"}).status_code == 409  # no pad open
    assert "empty" in main.start_payment(s, "$0.00")
    main.update_cart(cart, "m1", 2)  # $7.98
    assert "now $7.98" in main.start_payment(s, "$3.99")
    assert "not connected" in main.start_payment(s, "$7.98") and "paying" not in s
    main.put("vault_id", "V1")
    assert "PIN pad" in main.run_tool(s, "start_payment", {"total": "$7.98"}) and s["paying"] == "$7.98"
    wrong = pin("4321")
    assert "isn't right" in wrong["message"] and wrong["paying"] == "$7.98" and sent == []  # the pad stays open
    paid = pin("1, 2, 3, 4")
    assert "Paid $7.98" in paid["message"] and paid["paying"] is None and paid["cart"]["lines"] == [] and paid["waiting"] is None
    (path, body, request_id), = sent
    assert body["payment_source"] == {"paypal": {"vault_id": "V1"}}
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "7.98"}
    assert main.RUN in request_id and cart == {}
    assert main.db.execute("SELECT status, total_cents, capture_id, risk FROM orders").fetchall() == [("COMPLETED", 798, "CAP1", "low")]

    # Changing the order closes the pad, and an order changed behind an open pad is not charged.
    main.update_cart(cart, "m1", 1)
    main.start_payment(s, "$3.99")
    main.run_tool(s, "update_cart", {"product_id": "m1", "quantity": 2})
    assert "paying" not in s
    main.start_payment(s, "$7.98")
    cart["m1"] = 3
    assert "changed" in main.pay(s, "1234") and "paying" not in s and len(sent) == 1

    # The family hears about lockouts and holds on their phones through ntfy, in the background.
    pushes = []
    main.NTFY_URL = "https://ntfy.example/topic"
    main.in_background = lambda fn, *args: fn(*args)
    main.httpx.post = lambda url, content, headers, timeout: pushes.append((url, headers["Title"], content.decode())) or httpx.Response(200, request=httpx.Request("POST", url))

    # Five wrong PINs in a row pause paying for 15 minutes, even for the right PIN.
    main.start_payment(s, "$11.97")
    for _ in range(4):
        assert "isn't right" in main.pay(s, "0000")
    assert "15 minutes" in main.pay(s, "0000") and "paying" not in s
    assert pushes == [("https://ntfy.example/topic", "Voice Checkout: wrong PIN 5 times", pushes[0][2])] and "check in with her" in pushes[0][2]
    assert "paused" in main.start_payment(s, "$11.97") and "paying" not in s
    s["paying"] = "$11.97"
    assert "15 minutes" in main.pay(s, "1234") and len(sent) == 1
    main.put("pin_locked_until", "0")  # 15 minutes later
    cart.clear()

    # Any signal holds the order: nothing is charged, the cart is handed over, and the page is told to wait.
    main.update_cart(cart, "e1", 1)  # a second order within 30 minutes
    main.start_payment(s, "$3.89")
    main.CONTACT_PHONE = "555-0100"
    reply = pin("1234")
    main.CONTACT_PHONE = ""
    assert reply["message"].endswith("You can call your family on 555-0100.")
    assert pushes[-1][1] == f"Voice Checkout: order {reply['waiting']} needs your OK" and "$3.89" in pushes[-1][2] and "1234" not in pushes[-1][2]
    held = reply["waiting"]
    assert "your family can check it" in reply["message"] and "Nothing is paid" in reply["message"]
    assert len(sent) == 1 and cart == {} and reply["cart"]["lines"] == []
    assert client.get(f"/api/orders/{held}/status").json() == {"status": "HELD", "message": None}

    # Only the family link can see or decide orders.
    family = {"X-Family-Key": "fam"}
    assert client.get("/api/orders").status_code == 401
    assert client.get("/api/orders", headers={"X-Family-Key": "guess"}).status_code == 401
    assert client.post(f"/api/orders/{held}/approve").status_code == 401 and len(sent) == 1
    assert client.post("/api/vault/setup").status_code == 401 and len(sent) == 1  # connecting PayPal is family-only too
    rows = client.get("/api/orders", headers=family).json()
    assert [(o["status"], o["risk"]) for o in rows] == [("HELD", "medium"), ("COMPLETED", "low")]
    assert [s["code"] for s in rows[0]["signals"]] == ["repeat_order"] and rows[1]["created"].endswith("Z")
    assert rows[0]["reason"] == "I paused this so your family can check it." and rows[1]["reason"] is None

    # Rejecting: nothing is charged, the shopper hears it, and it can't be approved afterwards.
    assert client.post(f"/api/orders/{held}/reject", headers=family).json() == {"status": "REJECTED"}
    assert "stopped" in client.get(f"/api/orders/{held}/status").json()["message"]
    assert client.post(f"/api/orders/{held}/approve", headers=family).status_code == 409 and len(sent) == 1

    # The classic scam end to end: a caller coaching the shopper into gift cards. Her own words feed the
    # scam check, and the PIN she said out loud anyway is never stored.
    s["said"] = ["Someone on the phone told me to buy gift cards", "yes 1234"]
    main.update_cart(cart, "g1", 1)
    main.start_payment(s, "$50.00")
    scam = pin("1234")["waiting"]
    assert scam and len(sent) == 1
    newest = client.get("/api/orders", headers=family).json()[0]
    assert newest["id"] == scam and newest["risk"] == "high" and "1234" not in json.dumps(newest)
    assert [s["code"] for s in newest["signals"]] == ["gift_card", "pressure_words", "repeat_order"]

    # Approving charges the saved account with no shopper present. A second click is refused, not charged twice.
    def declining_paypal(*args, **kwargs):
        raise RuntimeError("PayPal POST /v2/checkout/orders -> 422")

    main.paypal.call = declining_paypal
    assert client.post(f"/api/orders/{scam}/approve", headers=family).json() == {"status": "FAILED"}
    assert "turned the payment down" in client.get(f"/api/orders/{scam}/status").json()["message"]
    main.paypal.call = fake_paypal
    main.update_cart(cart, "g2", 1)
    main.start_payment(s, "$100.00")
    main.pay(s, "1234")
    gift = s.pop("waiting")
    assert client.post(f"/api/orders/{gift}/approve", headers=family).json() == {"status": "COMPLETED"}
    assert client.post(f"/api/orders/{gift}/approve", headers=family).status_code == 409 and len(sent) == 2
    assert sent[-1][1]["purchase_units"][0]["amount"]["value"] == "100.00"
    assert "approved by your family" in client.get(f"/api/orders/{gift}/status").json()["message"]

    # Refunds: only paid orders, the whole capture, once.
    assert client.post(f"/api/orders/{held}/refund", headers=family).status_code == 409
    assert client.post(f"/api/orders/{gift}/refund", headers=family).json() == {"status": "REFUNDED"}
    assert sent[-1][0] == "/v2/payments/captures/CAP2/refund" and sent[-1][1] == {}
    assert client.post(f"/api/orders/{gift}/refund", headers=family).status_code == 409 and len(sent) == 3

    # What the payment screen said reaches the model with her next message, and stays out of the scam check's words.
    heard = []
    main.llm.reply = lambda system, tools, messages, text, run_tool: heard.append(text) or "ok"
    client.post("/api/chat", json={"session_id": "shop", "text": "thank you"})
    assert heard[-1].startswith("(The payment screen told Rose:") and heard[-1].endswith("\nthank you")
    assert "stopped by your family" in heard[-1] and "refunded" in heard[-1]  # the family's decisions too, once each
    assert "notes" not in s and s["said"][-1] == "thank you" and s["held"] == []

    # Backing out of the pad, and picking the session up again after a reload.
    main.update_cart(cart, "m1", 1)
    main.start_payment(s, "$3.99")
    restored = client.get("/api/session/shop").json()
    assert restored["cart"] == main.order(cart) and restored["paying"] == "$3.99"
    assert restored["contact"] == {"name": "your family", "phone": None}
    assert client.post("/api/pay/cancel", json={"session_id": "shop"}).json() == {"ok": True}
    assert "paying" not in s and "without paying" in s["notes"][0]
    nobody = client.get("/api/session/nobody").json()
    assert nobody["cart"] == {"lines": [], "total": "$0.00"} and nobody["paying"] is None

    # The raw webhook body is spliced into JSON, so anything but one object is rejected before any network call.
    assert not paypal.verify_webhook({}, '{}, "webhook_id": "attacker"')
    assert not paypal.verify_webhook({}, "[]")

    # A verified capture webhook updates the order it belongs to; a refunded order stays refunded.
    main.paypal.verify_webhook = lambda headers, raw: True
    main.db.execute("UPDATE orders SET status = 'PENDING' WHERE capture_id = 'CAP1'")
    hook = lambda status, cap: client.post("/api/paypal/webhook", json={"event_type": "PAYMENT.CAPTURE.COMPLETED", "resource": {"id": cap, "status": status}})
    assert hook("COMPLETED", "CAP1").json() == {"ok": True} and hook("COMPLETED", "CAP2").json() == {"ok": True}
    assert main.db.execute("SELECT capture_id, status FROM orders WHERE capture_id IS NOT NULL ORDER BY id").fetchall() == [
        ("CAP1", "COMPLETED"), ("CAP2", "REFUNDED")]


    # Her usual spend: the median of orders paid without a hold, once there are three.
    main.db.execute("DELETE FROM orders")
    assert main.usual_spend() is None
    for cents, risk in [(1200, "low"), (1800, "low"), (9900, "high"), (1500, "low")]:
        main.db.execute("INSERT INTO orders (total_cents, status, risk) VALUES (?, 'COMPLETED', ?)", (cents, risk))
    assert main.usual_spend() == 1500


if __name__ == "__main__":
    test()
    print("ok")

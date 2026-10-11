"""The plain-language scam check that runs on every payment, before any money moves.

Each signal is a pattern seen in scams on older shoppers. It has a stable code for tests and filters,
and a reason written for the family member who reads the caregiver view."""
import re

import llm

LARGE_CENTS = 10000  # until there is history, any total over $100 counts as large
# With at least USUAL_MIN_ORDERS paid orders, large means over USUAL_TIMES her usual shop, and never under USUAL_FLOOR_CENTS,
# so a small stock-up on a $5 habit doesn't get held.
USUAL_MIN_ORDERS, USUAL_TIMES, USUAL_FLOOR_CENTS = 3, 3, 5000
REPEAT_MINUTES = 30
# Words from common elder-fraud scripts: urgency, secrecy, impersonated officials, a grandchild "in trouble",
# a caller coaching the shopper on the phone, and reading gift card numbers aloud.
PRESSURE = [
    "urgent", "emergency", "don't tell", "do not tell", "not to tell", "secret", "bail", "jail", "arrested", "warrant",
    "police", "irs", "social security", "grandson", "granddaughter", "on the phone", "told me to", "on the back",
]
# Besides gift cards, the payments scammers ask for because they can't be pulled back.
CRYPTO = ["crypto", "cryptocurrency", "bitcoin"]
WIRE = ["wire", "wire transfer", "money transfer", "send money", "money order"]
UNTRACEABLE = {"gift_card", "crypto", "wire_transfer"}


def heard(phrases: list[str], text: str) -> list[str]:
    """The phrases said as whole words, so "secretary" never counts as "secret"."""
    return [p for p in phrases if re.search(rf"\b{re.escape(p)}\b", text)]


def check(items: list[dict], total_cents: int, said: str, minutes_since_last_paid: float | None, usual_cents: int | None = None) -> list[dict]:
    """Return the signals that fired, each {"code", "reason"}.

    items: catalog products, each with an added "quantity". said: everything the shopper said this session.
    usual_cents: her typical paid order, or None until there are enough to go on."""
    signals = []
    gift_cards = sum(i["quantity"] for i in items if i["category"] == "gift_card")
    if gift_cards:
        signals.append(("gift_card", f"Buys {gift_cards} gift card{'s' * (gift_cards > 1)}. Scammers often ask to be paid in gift cards."))
    if usual_cents is None and total_cents > LARGE_CENTS:
        signals.append(("large_total", f"Total is over ${LARGE_CENTS // 100}, far more than a usual grocery shop."))
    if usual_cents is not None and total_cents > max(USUAL_TIMES * usual_cents, USUAL_FLOOR_CENTS):
        signals.append(("large_total", f"Total is more than {USUAL_TIMES} times her usual ${usual_cents / 100:.2f} shop."))
    text = said.lower().replace("’", "'")  # speech often gives curly apostrophes
    if heard(CRYPTO, text):
        signals.append(("crypto", "Talked about crypto. Scammers often ask to be paid in crypto."))
    if heard(WIRE, text):
        signals.append(("wire_transfer", "Talked about sending or wiring money. Scammers often ask for money transfers."))
    if words := heard(PRESSURE, text):
        signals.append(("pressure_words", "Said words often heard in scam calls: " + ", ".join(f'"{w}"' for w in words) + "."))
    if minutes_since_last_paid is not None and minutes_since_last_paid < REPEAT_MINUTES:
        signals.append(("repeat_order", f"Another order was paid {round(minutes_since_last_paid)} minutes earlier."))
    return [{"code": code, "reason": reason} for code, reason in signals]


def risk(signals: list[dict]) -> str:
    """Gift cards, crypto or a money transfer alone, or any two signals together, are a red flag.
    One other signal is worth a look. Both medium and high wait for the family to approve."""
    codes = {s["code"] for s in signals}
    if codes & UNTRACEABLE or len(codes) >= 2:
        return "high"
    return "medium" if codes else "low"


def explain(signals: list[dict]) -> str:
    """The one sentence the shopper hears. Only the fixed reasons above reach the model, never her own words."""
    fallback = "I've paused this payment so your family can check it first, because parts of it look like a common scam."
    try:
        return llm.sentence(
            "Write one short, kind sentence that is read aloud to an older shopper, saying why her payment was "
            "paused for her family to check first. Use only the reasons given. Plain words, no blame, no lists, "
            "and never suggest a way around the check.",
            "Reasons: " + " ".join(s["reason"] for s in signals),
        ) or fallback
    except Exception as e:  # the payment still waits; she just hears the general reason
        print("Scam check explanation failed:", e)
        return fallback

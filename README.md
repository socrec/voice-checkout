# Voice Checkout

Shop for groceries by talking. Before any money moves, every payment gets a plain-language scam check, and risky ones wait for a trusted family member to approve them.

Voice uses the browser's Web Speech API, so use **desktop Google Chrome**. There is a text box for other browsers.

## Run locally

Needs Python 3.12, Node 20+ and [uv](https://docs.astral.sh/uv/).

```bash
cp .env.example .env              # fill in the keys, see the table below

cd backend
uv venv && uv pip install -r requirements.txt
uv run --env-file ../.env uvicorn main:app --reload --port 8001

cd frontend                       # in a second terminal
npm install
npm run dev                       # open http://localhost:5174 in Chrome
```

The family view is at `http://localhost:5174/caregiver.html#key=<FAMILY_KEY>`, using the `FAMILY_KEY` from `.env`. Open it first and press **Connect PayPal** to save the shopper's sandbox PayPal account.

Tests (no keys needed): `cd backend && uv run python test_main.py`

| Setting | What it is |
|---|---|
| `ANTHROPIC_API_KEY` | For the shopping agent |
| `PAYPAL_CLIENT_ID`, `PAYPAL_CLIENT_SECRET` | The sandbox app's credentials |
| `PAYPAL_WEBHOOK_ID` | From the sandbox app's webhook list. `python paypal.py` prints it. |
| `PAY_PIN` | The four digits the shopper types to pay |
| `FAMILY_KEY` | The secret at the end of the family link. Only someone with it can see orders, approve held payments, refund or connect PayPal. Use a long random value. |
| `TRUSTED_CONTACT` | Optional. The name the shopper hears for whoever approves held payments. Defaults to "your family". |
| `PAYPAL_VAULT_ID` | Optional. The server prints it after Connect PayPal. Setting it keeps PayPal connected after Render's free disk is wiped. |
| `APP_URL` | Optional. Where the frontend lives, so PayPal can send the family back. Defaults to `http://localhost:5174`. |
| `PAYPAL_BASE` | Optional. Defaults to the PayPal sandbox. |

## PayPal sandbox

1. At [developer.paypal.com](https://developer.paypal.com), go to Apps & Credentials (Sandbox), create an app, and put its client ID and secret in `.env`.
2. Under Sandbox accounts, keep one Personal account (buyer) and one Business account (merchant).
3. Add a webhook to the app pointing at `https://<your-api>.onrender.com/api/paypal/webhook`. Webhooks can't reach localhost.
4. Run `uv run --env-file ../.env python paypal.py` from `backend/`. It lists your webhooks; copy the ID into `PAYPAL_WEBHOOK_ID`.

## Deploy on Render

In Render, choose New, then Blueprint, then this repo. `render.yaml` creates the API (`voice-checkout-api`) and the static site (`voice-checkout`), and asks for the secret keys. Render makes up `FAMILY_KEY` itself. Copy it from the API's Environment tab to build the family link, `https://voice-checkout.onrender.com/caregiver.html#key=<FAMILY_KEY>`.

The site sends `/api/*` on to the API. If Render gives the API a different URL, change it in `render.yaml`.

## How a payment works

1. The agent reads the order back. When the shopper says yes, it calls `start_payment`, and a PIN pad opens on her screen with a "Pay $X" button.
2. She types her PIN. It goes straight to the server, never to the model. Five wrong PINs pause paying for 15 minutes.
3. The scam check in `backend/scamcheck.py` looks for gift cards, crypto, wire transfers, totals over $100, scam-call words and repeat orders.
4. Low risk is charged to her saved PayPal account. Anything else is held, and she hears why. The family view shows it at the top with **Approve and pay** and **Reject**. Her screen says the family's answer out loud when it comes.

## Built with

- **PayPal REST APIs** (sandbox): saved PayPal accounts (vault) charged with no buyer present, refunds, OAuth and webhook signature checks.
- **Claude** (Anthropic API): the shopping agent, through tool calls to `search_catalog`, `update_cart`, `read_back_order` and `start_payment`. It also writes the one plain sentence the shopper hears when a payment is held.
- **AG Grid Community**: the family view's order table, with sorting, column filters, search and a red-flags-only switch.
- **FastAPI**, **React + Vite**, **Render**.

The shopper's page follows research on voice interfaces and on design for older adults. The notes are in [frontend/DESIGN.md](frontend/DESIGN.md).

## License

[MIT](LICENSE)

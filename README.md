# Market Expert Gemini

Repository: https://github.com/GIRIRAJSINH25/trading-gemini-

Market Expert Gemini is an Angel One SmartAPI **rule-based trading prototype**. During market hours it reads five-minute candles and current LTP, calculates EMA(9), EMA(21), session VWAP, RSI(14), and ATR(14), and only produces BUY/SELL signals when trend, VWAP, and RSI filters all agree. Its simulated stop is 1% from entry and target 2% from entry; order quantity is capped by configured per-trade risk, maximum quantity, and configured lot size. Otherwise it reports HOLD.

Indicators and rules are transparent and testable; this is not a predictive Gemini/LLM model. News sentiment and macroeconomic analysis are not yet connected to data feeds. Trading cannot guarantee a profit, let alone consistent daily profits. Use paper mode first and independently review the strategy, data, sizing, and broker behavior.

## Setup

1. Copy `.env.example` to `.env`.
2. Set non-secret Angel One settings there: public IPv4 address, exchange, symbol, symbol token, maximum paper-mode risk, quantity cap, and lot size. Configure the exchange session using `ANGELONE_SESSION_START` and `ANGELONE_SESSION_END`.
3. Store credentials without putting them in chat or plaintext configuration: run `py -3 trading-agent.py --configure-local-secrets` from the project folder and enter the secrets in the hidden prompts. The vault encrypts them using Windows DPAPI for the current Windows user and stores the ciphertext under `%LOCALAPPDATA%\MarketExpertGemini`, outside the OneDrive workspace; the bot decrypts them into process memory when it starts. Optional values can be left blank. The current TOTP can be entered at the login prompt instead of storing a TOTP setup secret.
4. If credentials were previously placed in `.env`, run `py -3 trading-agent.py --secure-local-secrets`. This encrypts configured secret values, verifies the vault, then removes those secret settings from `.env`; it prints counts only, not values. The vault cannot be used from another Windows account or directly copied to Linux/cloud hosting. For hosted operation, use a suitable server-side secret manager.
5. Install the required packages:

```powershell
py -3 -m pip install requests python-dotenv pytest
```

6. Configure report delivery with your Gmail account. Set `SMTP_USER` and `REPORT_TO_EMAIL` in `.env` to the intended sender and recipient. Enable 2-step verification on that Gmail account and create an app password. Store the app password with `--configure-local-secrets`, never your normal account password. Do not use real email addresses in `.env.example` or commit `.env`.
   Test email delivery without starting the trading session by running `py -3 trading-agent.py --test-email` from the project folder. If `SMTP_PASSWORD` is not in the encrypted vault, the command prompts for it without displaying the input.
7. For Telegram alerts, create a bot with Telegram's `@BotFather`, enter its token and chat ID through `--configure-local-secrets`, then run `py -3 trading-agent.py --test-telegram` to send a harmless test message without starting trading.
8. Start with `ANGELONE_TRADING_MODE=paper`:

```powershell
py -3 "C:\Users\parma\OneDrive\Desktop\auto trading angelone\trading-agent.py"
```

Paper mode fetches broker market data, account margins, required order margin, and Angel One round-trip charge estimates. It never submits an order. It records simulated positions in the ignored `paper_trades.json` ledger, checks later candle highs/lows for 1% stop or 2% target exits, and closes remaining simulated positions at the configured session end. If both stop and target occur in one candle, it assumes the stop was hit first. Net P/L subtracts Angel One's estimated charges, including platform and statutory fees/taxes; it is still only an estimate and excludes slippage, tax on overall income, and other adjustments. Estimates that fail are reported as unavailable, not treated as zero.

The process must stay running during the trading session to poll prices and send the end-of-session email. Use Windows Task Scheduler to start it each trading day before the configured open, with the project folder as the working directory, and ensure the computer/network remains available through the configured close. Exchange holidays and special sessions are not automatically checked. The email subject and body identify the report as paper trading.

## Private phone dashboard

Run the responsive, read-only dashboard locally:

```powershell
py -3 dashboard.py
```

On this PC, open `http://127.0.0.1:8765`. It reads only the local paper ledger and monitor heartbeat. While the paper monitor is running, it also displays the latest rule-based decision, its EMA/VWAP/RSI/ATR inputs and explanation, plus the corresponding paper-ledger state. It displays paper P/L, wins/losses, an outcome pie chart, trade history, and a downloadable report. These are simulated paper results: no broker order is submitted, and the page does not show real-order execution. The Real mode control is visibly unavailable; there are no order, approval, or mode-changing API endpoints. The server binds only to loopback and must remain running while you use the page.

For access from a phone or another private device without exposing a port to the public internet, install Tailscale on both devices, sign them into the same private tailnet, and ensure your tailnet access policy only permits your own devices. Get the PC's HTTPS MagicDNS host name from Tailscale, start the dashboard allowing that exact name, then use Tailscale Serve as an HTTPS private reverse proxy:

```powershell
py -3 dashboard.py --allowed-host YOUR-PC.YOUR-TAILNET.ts.net
tailscale serve --bg http://127.0.0.1:8765
tailscale serve status
```

Open the HTTPS URL shown by `tailscale serve status` on the phone with Tailscale connected; it works over your private Wi-Fi or another network while both devices are on the tailnet. This is private tailnet access, not a public share link. Do not use Tailscale Funnel or open router/firewall ports for this dashboard. To turn the proxy off later, use `tailscale serve reset`. The PC must remain on with both the dashboard and Tailscale running. Cloud deployment on a separate VM is not configured; the Windows DPAPI credential vault cannot be moved to Linux.

## Export candles and run an offline backtest

With your non-secret Angel One settings in `.env` and credentials in the encrypted local vault, export five-minute history to CSV. Dates/times use IST and must be within Angel One's historical-data availability:

```powershell
py -3 trading-agent.py --export-candles candles.csv --start "2026-09-20 09:15" --end "2026-10-20 15:30"
```

The exporter uses credentials from the encrypted vault and prompts for a current TOTP in the terminal if a TOTP setup secret is not configured. It only requests historical market data; it does not place orders. Do not share credentials, TOTP, or API tokens. The CSV must have `timestamp,open,high,low,close,volume` columns.

Then run an offline replay:

```powershell
py -3 trading-agent.py --backtest candles.csv --risk 100 --quantity 5 --lot-size 1 --round-trip-fee-per-unit 0.5
```

`--risk` is the maximum planned stop risk in INR and `--quantity` is the size cap. The optional fee is the total entry-plus-exit fee estimate per unit; if omitted, net P/L is explicitly reported as unavailable. Signals are calculated using candles up to the previous close and entered at the next candle open. The replay checks OHLC stop/target touches (stop first if both touch), closes positions at session boundaries, and reports trade-level P/L and drawdown. This is a diagnostic, not evidence of future profitability: the backtest does not reproduce actual fills, margin availability, slippage, liquidity, all broker charges, or order failures. Use out-of-sample data and compare with paper results before drawing conclusions.

## Supervised proposals (pre-market session authorization; no order execution)

Set `ANGELONE_TRADING_MODE=supervised` to authorize a bounded proposal session before the configured market open. The terminal first logs in and verifies pre-market available margin, then requires the exact phrase `AUTHORIZE PROPOSAL SESSION YYYY-MM-DD`. The maximum estimated loss per proposal, including estimated stop charges, is 1% of that proposal's broker-estimated required margin. Position size is tested from the configured quantity downward in lot-size steps; a proposal is suppressed if no size fits the 1% cap and available margin. The total session proposal-risk budget is 1% of the available margin verified at authorization time. Proposals stop when reserved risk reaches that budget; there is no separate proposal-count limit. `ANGELONE_ORDER_QUANTITY` and `ANGELONE_LOT_SIZE` constrain sizing. `ANGELONE_MAX_RISK` applies only to paper-mode sizing.

This pre-market authorization applies **only to displaying proposals**. It cannot place, modify, or cancel broker orders. Before authorization and before each eligible proposal, supervised mode now requests a read-only order/trade/position snapshot and blocks when it sees pending or unknown-status orders, non-flat/unknown positions, or reconciliation discrepancies. This is a conservative gate, not a substitute for broker-managed exits or a guarantee that the account is safe; the broker response itself has not yet been validated against your account.

The session budget is based on the margin snapshot at authorization and proposal-risk reservations; it is not measured account loss or a guarantee of maximum real loss. Broker margin/charge estimates can change or be inaccurate. If you place any order yourself, this bot does not know about it or manage its exits. Supervised mode requires an interactive terminal and cannot run unattended. Live order submission remains blocked until broker-side protective exits, position reconciliation, and failure recovery are implemented and verified.

## Read-only broker account reconciliation

To run the offline checklist against local non-secret settings, run `py -3 trading-agent.py --live-readiness`. It reports invalid or missing instrument/order configuration and explicitly marks account-specific permissions, broker instrument identity, exit guarantees, session calendar, outage behavior, and sandbox validation as unverified or blocked. It does not log in, connect to Angel One, or enable live trading.

To verify the configured instrument and read current account state, run `py -3 trading-agent.py --broker-preflight`. It prompts for a current TOTP locally, logs in, checks the exact exchange/trading-symbol/token via Angel One's read-only instrument search, requests account margins, and reads orders/trades/positions. It prints only a bounded summary and the readiness report; it sends no order request. A positive margin and clear broker snapshot do not verify order permissions, stop/target protection, or failure recovery. The command needs the exact broker trading symbol (for example, with its exchange suffix where applicable). A report can only be considered complete after supported broker evidence and operational tests exist; local configuration cannot establish that by itself.

To request the current Angel One order book, trade book, and positions for inspection, run `py -3 trading-agent.py --check-broker-state`. It uses the locally encrypted credentials, asks for a current TOTP in the hidden terminal prompt unless a TOTP setup secret is configured, and makes only GET requests to the broker's read-only account endpoints. It prints a local snapshot and flags pending orders or trade/position differences for manual review. No response is stored or sent to the dashboard; no orders are placed, modified, or cancelled.

The comparison is intentionally conservative: missing fields or malformed responses cause an explicit error; partial fills, rejected orders, pending/unknown orders, and trade/position mismatches require manual review and block supervised proposals. A matching quantity is not proof that protective orders exist, fills were safe, or the account is ready for automation. The official public [Angel One SmartAPI Python SDK](https://github.com/angel-one/smartapi-python) documents these read endpoints; broker responses and account permissions must still be verified by the account holder. This command has not been run against a live account as part of automated tests.

For local execution-journal recovery, run `py -3 trading-agent.py --recover-execution-journal`. It logs in locally with a current TOTP, reads only the broker's order/trade/position endpoints, and asks for a dated confirmation after the account holder checks the Angel One app. It will resolve local intents only when the read-only snapshot is recent, flat, and discrepancy-free. It does not modify or cancel broker orders. Supervised proposal mode checks the journal at startup and before proposals and refuses to continue while any intent is unresolved.

## Run Telegram and paper monitoring without your PC

The Telegram command listener and paper-session monitor can run on an always-on Linux VM. A free VM is not guaranteed to be available or remain eligible for a provider's free tier; account/card verification may be required. Do not put credentials in GitHub, chat, command-line arguments, or a public repository.

1. Create an Ubuntu VM under a cloud provider's free tier, if available. Restrict inbound SSH to your own IP. The bot uses Telegram long polling, so you do not need to open a public Telegram webhook port.
2. From PowerShell on your PC, upload the project files (replace the key path and VM IP):

   ```powershell
   ssh -i "C:\path\to\your-vm-key.key" ubuntu@YOUR_VM_IP "mkdir -p ~/market-expert-gemini"
   scp -i "C:\path\to\your-vm-key.key" trading-agent.py telegram_alert.py requirements.txt .env.example ubuntu@YOUR_VM_IP:~/market-expert-gemini/
   scp -i "C:\path\to\your-vm-key.key" -r deploy ubuntu@YOUR_VM_IP:~/market-expert-gemini/
   ```

3. Connect to the VM and install Python:

   ```powershell
   ssh -i "C:\path\to\your-vm-key.key" ubuntu@YOUR_VM_IP
   ```

   Then run on the Ubuntu VM:

   ```bash
   sudo apt update
   sudo apt install -y python3-venv
   sudo mkdir -p /opt/market-expert-gemini
   sudo cp -r ~/market-expert-gemini/. /opt/market-expert-gemini/
   sudo chown -R ubuntu:ubuntu /opt/market-expert-gemini
   cd /opt/market-expert-gemini
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   cp .env.example .env
   chmod 600 .env
   nano .env
   ```

4. In the VM's `.env`, enter your broker credentials, a **Base32 TOTP setup secret** as `ANGELONE_TOTP_SECRET`, the VM's public IPv4 address, verified instrument settings, Telegram token/chat ID, and Gmail SMTP app password. Never copy `.env` back to your PC or commit it. A current six-digit authenticator code is not the TOTP setup secret. If you do not already securely have that setup secret, do not try to extract it from an authenticator backup; use your broker's official MFA reset/re-enrollment process. Unattended login is not possible until Angel One's MFA setup is configured securely.
5. Verify the Telegram token and private chat ID directly on the VM:

   ```bash
   cd /opt/market-expert-gemini
   .venv/bin/python trading-agent.py --test-telegram
   ```

6. Install and start the always-on Telegram listener and weekday paper-monitor timer:

   ```bash
   sudo cp deploy/systemd/market-expert-telegram.service /etc/systemd/system/
   sudo cp deploy/systemd/market-expert-paper.service /etc/systemd/system/
   sudo cp deploy/systemd/market-expert-paper.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now market-expert-telegram.service
   sudo systemctl enable --now market-expert-paper.timer
   systemctl status market-expert-telegram.service
   systemctl list-timers market-expert-paper.timer
   ```

Send `/start`, `/help`, or `/status` to the Telegram bot. The listener stays online on the VM; the paper monitor runs only during the configured weekday session and uses internet/broker APIs. This does not make markets open 24/7, and the free VM, network, broker API, and Telegram can still become unavailable. Check service logs with `sudo journalctl -u market-expert-telegram.service -f` and `sudo journalctl -u market-expert-paper.service -f`.

## Live orders

Live order submission remains blocked. Supervised mode is proposal-only and its acknowledgement never sends an order. The separate `execution_safety.py` component implements a durable local intent journal: unique intent keys reject duplicate local intents, ambiguous submissions are never retried by the journal, partial/full fills are recorded as states needing follow-up, and its restart-recovery method marks unfinished work for manual review. The journal is wired into supervised proposal gates but is not connected to an order-submission path and does not itself prove whether a request reached Angel One.

The current implementation still does not attach and verify broker-managed stop-loss/target exits, reconcile positions after bot-submitted fills, provide verified linked/OCO exit semantics, or reliably close positions at the segment's session end. Those behaviors require a broker-supported test environment and validation of the exact account/product/segment; this project has not performed that validation. Passing mocked tests is not broker validation. Do not bypass the live-order safeguard. Live account margins can be queried, but no real orders are placed and this bot cannot produce a real-execution report yet.

The strategy is not yet calibrated or separately validated for equity, futures, options, currency, and commodity contracts. It operates on one manually configured exchange/symbol/token at a time and does not scan every instrument available in Angel One. Verify lot sizes, prices, tick sizes, margin, session times, and expiry details for each contract. Never commit `.env`.

## Tests

```powershell
py -3 -m pytest -q
```

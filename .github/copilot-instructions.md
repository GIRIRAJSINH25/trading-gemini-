# Market Expert Gemini — Project Instructions

## Project scope

- This is a Python Angel One SmartAPI **paper-trading prototype** with Telegram commands and email reports.
- Read `README.md` before changing trading, deployment, authentication, or reporting behavior.
- The strategy is rule-based (EMA, VWAP, RSI, ATR); do not describe it as a trained Gemini model or promise profitability.

## Trading safety

- Preserve paper mode as the default. Do not enable, add, or trigger live order placement without explicit user authorization and verified broker-managed protective exits.
- Never remove risk limits, the 1% stop-loss, intraday-only restrictions, margin checks, stale-data checks, or market-session checks as a shortcut.
- Treat broker margin and fee/tax responses as estimates. If required data is missing or an estimate fails, report the error and block new entries; never substitute zero or fabricate results.
- Do not claim that a paper fill, stop, target, or session close was executed by a broker.
- Changes to order sizing, indicators, paper fills, fees, sessions, authentication, or report contents require focused tests.

## Credentials and privacy

- Never read credentials aloud, print them, add them to source, tests, logs, example files, commits, or documentation.
- `.env` contains secrets and must remain local and ignored. Use placeholder values only in `.env.example`.
- Never ask the user to paste API keys, passwords, current TOTP codes, TOTP setup secrets, SMTP app passwords, Telegram tokens, or private SSH keys into chat.
- Do not transmit secrets to third parties except as required by the explicitly configured provider API call.

## Code and validation

- Keep changes surgical and follow the existing Python style and module boundaries.
- Keep Telegram bot functionality restricted to the configured private chat; never let Telegram commands authorize or submit live trades.
- Run the focused tests for changed behavior, then the full suite when practical:

  ```powershell
  py -3 -m pytest -q
  py -3 -m py_compile trading-agent.py telegram_alert.py
  ```

- Update `README.md` when setup, configuration, deployment, or user-visible behavior changes.
- Do not deploy services, access a broker account, send emails, or place trades without an explicit user request and required authorization.

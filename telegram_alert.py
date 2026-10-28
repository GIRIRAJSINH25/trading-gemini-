import os
import threading

import requests


def build_trade_keyboard():
    """Build a Telegram inline keyboard with buy, sell, and hold options."""
    return {
        "inline_keyboard": [
            [
                {"text": "BUY", "callback_data": "BUY"},
                {"text": "SELL", "callback_data": "SELL"},
                {"text": "HOLD", "callback_data": "HOLD"},
            ]
        ]
    }


def send_telegram_message(message: str, reply_markup=None):
    """Send a Telegram message, optionally with inline buttons."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id or bot_token.startswith("YOUR_") or chat_id.startswith("YOUR_"):
        print("Telegram not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID.")
        return False

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup

    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        print(f"Telegram message request failed ({type(exc).__name__}).")
        return False
    except ValueError:
        print("Telegram returned an invalid JSON response.")
        return False

    if not isinstance(data, dict) or not data.get("ok"):
        description = data.get("description", "Telegram rejected the message.") if isinstance(data, dict) else "Telegram returned an invalid response."
        print(f"Telegram message failed: {description}")
        return False
    return True


def discover_private_chat_id(bot_token: str):
    """Return the chat ID from an unconsumed private /start update, if available."""
    bot_token = (bot_token or "").strip()
    if not bot_token or bot_token.startswith("YOUR_"):
        raise ValueError("A valid Telegram bot token is required.")

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    try:
        response = requests.get(url, params={"timeout": 0}, timeout=10)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram update request failed ({type(exc).__name__}).") from exc
    except ValueError as exc:
        raise RuntimeError("Telegram returned an invalid JSON response.") from exc

    if not isinstance(payload, dict) or not payload.get("ok") or not isinstance(payload.get("result"), list):
        raise RuntimeError("Telegram did not return a valid updates list.")

    for update in payload["result"]:
        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if (
            isinstance(chat, dict)
            and chat.get("type") == "private"
            and isinstance(message.get("text"), str)
            and bool(message["text"].strip())
            and message["text"].strip().split(maxsplit=1)[0].partition("@")[0].lower() == "/start"
        ):
            chat_id = chat.get("id")
            if chat_id is not None:
                return str(chat_id)
    return None


def send_telegram_test_message():
    """Send a harmless delivery test to the configured Telegram chat."""
    sent = send_telegram_message(
        "Market Expert Gemini: Telegram delivery test. No trade was placed."
    )
    if not sent:
        raise RuntimeError("Telegram test message was not delivered. Check the bot token, chat ID, and bot access.")
    print("Telegram test message sent successfully.")
    return True


def process_status_updates(updates, allowed_chat_id, status_provider):
    """Reply only to supported commands from the configured Telegram chat."""
    next_offset = None
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = max(next_offset or 0, update_id + 1)

        message = update.get("message")
        if not isinstance(message, dict):
            continue
        chat = message.get("chat")
        if not isinstance(chat, dict) or str(chat.get("id")) != str(allowed_chat_id):
            continue

        text = message.get("text")
        if not isinstance(text, str):
            continue
        parts = text.strip().split(maxsplit=1)
        if not parts:
            continue
        command = parts[0].partition("@")[0].lower()
        if command == "/status":
            reply = status_provider()
        elif command in {"/start", "/help"}:
            reply = (
                "<b>Market Expert Gemini</b>\n"
                "Telegram is connected.\n"
                "/status — show bot and paper-ledger status\n"
                "/help — show available commands\n\n"
                "This Telegram service does not place trades. "
                "The market-trading process must be started separately."
            )
        else:
            continue
        if not send_telegram_message(reply):
            print("Telegram command reply could not be delivered.")

    return next_offset


def listen_for_status_commands(stop_event: threading.Event, status_provider):
    """Keep a Telegram command listener online until its stop event is set."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    allowed_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if (
        not bot_token
        or not allowed_chat_id
        or bot_token.startswith("YOUR_")
        or allowed_chat_id.startswith("YOUR_")
    ):
        raise RuntimeError("Configure valid TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env.")

    url = f"https://api.telegram.org/bot{bot_token}/getUpdates"
    next_offset = None
    print("Telegram listener online. Send /status or /help in your private bot chat. Press Ctrl+C to stop.")
    while not stop_event.is_set():
        params = {"timeout": 20}
        if next_offset is not None:
            params["offset"] = next_offset
        try:
            response = requests.get(url, params=params, timeout=25)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not payload.get("ok") or not isinstance(payload.get("result"), list):
                raise RuntimeError("Telegram returned an invalid updates response.")
            offset = process_status_updates(payload["result"], allowed_chat_id, status_provider)
            if offset is not None:
                next_offset = offset
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            print(f"Telegram status monitoring error ({type(exc).__name__}); retrying.")
            stop_event.wait(5)

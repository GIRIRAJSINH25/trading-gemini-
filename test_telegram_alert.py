import telegram_alert


def test_discover_private_chat_id_uses_only_private_start_updates(monkeypatch):
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "ok": True,
                "result": [
                    {
                        "update_id": 1,
                        "message": {
                            "chat": {"id": 555, "type": "private"},
                            "text": "/start",
                        },
                    },
                    {
                        "update_id": 2,
                        "message": {
                            "chat": {"id": -10, "type": "group"},
                            "text": "/start",
                        },
                    },
                ],
            }

    monkeypatch.setattr(telegram_alert.requests, "get", lambda *args, **kwargs: Response())
    assert telegram_alert.discover_private_chat_id("secret-token") == "555"


def test_status_update_answers_only_allowlisted_chat(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telegram_alert,
        "send_telegram_message",
        lambda message: sent.append(message) or True,
    )
    offset = telegram_alert.process_status_updates(
        [
            {"update_id": 12, "message": {"chat": {"id": 1001}, "text": "/status"}},
            {"update_id": 13, "message": {"chat": {"id": 2002}, "text": "/status"}},
            {"update_id": 14, "message": {"chat": {"id": 1001}, "text": "/start"}},
        ],
        1001,
        lambda: "PAPER RUNNING",
    )
    assert offset == 15
    assert sent == [
        "PAPER RUNNING",
        "<b>Market Expert Gemini</b>\n"
        "Telegram is connected.\n"
        "/status — show bot and paper-ledger status\n"
        "/help — show available commands\n\n"
        "This Telegram service does not place trades. "
        "The market-trading process must be started separately.",
    ]


def test_status_command_supports_bot_username_suffix(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telegram_alert,
        "send_telegram_message",
        lambda message: sent.append(message) or True,
    )
    telegram_alert.process_status_updates(
        [{
            "update_id": 20,
            "message": {
                "chat": {"id": "1001"},
                "text": "/status@market_expert_bot",
            },
        }],
        "1001",
        lambda: "PAPER RUNNING",
    )
    assert sent == ["PAPER RUNNING"]


def test_empty_or_non_text_messages_are_ignored(monkeypatch):
    sent = []
    monkeypatch.setattr(
        telegram_alert,
        "send_telegram_message",
        lambda message: sent.append(message) or True,
    )
    telegram_alert.process_status_updates(
        [
            {"update_id": 21, "message": {"chat": {"id": 1001}, "text": "  "}},
            {"update_id": 22, "message": {"chat": {"id": 1001}}},
        ],
        1001,
        lambda: "PAPER RUNNING",
    )
    assert sent == []


def test_send_message_posts_json_and_does_not_log_token(monkeypatch, capsys):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secret-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")
    captured = {}

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"ok": True}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return Response()

    monkeypatch.setattr(telegram_alert.requests, "post", fake_post)
    assert telegram_alert.send_telegram_message("hello", telegram_alert.build_trade_keyboard())
    assert captured["kwargs"]["json"]["chat_id"] == "12345"
    assert captured["kwargs"]["json"]["reply_markup"] == telegram_alert.build_trade_keyboard()
    assert "secret-token" not in capsys.readouterr().out


def test_test_message_fails_explicitly_when_telegram_is_unconfigured(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    try:
        telegram_alert.send_telegram_test_message()
    except RuntimeError as exc:
        assert "was not delivered" in str(exc)
    else:
        raise AssertionError("Expected an explicit error when Telegram is unconfigured.")
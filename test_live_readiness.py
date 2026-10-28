from live_readiness import build_live_readiness_report


def valid_config():
    return {
        "client_public_ip": "203.0.113.10",
        "credentials_configured": True,
        "trading_mode": "paper",
        "place_order_enabled": False,
        "exchange": "NSE",
        "symbol": "RELIANCE",
        "symbol_token": "2885",
        "instrument_type": "EQUITY",
        "product_type": "INTRADAY",
        "tick_size": "0.05",
        "lot_size": "1",
        "quantity": "2",
        "max_daily_trades": "3",
        "order_type": "MARKET",
        "order_validity": "DAY",
        "session_start": "09:15",
        "session_end": "15:30",
        "stop_loss_percent": 0.01,
        "target_percent": 0.02,
    }


def test_readiness_report_never_approves_live_trading_from_local_config():
    report = build_live_readiness_report(valid_config())

    assert report["live_ready"] is False
    assert report["submission_enabled"] is False
    assert report["summary"].startswith("Not ready")
    assert any(check["status"] == "BLOCKED" for check in report["checks"])
    assert any(
        check["name"] == "broker_managed_linked_protective_exits"
        and check["status"] == "BLOCKED"
        for check in report["checks"]
    )


def test_readiness_report_marks_invalid_config_failures():
    config = valid_config()
    config.update({
        "client_public_ip": "not-an-ip",
        "symbol_token": "YOUR_SYMBOL_TOKEN",
        "tick_size": "0",
        "quantity": "2.5",
        "lot_size": "10",
        "order_type": "",
        "session_start": "15:30",
        "session_end": "09:15",
        "stop_loss_percent": 0.02,
        "place_order_enabled": True,
    })

    report = build_live_readiness_report(config)
    failures = {check["name"] for check in report["checks"] if check["status"] == "FAIL"}

    assert {
        "client_public_ipv4",
        "instrument_identity_configured",
        "instrument_contract_details",
        "quantity_matches_lot_size",
        "order_type_and_validity",
        "session_window",
        "risk_limits",
        "live_submission_switch",
    }.issubset(failures)


def test_readiness_report_does_not_claim_broker_identity_verification():
    report = build_live_readiness_report(valid_config())

    check = next(
        item for item in report["checks"]
        if item["name"] == "instrument_identity_broker_verified"
    )
    assert check["status"] == "UNVERIFIED"


def test_readiness_report_does_not_treat_local_credentials_as_authenticated():
    report = build_live_readiness_report(valid_config())

    check = next(
        item for item in report["checks"]
        if item["name"] == "broker_authentication_and_account_permissions"
    )
    assert check["status"] == "UNVERIFIED"
    assert report["submission_enabled"] is False


def test_live_readiness_can_include_read_only_broker_evidence_but_stays_blocked():
    config = valid_config()
    config.update({
        "broker_authenticated": True,
        "broker_instrument_verified": True,
        "broker_margin_available": True,
        "broker_state_status": "NO_DISCREPANCY_DETECTED",
    })

    report = build_live_readiness_report(config)
    checks = {check["name"]: check["status"] for check in report["checks"]}

    assert checks["broker_authentication_and_account_permissions"] == "PASS"
    assert checks["instrument_identity_broker_verified"] == "PASS"
    assert checks["account_margin_snapshot"] == "PASS"
    assert checks["read_only_broker_order_and_position_state"] == "PASS"
    assert report["live_ready"] is False
    assert report["submission_enabled"] is False

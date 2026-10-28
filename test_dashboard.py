import json
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import dashboard


def test_dashboard_summary_and_report_use_only_paper_ledger(tmp_path):
    ledger = tmp_path / "paper_trades.json"
    ledger.write_text(
        json.dumps([
            {
                "symbol": "AAA",
                "exchange": "NSE",
                "action": "BUY",
                "status": "CLOSED",
                "entry_time": "2026-10-20T10:00:00+05:30",
                "entry_price": 100,
                "exit_price": 102,
                "quantity": 1,
                "gross_pnl": 2,
                "net_pnl": 1.5,
            },
            {
                "symbol": "BBB",
                "action": "SELL",
                "status": "CLOSED",
                "entry_time": "2026-10-20T11:00:00+05:30",
                "entry_price": 200,
                "exit_price": 202,
                "quantity": 2,
                "gross_pnl": -4,
                "net_pnl": -5,
            },
            {
                "symbol": "CCC",
                "action": "BUY",
                "status": "OPEN",
                "entry_time": "2026-10-20T12:00:00+05:30",
                "entry_price": 50,
                "quantity": 1,
            },
        ]),
        encoding="utf-8",
    )
    now = datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc)

    result = dashboard.build_dashboard_data(
        ledger,
        tmp_path / "missing-status.json",
        now,
    )

    assert result["mode"] == "paper"
    assert result["closed_count"] == 2
    assert result["open_count"] == 1
    assert result["wins"] == 1
    assert result["losses"] == 1
    assert result["win_rate"] == 50
    assert result["gross_pnl"] == -2
    assert result["net_pnl"] == -3.5
    assert [trade["symbol"] for trade in result["trades"]] == ["CCC", "BBB", "AAA"]
    assert "does not connect to broker positions or submit" in result["report"]


def test_dashboard_includes_latest_paper_decision_from_monitor_status(tmp_path):
    status = tmp_path / "status.json"
    status.write_text(
        json.dumps({
            "state": "running",
            "updated_at": "2026-10-20T13:00:00+05:30",
            "latest_decision": {
                "symbol": "AAA",
                "action": "HOLD",
                "reasoning": "EMA, VWAP, and RSI do not align.",
                "market_indicators": {"ema_short": 100.1, "vwap": 99.8},
                "paper_execution": "No broker order was submitted.",
            },
        }),
        encoding="utf-8",
    )

    result = dashboard.build_dashboard_data(
        tmp_path / "missing-ledger.json",
        status,
        datetime(2026, 10, 20, 13, 0, tzinfo=timezone.utc),
    )

    assert result["latest_decision"]["symbol"] == "AAA"
    assert result["latest_decision"]["action"] == "HOLD"
    assert result["latest_decision"]["market_indicators"]["ema_short"] == 100.1
    assert result["latest_decision"]["paper_execution"] == "No broker order was submitted."


def test_dashboard_has_read_only_paper_ui_and_no_order_action():
    assert "Real mode · status" in dashboard.PAGE
    assert 'id="realMode"' in dashboard.PAGE
    assert 'id="realNotice"' in dashboard.PAGE
    assert "Real mode is not enabled." in dashboard.PAGE
    assert "Paper portfolio" in dashboard.PAGE
    assert "Latest paper decision" in dashboard.PAGE
    assert "Paper simulation:" in dashboard.PAGE
    assert "Download report" in dashboard.PAGE
    assert "fetch(\"/api/dashboard\"" in dashboard.PAGE


def test_dashboard_rejects_invalid_ledger(tmp_path):
    ledger = tmp_path / "bad.json"
    ledger.write_text('{"not":"a list"}', encoding="utf-8")

    with pytest.raises(ValueError, match="JSON list"):
        dashboard.build_dashboard_data(ledger, tmp_path / "status.json")


@pytest.fixture
def dashboard_server(monkeypatch, tmp_path):
    ledger = tmp_path / "paper_trades.json"
    ledger.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(dashboard, "LEDGER_PATH", ledger)
    monkeypatch.setattr(dashboard, "STATUS_PATH", tmp_path / "status.json")
    server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard.DashboardHandler)
    server.allowed_hosts = set()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()
    worker.join(timeout=2)


def test_dashboard_serves_html_and_json_read_only(dashboard_server):
    with urlopen(dashboard_server + "/") as response:
        page = response.read().decode("utf-8")
        assert response.headers["X-Frame-Options"] == "DENY"
        assert "Market Expert Gemini" in page

    with urlopen(dashboard_server + "/api/dashboard") as response:
        data = json.loads(response.read())
        assert data["mode"] == "paper"
        assert data["trades"] == []

    with pytest.raises(HTTPError) as error:
        urlopen(Request(dashboard_server + "/api/dashboard", method="POST"))
    assert error.value.code == 405


def test_dashboard_rejects_untrusted_host_header(dashboard_server):
    request = Request(
        dashboard_server + "/api/dashboard",
        headers={"Host": "attacker.example"},
    )
    with pytest.raises(HTTPError) as error:
        urlopen(request)
    assert error.value.code == 403

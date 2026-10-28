import argparse
import json
import os
import ipaddress
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
LEDGER_PATH = ROOT / "paper_trades.json"
STATUS_PATH = ROOT / "paper_monitor_status.json"

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#101827">
<title>Market Expert Gemini</title>
<style>
:root{color-scheme:dark;--bg:#0b1220;--panel:#121d2e;--panel2:#18263b;--line:#263750;--text:#ecf3ff;--muted:#a5b5ca;--green:#45d39b;--red:#ff6f7d;--blue:#72a7ff;--amber:#ffc66b}
*{box-sizing:border-box}body{margin:0;background:linear-gradient(160deg,#0e1a2a,var(--bg) 45%);color:var(--text);font:15px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1120px;margin:auto;padding:24px 18px 48px}.top{display:flex;gap:20px;justify-content:space-between;align-items:center;flex-wrap:wrap}
h1{font-size:clamp(24px,5vw,34px);letter-spacing:-.04em;margin:0}.sub,.muted{color:var(--muted)}.sub{margin:5px 0 0}
.badge{display:inline-flex;align-items:center;gap:8px;border:1px solid #237456;background:#123b32;color:#8cf0c0;border-radius:999px;padding:8px 13px;font-weight:700}.dot{height:8px;width:8px;border-radius:50%;background:var(--green)}
.notice{margin:22px 0;padding:14px 16px;background:#30261a;border:1px solid #6a512c;border-radius:14px;color:#ffe0a7}.notice[hidden]{display:none}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:13px}.card{background:linear-gradient(150deg,var(--panel2),var(--panel));border:1px solid var(--line);border-radius:16px;padding:17px;min-width:0}.label{color:var(--muted);font-size:13px}.value{font-size:25px;font-weight:750;margin-top:7px;overflow-wrap:anywhere}.positive{color:var(--green)}.negative{color:var(--red)}
.section{margin-top:20px}.section-head{display:flex;justify-content:space-between;align-items:center;gap:10px;margin:0 0 12px}h2{font-size:18px;margin:0}.modes{display:flex;gap:8px}.mode{border:1px solid var(--line);background:#19273a;color:var(--muted);border-radius:10px;padding:9px 13px;font-weight:700}.mode.active{border-color:#368466;background:#143c32;color:#a2f5cb}.mode:disabled{opacity:.6;cursor:not-allowed}
.two{display:grid;grid-template-columns:1fr 1fr;gap:14px}.chart-layout{display:flex;gap:22px;align-items:center;flex-wrap:wrap}.pie{height:170px;width:170px;border-radius:50%;background:conic-gradient(#263750 0deg 360deg);position:relative;flex:none}.pie:after{content:"";position:absolute;inset:28px;background:var(--panel);border-radius:50%}.pie-center{position:absolute;inset:0;z-index:1;display:grid;place-content:center;text-align:center}.pie-center strong{font-size:21px}.legend{display:grid;gap:10px;color:var(--muted)}.key{display:inline-block;height:10px;width:10px;border-radius:3px;margin-right:7px}.key.win{background:var(--green)}.key.loss{background:var(--red)}.key.open{background:var(--blue)}
.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;min-width:650px}th,td{text-align:left;padding:11px 10px;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-size:12px;font-weight:600}td{font-size:13px}.pill{border-radius:999px;padding:3px 8px;font-weight:700;font-size:11px}.pill.buy{background:#143c32;color:#91ecc2}.pill.sell{background:#48242b;color:#ff9da5}.pill.open{background:#1b3150;color:#a6c8ff}.pill.closed{background:#293448;color:#c7d3e3}
.btn{background:#20324b;border:1px solid #395371;color:var(--text);padding:9px 12px;border-radius:9px;font-weight:700;cursor:pointer}.btn:hover{background:#294261}.report{white-space:pre-wrap;background:#0d1725;padding:15px;border-radius:10px;color:#d5e2f4;max-height:300px;overflow:auto;font:13px/1.6 ui-monospace,Consolas,monospace}
footer{color:var(--muted);font-size:12px;margin-top:20px}.error{color:var(--red)}@media(max-width:760px){main{padding:19px 13px 36px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.two{grid-template-columns:1fr}.chart-layout{gap:15px}.pie{height:145px;width:145px}.section-head{align-items:flex-start;flex-direction:column}}@media(max-width:380px){.grid{gap:8px}.card{padding:13px}.value{font-size:21px}}
</style>
</head>
<body><main>
<header class="top">
  <div><h1>Market Expert Gemini</h1><p class="sub">Paper portfolio · private read-only dashboard</p></div>
  <span class="badge"><i class="dot"></i><span id="monitor">Paper mode</span></span>
</header>
<div class="notice"><strong>Real trading is unavailable.</strong> This dashboard cannot submit, modify, or cancel Angel One orders. Paper results are simulated and are not account P/L.</div>
<div class="notice" id="realNotice" role="status" hidden><strong>Real mode is not enabled.</strong> Selecting this only displays this notice; no broker connection or order action is available. The dashboard remains in paper-ledger view.</div>
<section class="section">
  <div class="section-head"><h2>Mode</h2><div class="modes"><span class="mode active" aria-current="true">Paper ledger</span><button class="mode" id="realMode" type="button" aria-controls="realNotice">Real mode · status</button></div></div>
  <div class="grid">
    <article class="card"><div class="label">Closed paper P/L (net if known)</div><div id="pnl" class="value">—</div></article>
    <article class="card"><div class="label">Closed trades</div><div id="closedCount" class="value">—</div></article>
    <article class="card"><div class="label">Win rate</div><div id="winRate" class="value">—</div></article>
    <article class="card"><div class="label">Open paper positions</div><div id="openCount" class="value">—</div></article>
  </div>
</section>
<section class="section card">
  <div class="section-head"><h2>Latest paper decision</h2><span id="decisionUpdated" class="muted">Waiting for analysis</span></div>
  <p><strong id="decisionSummary">No paper decision recorded yet.</strong></p>
  <p id="decisionReason" class="muted">Start the paper monitor to see its latest rule-based decision.</p>
  <p><strong>Indicators:</strong> <span id="decisionIndicators" class="muted">Unavailable</span></p>
  <p><strong>Paper simulation:</strong> <span id="decisionExecution" class="muted">No broker order was submitted.</span></p>
</section>
<section class="section two">
  <article class="card"><div class="section-head"><h2>Trade outcomes</h2><span id="pieCaption" class="muted"></span></div><div class="chart-layout"><div class="pie" id="pie"><div class="pie-center"><strong id="pieTotal">0</strong><span class="muted">trades</span></div></div><div class="legend" id="legend"></div></div></article>
  <article class="card"><div class="section-head"><h2>Paper performance</h2></div><div class="label">Estimated gross P/L</div><div id="grossPnl" class="value">—</div><div class="label" style="margin-top:14px">Estimated net P/L</div><div id="netPnl" class="value">—</div><p class="muted" id="feeNote"></p></article>
</section>
<section class="section card">
  <div class="section-head"><h2>Paper trade history</h2><button class="btn" id="download" type="button">Download report</button></div>
  <div class="table-wrap"><table><thead><tr><th>Entered</th><th>Instrument</th><th>Side</th><th>Qty</th><th>Entry</th><th>Exit / status</th><th>Net P/L</th></tr></thead><tbody id="trades"></tbody></table></div>
  <p id="empty" class="muted" hidden>No paper trades recorded yet.</p>
</section>
<section class="section card"><div class="section-head"><h2>Paper report</h2><span id="updated" class="muted"></span></div><div id="report" class="report">Loading…</div></section>
<footer>Refreshes local paper-ledger data every 30 seconds. No live account position data is queried. Do not share this dashboard link outside your private devices.</footer>
</main>
<script>
const el=id=>document.getElementById(id);
function fmtMoney(value){if(value===null||value===undefined)return"Unavailable";return"₹"+Number(value).toLocaleString("en-IN",{minimumFractionDigits:2,maximumFractionDigits:2})}
function setPnl(node,value){node.textContent=fmtMoney(value);node.classList.toggle("positive",Number(value)>0);node.classList.toggle("negative",Number(value)<0)}
function safeDate(value){if(!value)return"—";const d=new Date(value);return Number.isNaN(d.getTime())?String(value):d.toLocaleString("en-IN",{dateStyle:"short",timeStyle:"short"})}
function render(data){
  el("monitor").textContent=data.monitor;
  const decision=data.latest_decision;
  if(decision){
    el("decisionSummary").textContent=`${decision.symbol||"—"} ${decision.exchange||""} · ${decision.action||"HOLD"}`;
    el("decisionReason").textContent=decision.reasoning||"No explanation recorded.";
    el("decisionUpdated").textContent="Updated "+safeDate(decision.updated_at);
    const indicators=decision.market_indicators||{};
    el("decisionIndicators").textContent=["close","ema_short","ema_long","vwap","rsi","atr"].map(key=>`${key.replace("_"," ").toUpperCase()}: ${indicators[key]??"—"}`).join(" · ");
    el("decisionExecution").textContent=decision.paper_execution||"No broker order was submitted.";
  }else{
    el("decisionSummary").textContent="No paper decision recorded yet.";
    el("decisionReason").textContent="Start the paper monitor to see its latest rule-based decision.";
    el("decisionUpdated").textContent="Waiting for analysis";
    el("decisionIndicators").textContent="Unavailable";
    el("decisionExecution").textContent="No broker order was submitted.";
  }
  el("closedCount").textContent=data.closed_count;
  el("openCount").textContent=data.open_count;
  el("winRate").textContent=data.win_rate===null?"—":data.win_rate.toFixed(1)+"%";
  setPnl(el("pnl"),data.net_pnl);
  setPnl(el("grossPnl"),data.gross_pnl);
  setPnl(el("netPnl"),data.net_pnl);
  el("feeNote").textContent=data.net_pnl===null?"Net P/L is unavailable when fees are unknown or no trades have closed.":"Net P/L subtracts paper-ledger estimated fees only.";
  const wins=data.wins,losses=data.losses,open=data.open_count,total=wins+losses+open;
  el("pieTotal").textContent=total;
  el("pieCaption").textContent=total?`${wins} wins · ${losses} losses · ${open} open`:"No trades yet";
  const w=total?wins/total*360:0,l=total?losses/total*360:0;
  el("pie").style.background=total?`conic-gradient(#45d39b 0deg ${w}deg,#ff6f7d ${w}deg ${w+l}deg,#72a7ff ${w+l}deg 360deg)`:"conic-gradient(#263750 0deg 360deg)";
  el("legend").innerHTML=`<span><i class="key win"></i>Wins: ${wins}</span><span><i class="key loss"></i>Losses: ${losses}</span><span><i class="key open"></i>Open: ${open}</span>`;
  const tbody=el("trades");tbody.replaceChildren();
  for(const trade of data.trades){
    const row=document.createElement("tr");
    const values=[safeDate(trade.entry_time),`${trade.symbol||"—"} ${trade.exchange||""}`,trade.action||"—",trade.quantity??"—",fmtMoney(trade.entry_price),trade.status==="CLOSED"?`${fmtMoney(trade.exit_price)} · ${trade.exit_reason||"Closed"}`:"OPEN",trade.status==="CLOSED"?fmtMoney(trade.net_pnl):"—"];
    for(let i=0;i<values.length;i++){const cell=document.createElement("td");cell.textContent=values[i];if(i===2){const pill=document.createElement("span");pill.className=`pill ${(trade.action||"").toLowerCase()}`;pill.textContent=values[i];cell.replaceChildren(pill)}else if(i===5){const pill=document.createElement("span");pill.className=`pill ${(trade.status||"").toLowerCase()}`;pill.textContent=values[i];cell.replaceChildren(pill)}row.appendChild(cell)}
    tbody.appendChild(row);
  }
  el("empty").hidden=data.trades.length>0;
  el("updated").textContent="Updated "+safeDate(data.updated_at);
  el("report").textContent=data.report;
}
async function refresh(){
  try{const response=await fetch("/api/dashboard",{cache:"no-store"});if(!response.ok)throw new Error("Dashboard data unavailable ("+response.status+").");render(await response.json())}
  catch(error){el("report").textContent=error.message;el("report").classList.add("error")}
}
el("download").addEventListener("click",async()=>{const response=await fetch("/api/report",{cache:"no-store"});if(!response.ok){el("report").textContent="Could not generate report.";return}const blob=await response.blob();const url=URL.createObjectURL(blob);const link=document.createElement("a");link.href=url;link.download="market-expert-paper-report.txt";link.click();URL.revokeObjectURL(url)});
el("realMode").addEventListener("click",()=>{el("realNotice").hidden=false;el("realNotice").scrollIntoView({behavior:"smooth",block:"nearest"})});
refresh();setInterval(refresh,30000);
</script></body></html>"""


def build_dashboard_data(ledger_path=LEDGER_PATH, status_path=STATUS_PATH, now=None):
    current_time = now or datetime.now().astimezone()
    try:
        if Path(ledger_path).exists():
            trades = json.loads(Path(ledger_path).read_text(encoding="utf-8"))
        else:
            trades = []
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read paper trade history: {exc}") from exc
    if not isinstance(trades, list) or any(not isinstance(trade, dict) for trade in trades):
        raise ValueError("Paper trade history must be a JSON list of trade records.")

    closed = [trade for trade in trades if trade.get("status") == "CLOSED"]
    opened = [trade for trade in trades if trade.get("status") == "OPEN"]
    known_net = all(trade.get("net_pnl") is not None for trade in closed)
    net_pnl = (
        round(sum(float(trade["net_pnl"]) for trade in closed), 2)
        if closed and known_net
        else None
    )
    gross_pnl = round(sum(float(trade.get("gross_pnl", 0)) for trade in closed), 2)
    wins = sum(float(trade.get("net_pnl") if trade.get("net_pnl") is not None else trade.get("gross_pnl", 0)) > 0 for trade in closed)
    losses = sum(float(trade.get("net_pnl") if trade.get("net_pnl") is not None else trade.get("gross_pnl", 0)) < 0 for trade in closed)
    win_rate = round(wins / len(closed) * 100, 1) if closed else None

    monitor = "Paper ledger loaded"
    updated_at = current_time.isoformat()
    try:
        if Path(status_path).exists():
            status = json.loads(Path(status_path).read_text(encoding="utf-8"))
            updated_at = status.get("updated_at", updated_at)
            monitor = f"Paper monitor: {status.get('state', 'unknown')}"
            latest_decision = status.get("latest_decision")
            if not isinstance(latest_decision, dict):
                latest_decision = None
        else:
            latest_decision = None
    except (OSError, json.JSONDecodeError, AttributeError):
        monitor = "Paper monitor status unavailable"
        latest_decision = None

    report_lines = [
        "Market Expert Gemini — PAPER TRADING REPORT",
        f"Generated: {current_time.isoformat()}",
        f"Paper trades recorded: {len(trades)}",
        f"Closed: {len(closed)} | Open: {len(opened)}",
        f"Wins: {wins} | Losses: {losses}",
        f"Win rate: {win_rate:.1f}%" if win_rate is not None else "Win rate: unavailable",
        f"Gross realized paper P/L: Rs {gross_pnl:.2f}",
        f"Estimated net paper P/L: Rs {net_pnl:.2f}" if net_pnl is not None else "Estimated net paper P/L: unavailable (fees unknown or no closed trades)",
        "",
        "Paper performance is simulated; it is not a broker account statement or a guarantee of actual results.",
        "This dashboard does not connect to broker positions or submit, modify, or cancel orders.",
    ]
    return {
        "mode": "paper",
        "monitor": monitor,
        "updated_at": updated_at,
        "latest_decision": latest_decision,
        "closed_count": len(closed),
        "open_count": len(opened),
        "wins": wins,
        "losses": losses,
        "win_rate": win_rate,
        "gross_pnl": gross_pnl,
        "net_pnl": net_pnl,
        "trades": trades[-200:][::-1],
        "report": "\n".join(report_lines),
    }


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "MarketExpertDashboard"

    def _host_is_allowed(self):
        host_header = self.headers.get("Host", "")
        try:
            hostname = urlparse("//" + host_header).hostname
        except ValueError:
            return False
        if not hostname:
            return False
        hostname = hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(hostname)
            if address.is_loopback:
                return True
        except ValueError:
            pass
        return hostname in self.server.allowed_hosts

    def _write(self, status, body, content_type):
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self):
        if not self._host_is_allowed():
            self._write(403, "Host is not allowed.", "text/plain; charset=utf-8")
            return
        route = urlparse(self.path).path
        if route == "/":
            self._write(200, PAGE, "text/html; charset=utf-8")
        elif route == "/api/dashboard":
            try:
                payload = json.dumps(build_dashboard_data(), ensure_ascii=False)
            except (RuntimeError, ValueError, TypeError) as exc:
                self._write(500, json.dumps({"error": str(exc)}), "application/json; charset=utf-8")
                return
            self._write(200, payload, "application/json; charset=utf-8")
        elif route == "/api/report":
            try:
                report = build_dashboard_data()["report"]
            except (RuntimeError, ValueError, TypeError) as exc:
                self._write(500, str(exc), "text/plain; charset=utf-8")
                return
            self._write(200, report, "text/plain; charset=utf-8")
        else:
            self._write(404, "Not found", "text/plain; charset=utf-8")

    def do_POST(self):
        if not self._host_is_allowed():
            self._write(403, "Host is not allowed.", "text/plain; charset=utf-8")
            return
        self._write(405, "Dashboard is read-only; order and mode-changing actions are not available.", "text/plain; charset=utf-8")

    def log_message(self, format_string, *args):
        print(f"Dashboard request from {self.client_address[0]}: {format_string % args}")


class LoopbackDashboardServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def main():
    parser = argparse.ArgumentParser(
        description="Run a private, read-only paper-trading dashboard."
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Extra host name for a trusted private reverse proxy, e.g. Tailscale MagicDNS.",
    )
    options = parser.parse_args()
    if not 1024 <= options.port <= 65535:
        raise ValueError("Dashboard port must be between 1024 and 65535.")
    allowed_hosts = set()
    for value in options.allowed_host:
        hostname = value.strip().rstrip(".").lower()
        if not hostname or "/" in hostname or ":" in hostname or "@" in hostname:
            raise ValueError(f"Invalid allowed dashboard host: {value}")
        allowed_hosts.add(hostname)
    server = LoopbackDashboardServer(("127.0.0.1", options.port), DashboardHandler)
    server.allowed_hosts = allowed_hosts
    print(
        f"Read-only paper dashboard: http://127.0.0.1:{options.port}/ "
        "(loopback only; no order controls). Press Ctrl+C to stop."
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local dashboard.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

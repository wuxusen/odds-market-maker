"""Minimal live dashboard — stdlib http.server + one HTML page polling JSON.

Deliberately framework-free: the whole UI is one embedded page hitting
``GET /state`` once a second. Production readiness here means "zero extra
dependencies, zero build step, works over ssh port-forward".
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class StateStore:
    """Thread-safe latest-state holder shared between agent loop and HTTP."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: dict = {"status": "starting"}

    def update(self, state: dict) -> None:
        with self._lock:
            self._state = state

    def get(self) -> dict:
        with self._lock:
            return self._state


_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>Odds Market Maker</title>
<style>
 :root { --bg:#101418; --panel:#1a2028; --text:#dfe6ec; --dim:#8a97a3;
         --green:#38b26b; --red:#d4544f; --amber:#d9a441; }
 body { background:var(--bg); color:var(--text);
        font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace;
        margin:0; padding:20px; }
 h1 { font-size:17px; margin:0 0 14px; }
 .grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(300px,1fr));
         gap:14px; }
 .panel { background:var(--panel); border-radius:8px; padding:12px 14px; }
 .panel h2 { font-size:12px; text-transform:uppercase; letter-spacing:.08em;
             color:var(--dim); margin:0 0 8px; }
 table { border-collapse:collapse; width:100%; }
 td,th { padding:3px 8px 3px 0; text-align:right; font-variant-numeric:tabular-nums; }
 th { color:var(--dim); font-weight:normal; }
 td:first-child, th:first-child { text-align:left; }
 .pos { color:var(--green); } .neg { color:var(--red); }
 .badge { display:inline-block; padding:2px 10px; border-radius:10px;
          font-weight:bold; }
 .ACTIVE { background:#153f2a; color:var(--green); }
 .TRIPPED { background:#46201e; color:var(--red); }
 .halt { color:var(--amber); }
 svg { width:100%; height:90px; }
 .kv td { text-align:left; }
 .dot { display:inline-block; width:10px; height:10px; border-radius:50%;
        margin-right:6px; vertical-align:middle; }
 .dot.ACTIVE { background:var(--green); box-shadow:0 0 6px var(--green); }
 .dot.TRIPPED { background:var(--red); box-shadow:0 0 6px var(--red); animation:pulse 1s infinite; }
 @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:.35} }
 .gauge { background:#0d1116; border-radius:6px; height:8px; margin:4px 0 10px;
          overflow:hidden; }
 .gauge > div { height:100%; background:var(--green); }
 .gauge > div.warn { background:var(--amber); }
 .gauge > div.danger { background:var(--red); }
 .gauge-label { display:flex; justify-content:space-between; color:var(--dim); font-size:12px; }
 #log { max-height:220px; overflow-y:auto; }
 #log div { padding:2px 0; border-bottom:1px solid #232b34; }
 #log .m { color:var(--dim); display:inline-block; width:44px; }
 #log .trip { color:var(--red); }
 #log .rearm { color:var(--green); }
 #log .goal { color:var(--amber); font-weight:bold; }
</style></head><body>
<h1>In-Play Odds Market Maker <span id="clock" style="color:var(--dim)"></span></h1>
<div class="grid">
 <div class="panel"><h2>Circuit Breaker</h2><div id="breaker"></div></div>
 <div class="panel"><h2>PnL / Equity</h2><div id="pnl"></div><svg id="curve"></svg></div>
 <div class="panel"><h2>Match</h2><div id="match"></div></div>
 <div class="panel" style="grid-column:1/-1"><h2>Quotes (probability space / decimal odds)</h2>
   <div id="quotes"></div></div>
 <div class="panel"><h2>Positions</h2><div id="positions"></div></div>
 <div class="panel"><h2>Exposure vs Hard Caps</h2><div id="exposure"></div></div>
 <div class="panel"><h2>Consensus Fair Price</h2><div id="fair"></div></div>
 <div class="panel" style="grid-column:1/-1"><h2>Timeline</h2><div id="log"></div></div>
</div>
<script>
const fmt = (x,d=3) => x==null ? "-" : Number(x).toFixed(d);
const cls = x => x>=0 ? "pos" : "neg";
async function tick() {
  let s;
  try { s = await (await fetch("/state")).json(); }
  catch (e) { document.getElementById("breaker").innerHTML =
      '<span class="badge TRIPPED">NO DATA</span>'; return; }
  document.getElementById("clock").textContent = " — " + (s.match?.clock ?? "");
  const b = s.breaker || {};
  document.getElementById("breaker").innerHTML =
    `<span class="dot ${b.state}"></span><span class="badge ${b.state}">${b.state||"?"}</span>` +
    (b.reason ? ` <span class="halt">${b.reason}</span>` : "") +
    `<div style="color:var(--dim);margin-top:6px">trips this match: ${b.trip_count??0}</div>` +
    (s.halt_reason ? `<div class="halt">quoting halted: ${s.halt_reason}</div>` : "");
  const l = s.ledger || {};
  document.getElementById("pnl").innerHTML =
    `equity <b class="${cls(l.equity - 1000)}">${fmt(l.equity,2)}</b>` +
    ` &nbsp; cash ${fmt(l.cash,2)} &nbsp; fills ${l.n_fills??0}` +
    `<div style="color:var(--dim)">audit chain: ${l.audit_len??0} records, head ${String(l.audit_head||"").slice(0,12)}…</div>`;
  const m = s.match || {};
  document.getElementById("match").innerHTML =
    `<table class="kv"><tr><td>${m.home??"?"} vs ${m.away??"?"}</td></tr>` +
    `<tr><td>score <b>${m.score??"-"}</b> &nbsp; ${m.phase??""} ${m.clock??""}</td></tr>` +
    `<tr><td>reds: ${m.reds??"0-0"}</td></tr></table>`;
  const rows = (s.quotes||[]).map(q =>
    `<tr><td>${q.outcome}</td>` +
    `<td>${fmt(q.bid)} / ${fmt(q.ask)}</td>` +
    `<td>${q.bid ? fmt(1/q.bid,2):"-"} / ${q.ask ? fmt(1/q.ask,2):"-"}</td>` +
    `<td>${fmt(q.bid_size,0)} × ${fmt(q.ask_size,0)}</td>` +
    `<td>${fmt(q.fair)}</td></tr>`).join("");
  document.getElementById("quotes").innerHTML = s.quotes && s.quotes.length ?
    `<table><tr><th>outcome</th><th>bid/ask (prob)</th><th>odds</th><th>sizes</th><th>fair</th></tr>${rows}</table>`
    : '<span class="halt">no live quotes</span>';
  const pos = Object.entries(s.positions||{}).map(([k,v]) =>
    `<tr><td>${k}</td><td class="${cls(v.qty??v)}">${fmt(v.qty??v,1)}</td>` +
    `<td>${fmt(v.avg_price)}</td><td class="${cls(v.realized_pnl??0)}">${fmt(v.realized_pnl,2)}</td></tr>`).join("");
  document.getElementById("positions").innerHTML = pos ?
    `<table><tr><th>leg</th><th>qty</th><th>avg</th><th>real pnl</th></tr>${pos}</table>` : "flat";
  const f = s.fair || {};
  document.getElementById("fair").innerHTML =
    `<table><tr><th>outcome</th><th>prob</th></tr>` +
    Object.entries(f.probs||{}).map(([k,v])=>`<tr><td>${k}</td><td>${fmt(v)}</td></tr>`).join("") +
    `</table><div style="color:var(--dim)">confidence ${fmt(f.confidence,2)} · books ${f.n_books??0}</div>`;
  const ex = s.exposure || {};
  const gauge = (label, val, max) => {
    const pct = max ? Math.min(100, 100*(val||0)/max) : 0;
    const level = pct>90?"danger":pct>60?"warn":"";
    return `<div class="gauge-label"><span>${label}</span><span>${fmt(val,1)} / ${fmt(max,0)}</span></div>` +
           `<div class="gauge"><div class="${level}" style="width:${pct}%"></div></div>`;
  };
  document.getElementById("exposure").innerHTML =
    gauge("fixture worst-case loss", ex.fixture, ex.fixture_max) +
    gauge("book-wide worst-case loss", ex.total, ex.total_max);
  const entries = (s.log || []).slice().reverse();
  document.getElementById("log").innerHTML = entries.map(e => {
    const t = e.text.includes("TRIPPED") ? "trip" :
              e.text.includes("re-armed") ? "rearm" :
              (e.text.startsWith("GOAL")||e.text.startsWith("RED_CARD")) ? "goal" : "";
    return `<div class="${t}"><span class="m">${e.minute}'</span>${e.text}</div>`;
  }).join("") || '<span class="halt">warming up…</span>';
  // equity sparkline
  const pts = s.equity_curve || [];
  if (pts.length > 1) {
    const w=560,h=90, ys=pts.map(p=>p[1]);
    const y0=Math.min(...ys), y1=Math.max(...ys), span=(y1-y0)||1;
    const path = pts.map((p,i)=>`${i?"L":"M"}${(i/(pts.length-1)*w).toFixed(1)},${(h-8-(p[1]-y0)/span*(h-16)).toFixed(1)}`).join("");
    const base = h-8-(1000-y0)/span*(h-16);
    document.getElementById("curve").innerHTML =
      `<line x1="0" y1="${base}" x2="${w}" y2="${base}" stroke="#3a4552" stroke-dasharray="4 4"/>`+
      `<path d="${path}" fill="none" stroke="${ys[ys.length-1]>=1000?'#38b26b':'#d4544f'}" stroke-width="1.6"/>`;
    document.getElementById("curve").setAttribute("viewBox",`0 0 ${w} ${h}`);
  }
}
setInterval(tick, 1000); tick();
</script></body></html>
"""


class _Handler(BaseHTTPRequestHandler):
    store: StateStore  # injected by DashboardServer

    def do_GET(self) -> None:  # noqa: N802 — stdlib API
        if self.path == "/" or self.path.startswith("/index"):
            body = _PAGE.encode("utf-8")
            self._reply(200, "text/html; charset=utf-8", body)
        elif self.path.startswith("/state"):
            body = json.dumps(self.store.get()).encode("utf-8")
            self._reply(200, "application/json", body)
        else:
            self._reply(404, "text/plain", b"not found")

    def _reply(self, code: int, ctype: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:  # silence stdlib noise
        pass


class DashboardServer:
    """Background-thread HTTP server bound to localhost by default."""

    def __init__(self, store: StateStore, host: str = "127.0.0.1", port: int = 8765):
        handler = type("BoundHandler", (_Handler,), {"store": store})
        self.httpd = ThreadingHTTPServer((host, port), handler)
        self._thread = threading.Thread(
            target=self.httpd.serve_forever, name="dashboard", daemon=True
        )

    @property
    def url(self) -> str:
        host, port = self.httpd.server_address[:2]
        return f"http://{host}:{port}/"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

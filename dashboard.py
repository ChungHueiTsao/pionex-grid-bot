"""Local-only monitoring dashboard for live_ta.py. Read-only view of
live_status.json, plus a "Request stop" button that writes a flag file --
it never places or closes an order itself, and never starts trading.

Binds to 127.0.0.1 only (never 0.0.0.0) -- this is deliberately not
reachable from your network or the internet. Run it, then open
http://127.0.0.1:8765 in your browser.

Starting real trading is NOT available from here on purpose -- run
live_ta.py yourself from a terminal and complete its confirmation prompts,
same as always.
"""
from __future__ import annotations

import json
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

STATUS_FILE = Path(__file__).parent / "live_status.json"
STOP_FLAG_FILE = Path(__file__).parent / "live_stop_requested.flag"
HOST = "127.0.0.1"
PORT = 8765

PAGE = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<title>live_ta.py 本機儀表板</title>
<style>
  body { font-family: system-ui, -apple-system, "Segoe UI", "PingFang TC", sans-serif;
         background: #0d0d0d; color: #fff; margin: 0; padding: 24px; }
  .wrap { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: #999; font-size: 13px; margin-bottom: 24px; }
  .card { background: #1a1a19; border: 1px solid rgba(255,255,255,0.1); border-radius: 12px;
          padding: 18px 20px; margin-bottom: 16px; }
  .row { display: flex; justify-content: space-between; padding: 6px 0;
         border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 14px; }
  .row:last-child { border-bottom: none; }
  .label { color: #999; }
  .value { font-variant-numeric: tabular-nums; font-weight: 600; }
  .badge { display: inline-block; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .badge.running { background: #0ca30c33; color: #4ade80; }
  .badge.stopped { background: #d03b3b33; color: #f87171; }
  .badge.unknown { background: #89878133; color: #999; }
  .msg { font-size: 13px; color: #ccc; margin-top: 10px; line-height: 1.5; }
  button { background: #d03b3b; color: #fff; border: none; border-radius: 8px;
           padding: 10px 18px; font-size: 14px; cursor: pointer; font-weight: 600; }
  button:hover { background: #b83232; }
  button:disabled { background: #444; cursor: not-allowed; }
  .note { font-size: 12px; color: #777; margin-top: 20px; line-height: 1.6; }
  .chart-title { font-size: 13px; color: #999; margin-bottom: 12px; }
  .no-data { font-size: 13px; color: #666; text-align: center; padding: 30px 0; }
  #equity-chart { width: 100%; height: auto; }
  .eq-line { fill: none; stroke: #3987e5; stroke-width: 2; }
  .eq-grid { stroke: #2c2c2a; stroke-width: 1; }
  .eq-axis { fill: #777; font-size: 10px; }
  .eq-dot { fill: #3987e5; }
</style>
</head>
<body>
<div class="wrap">
  <h1>live_ta.py 本機儀表板</h1>
  <p class="sub">只在本機讀取，不對外公開。每 5 秒自動刷新一次。</p>

  <div class="card" id="status-card">
    <div class="row"><span class="label">狀態</span><span id="running-badge" class="badge unknown">尚未偵測到</span></div>
    <div class="row"><span class="label">策略</span><span class="value" id="strategy">-</span></div>
    <div class="row"><span class="label">交易對</span><span class="value" id="symbol">-</span></div>
    <div class="row"><span class="label">目前價格</span><span class="value" id="price">-</span></div>
    <div class="row"><span class="label">持有幣</span><span class="value" id="base-free">-</span></div>
    <div class="row"><span class="label">USDT 餘額</span><span class="value" id="quote-free">-</span></div>
    <div class="row"><span class="label">總權益</span><span class="value" id="equity">-</span></div>
    <div class="row"><span class="label">歷史最高權益</span><span class="value" id="peak">-</span></div>
    <div class="row"><span class="label">目前回撤</span><span class="value" id="drawdown">-</span></div>
    <div class="row"><span class="label">最後更新</span><span class="value" id="updated">-</span></div>
    <div class="msg" id="message"></div>
  </div>

  <div class="card">
    <div class="chart-title">即時權益曲線（這支程式跑起來之後的真實紀錄，不是回測）</div>
    <div id="chart-empty" class="no-data">目前還沒有足夠的紀錄點，live_ta.py 每輪詢一次就會多一個點。</div>
    <svg id="equity-chart" viewBox="0 0 600 220" style="display:none"></svg>
  </div>

  <button id="stop-btn" onclick="requestStop()">請求停止（不會平倉，只是讓輪詢迴圈安全退出）</button>

  <div class="note">
    這個按鈕只會建立一個「停止旗標」檔案，live_ta.py 下一輪輪詢會偵測到後自己安全退出——跟你按 Ctrl+C 效果一樣，不會幫你平倉、不會下任何單。<br><br>
    要重新開始交易，請自己回到終端機執行 live_ta.py 並完成裡面的確認步驟，這個儀表板沒有「啟動」按鈕，是刻意設計成這樣。
  </div>
</div>

<script>
async function refresh() {
  try {
    const res = await fetch('/status');
    const s = await res.json();
    document.getElementById('strategy').textContent = s.strategy || '-';
    document.getElementById('symbol').textContent = s.symbol || '-';
    document.getElementById('price').textContent = s.price ? '$' + s.price.toLocaleString() : '-';
    document.getElementById('base-free').textContent = s.base_free != null ? s.base_free : '-';
    document.getElementById('quote-free').textContent = s.quote_free != null ? ('$' + s.quote_free.toFixed(2)) : '-';
    document.getElementById('equity').textContent = s.equity != null ? ('$' + s.equity.toFixed(2)) : '-';
    document.getElementById('peak').textContent = s.peak_equity != null ? ('$' + s.peak_equity.toFixed(2)) : '-';
    document.getElementById('drawdown').textContent = s.drawdown_pct != null ? (s.drawdown_pct.toFixed(1) + '%') : '-';
    document.getElementById('updated').textContent = s.updated_at || '-';
    document.getElementById('message').textContent = s.last_message || '';
    const badge = document.getElementById('running-badge');
    if (s.running === true) {
      badge.textContent = '執行中'; badge.className = 'badge running';
    } else if (s.running === false) {
      badge.textContent = '已停止'; badge.className = 'badge stopped';
    } else {
      badge.textContent = '尚未偵測到'; badge.className = 'badge unknown';
    }
    drawChart(s.equity_history || []);
  } catch (e) {
    document.getElementById('message').textContent = '(讀取 live_status.json 失敗 -- live_ta.py 可能還沒開始跑)';
  }
}

function drawChart(history) {
  const svg = document.getElementById('equity-chart');
  const empty = document.getElementById('chart-empty');
  if (!history || history.length < 2) {
    svg.style.display = 'none';
    empty.style.display = 'block';
    return;
  }
  svg.style.display = 'block';
  empty.style.display = 'none';
  svg.innerHTML = '';

  const W = 600, H = 220, M = { top: 12, right: 60, bottom: 22, left: 8 };
  const plotW = W - M.left - M.right, plotH = H - M.top - M.bottom;
  const n = history.length;
  const values = history.map(p => p.equity);
  const yMin = Math.min(...values) * 0.995, yMax = Math.max(...values) * 1.005;
  const range = (yMax - yMin) || 1;

  function xScale(i) { return M.left + (i / (n - 1)) * plotW; }
  function yScale(v) { return M.top + (1 - (v - yMin) / range) * plotH; }

  const ns = 'http://www.w3.org/2000/svg';
  function el(tag, attrs) {
    const e = document.createElementNS(ns, tag);
    for (const k in attrs) e.setAttribute(k, attrs[k]);
    return e;
  }

  // 3 horizontal gridlines with $ labels
  for (let i = 0; i <= 2; i++) {
    const v = yMin + (range * i / 2);
    const y = yScale(v);
    svg.appendChild(el('line', { x1: M.left, x2: M.left + plotW, y1: y.toFixed(1), y2: y.toFixed(1), class: 'eq-grid' }));
    const t = el('text', { x: M.left + plotW + 6, y: (y + 3).toFixed(1), class: 'eq-axis' });
    t.textContent = '$' + v.toFixed(0);
    svg.appendChild(t);
  }

  let d = '';
  history.forEach((p, i) => { d += (i === 0 ? 'M' : 'L') + xScale(i).toFixed(1) + ',' + yScale(p.equity).toFixed(1) + ' '; });
  svg.appendChild(el('path', { d, class: 'eq-line' }));

  const lastX = xScale(n - 1), lastY = yScale(values[n - 1]);
  svg.appendChild(el('circle', { cx: lastX.toFixed(1), cy: lastY.toFixed(1), r: 3.5, class: 'eq-dot' }));

  const firstTime = new Date(history[0].t).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  const lastTime = new Date(history[n - 1].t).toLocaleString('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  svg.appendChild(Object.assign(el('text', { x: M.left, y: H - 4, class: 'eq-axis' }), { textContent: firstTime }));
  svg.appendChild(Object.assign(el('text', { x: M.left + plotW, y: H - 4, class: 'eq-axis', 'text-anchor': 'end' }), { textContent: lastTime }));
}

async function requestStop() {
  const btn = document.getElementById('stop-btn');
  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = '傳送中...';
  try {
    const res = await fetch('/stop', { method: 'POST' });
    if (!res.ok) throw new Error('server returned ' + res.status);
    btn.textContent = '已送出停止請求，等待下一輪輪詢...';
    setTimeout(() => { btn.disabled = false; btn.textContent = originalText; }, 8000);
  } catch (e) {
    btn.textContent = '送出失敗，請確認 dashboard.py 還在跑，再試一次';
    btn.disabled = false;
  }
}

refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # keep the console quiet

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send(200, "text/html; charset=utf-8", PAGE.encode("utf-8"))
        elif self.path == "/status":
            if STATUS_FILE.exists():
                self._send(200, "application/json", STATUS_FILE.read_bytes())
            else:
                self._send(200, "application/json", b'{"running": null}')
        else:
            self._send(404, "text/plain", b"Not found")

    def do_POST(self):
        if self.path == "/stop":
            STOP_FLAG_FILE.write_text("requested")
            self._send(200, "application/json", b'{"ok": true}')
        else:
            self._send(404, "text/plain", b"Not found")

    def _send(self, code: int, content_type: str, body: bytes) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    server = HTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"Dashboard running at {url} (localhost only, not reachable from your network)")
    print("Ctrl+C to stop the dashboard server (this does NOT stop live_ta.py).")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nDashboard server stopped.")

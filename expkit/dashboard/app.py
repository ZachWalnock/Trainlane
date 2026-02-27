from __future__ import annotations

import json
from uuid import UUID

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from expkit.db import Store


DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>ExpKit Run Dashboard</title>
    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;500;600&family=Space+Grotesk:wght@400;500;700&display=swap" rel="stylesheet" />
    <script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
    <style>
      :root {
        --bg-1: #f3efe8;
        --bg-2: #f8f6f2;
        --ink: #1e2329;
        --muted: #606a76;
        --accent: #006d77;
        --accent-soft: #bde0d7;
        --warn: #bc6c25;
        --error: #b00020;
        --card: rgba(255, 255, 255, 0.72);
        --edge: rgba(0, 0, 0, 0.08);
        --shadow: 0 18px 40px rgba(35, 44, 58, 0.12);
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        color: var(--ink);
        background:
          radial-gradient(80rem 30rem at 20% -10%, #d6ebe6 0%, transparent 70%),
          radial-gradient(60rem 30rem at 110% 0%, #ead7c4 0%, transparent 70%),
          linear-gradient(180deg, var(--bg-1), var(--bg-2));
        min-height: 100vh;
        font-family: "Space Grotesk", sans-serif;
      }
      .wrap {
        max-width: 1200px;
        margin: 0 auto;
        padding: 24px;
      }
      .top {
        display: grid;
        grid-template-columns: 1.1fr 1fr;
        gap: 16px;
        margin-bottom: 16px;
      }
      .card {
        border: 1px solid var(--edge);
        background: var(--card);
        backdrop-filter: blur(9px);
        border-radius: 16px;
        box-shadow: var(--shadow);
      }
      .run {
        padding: 18px;
      }
      .run h1 {
        margin: 0 0 8px 0;
        font-size: 22px;
      }
      .sub {
        color: var(--muted);
        font-size: 13px;
      }
      .status-pill {
        margin-top: 12px;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        padding: 7px 12px;
        border-radius: 999px;
        border: 1px solid var(--edge);
        font-weight: 600;
      }
      .dot {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        background: var(--warn);
        box-shadow: 0 0 0 6px rgba(188, 108, 37, 0.18);
      }
      .metrics-grid {
        padding: 14px;
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        gap: 10px;
      }
      .metric-card {
        border: 1px solid var(--edge);
        border-radius: 12px;
        padding: 12px;
        background: rgba(255, 255, 255, 0.74);
      }
      .metric-name {
        font-size: 12px;
        text-transform: uppercase;
        color: var(--muted);
        letter-spacing: 0.08em;
      }
      .metric-value {
        margin-top: 6px;
        font-size: 24px;
        font-weight: 700;
        font-family: "IBM Plex Mono", monospace;
      }
      .mid {
        display: grid;
        grid-template-columns: 1fr;
        gap: 16px;
      }
      .chart-card {
        padding: 12px;
      }
      #chart {
        width: 100%;
        height: 340px;
      }
      .logs {
        padding: 14px;
      }
      .logs-head {
        display: flex;
        justify-content: space-between;
        align-items: center;
      }
      .console {
        margin-top: 10px;
        border-radius: 10px;
        border: 1px solid #d7dadd;
        background: #121820;
        color: #f4f7fc;
        font-family: "IBM Plex Mono", monospace;
        font-size: 12px;
        min-height: 260px;
        max-height: 420px;
        overflow: auto;
        padding: 10px;
      }
      .line { white-space: pre-wrap; margin: 0 0 5px 0; }
      .line.stderr { color: #ff8f96; }
      .line.metric { color: #6ef9c8; }
      @media (max-width: 980px) {
        .top { grid-template-columns: 1fr; }
        .metrics-grid { grid-template-columns: 1fr 1fr; }
      }
      @media (max-width: 620px) {
        .metrics-grid { grid-template-columns: 1fr; }
      }
    </style>
  </head>
  <body>
    <div class="wrap">
      <section class="top">
        <div class="card run">
          <h1>Experiment Run</h1>
          <div class="sub" id="run-id"></div>
          <div class="sub" id="run-backend"></div>
          <div class="status-pill">
            <span class="dot" id="dot"></span>
            <span id="status">running</span>
          </div>
          <div class="sub" id="error"></div>
        </div>
        <div class="card metrics-grid" id="metrics-grid"></div>
      </section>

      <section class="mid">
        <div class="card chart-card">
          <div class="sub">Live Metrics</div>
          <div id="chart"></div>
        </div>
        <div class="card logs">
          <div class="logs-head">
            <strong>Live Console</strong>
            <span class="sub" id="event-count">0 events</span>
          </div>
          <div id="console" class="console"></div>
        </div>
      </section>
    </div>

    <script>
      const runId = window.location.pathname.split('/').pop();
      let after = 0;
      let eventCount = 0;
      const metrics = new Map();
      const consoleEl = document.getElementById('console');
      const metricsGrid = document.getElementById('metrics-grid');
      const eventCountEl = document.getElementById('event-count');
      const chart = echarts.init(document.getElementById('chart'));

      document.getElementById('run-id').textContent = `Run ID: ${runId}`;

      function drawChart() {
        const series = [];
        for (const [name, points] of metrics.entries()) {
          series.push({
            name,
            type: 'line',
            smooth: true,
            showSymbol: false,
            data: points.map((p) => [p.step, p.value]),
            lineStyle: { width: 2 }
          });
        }
        chart.setOption({
          backgroundColor: 'transparent',
          tooltip: { trigger: 'axis' },
          legend: { top: 0 },
          grid: { left: 40, right: 18, top: 34, bottom: 28 },
          xAxis: { type: 'value', name: 'step', minInterval: 1 },
          yAxis: { type: 'value' },
          series
        });
      }

      function renderMetricCards() {
        metricsGrid.innerHTML = '';
        const names = Array.from(metrics.keys()).slice(0, 6);
        if (!names.length) {
          metricsGrid.innerHTML = '<div class="sub">No metrics yet. Call trainer.log("loss", value, step=...).</div>';
          return;
        }
        for (const name of names) {
          const data = metrics.get(name);
          const latest = data[data.length - 1];
          const values = data.map((d) => d.value);
          const min = Math.min(...values);
          const max = Math.max(...values);
          const card = document.createElement('div');
          card.className = 'metric-card';
          card.innerHTML = `
            <div class="metric-name">${name}</div>
            <div class="metric-value">${Number(latest.value).toFixed(5)}</div>
            <div class="sub">step ${latest.step ?? '-'} | min ${min.toFixed(5)} | max ${max.toFixed(5)}</div>
          `;
          metricsGrid.appendChild(card);
        }
      }

      function appendLine(kind, text) {
        const p = document.createElement('p');
        p.className = `line ${kind}`;
        p.textContent = text;
        consoleEl.appendChild(p);
        while (consoleEl.childElementCount > 800) {
          consoleEl.removeChild(consoleEl.firstChild);
        }
        consoleEl.scrollTop = consoleEl.scrollHeight;
      }

      async function poll() {
        try {
          const runRes = await fetch(`/api/runs/${runId}`);
          if (runRes.ok) {
            const run = await runRes.json();
            document.getElementById('status').textContent = run.status;
            document.getElementById('run-backend').textContent = `Backend: ${run.backend}`;
            document.getElementById('error').textContent = run.error_summary ? `Error: ${run.error_summary}` : '';

            const dot = document.getElementById('dot');
            if (run.status === 'succeeded') {
              dot.style.background = '#2b9348';
              dot.style.boxShadow = '0 0 0 6px rgba(43,147,72,0.18)';
            } else if (run.status === 'failed') {
              dot.style.background = '#b00020';
              dot.style.boxShadow = '0 0 0 6px rgba(176,0,32,0.20)';
            }
          }

          const eventsRes = await fetch(`/api/runs/${runId}/events?after_id=${after}`);
          if (!eventsRes.ok) return;
          const payload = await eventsRes.json();
          const events = payload.events || [];
          for (const ev of events) {
            after = ev.id;
            eventCount += 1;
            const line = ev.value_json?.line || ev.value_json?.message || JSON.stringify(ev.value_json);
            if (ev.event_type === 'metric') {
              const v = Number(ev.value_json?.value);
              if (!Number.isNaN(v)) {
                const step = ev.step ?? ev.value_json?.step ?? 0;
                if (!metrics.has(ev.key)) metrics.set(ev.key, []);
                metrics.get(ev.key).push({ step, value: v });
              }
              appendLine('metric', `[metric] ${ev.key}: ${v}`);
            } else {
              appendLine(ev.event_type === 'stderr' ? 'stderr' : '', `[${ev.event_type}] ${line}`);
            }
          }

          eventCountEl.textContent = `${eventCount} events`;
          if (events.length) {
            renderMetricCards();
            drawChart();
          }
        } catch (_) {
          // Dashboard keeps polling even during transient errors.
        }
      }

      setInterval(poll, 1200);
      poll();
      window.addEventListener('resize', () => chart.resize());
    </script>
  </body>
</html>
"""


def create_dashboard_app(store: Store) -> FastAPI:
    app = FastAPI(title="ExpKit Dashboard")

    @app.get("/", response_class=HTMLResponse)
    def root() -> str:
        return """
        <html><body style='font-family: sans-serif; padding: 24px;'>
        <h2>ExpKit Dashboard</h2>
        <p>Open <code>/runs/&lt;run_id&gt;</code> to inspect a run.</p>
        </body></html>
        """

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_page(run_id: UUID) -> str:
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")
        return DASHBOARD_HTML

    @app.get("/api/runs/{run_id}")
    def run_meta(run_id: UUID):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        serialized = {
            **run,
            "id": str(run["id"]),
            "started_at": run["started_at"].isoformat() if run["started_at"] else None,
            "ended_at": run["ended_at"].isoformat() if run["ended_at"] else None,
        }
        return JSONResponse(serialized)

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: UUID, after_id: int = 0):
        run = store.get_run(run_id)
        if not run:
            raise HTTPException(status_code=404, detail="Run not found")

        rows = store.get_events(run_id=run_id, after_id=after_id)
        events = []
        for r in rows:
            events.append(
                {
                    "id": r["id"],
                    "ts": r["ts"].isoformat() if r["ts"] else None,
                    "event_type": r["event_type"],
                    "key": r["key"],
                    "value_json": r["value_json"],
                    "step": r["step"],
                    "source": r["source"],
                }
            )

        return JSONResponse({"events": json.loads(json.dumps(events, default=str))})

    return app

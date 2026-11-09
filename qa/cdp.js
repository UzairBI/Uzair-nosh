// Minimal headless-Edge driver over the DevTools protocol (no dependencies). Read-only with respect to the app under test.
const { spawn } = require('child_process');
const os = require('os');
const EDGE = process.env.EDGE || 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function launch({ width = 1366, height = 768 } = {}) {
  const port = 9300 + Math.floor(Math.random() * 600);
  const proc = spawn(EDGE, ['--headless=new', '--disable-gpu', `--remote-debugging-port=${port}`, `--window-size=${width},${height}`,
    '--hide-scrollbars', '--user-data-dir=' + os.tmpdir() + '/edge_qa_' + port, 'about:blank'], { stdio: 'ignore' });
  let targets;
  for (let i = 0; i < 60; i++) { try { targets = await (await fetch(`http://127.0.0.1:${port}/json`)).json(); if (targets.length) break; } catch (e) {} await sleep(300); }
  const ws = new WebSocket(targets.find(t => t.type === 'page').webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0; const pend = {}; const errors = []; const failedReqs = [];
  ws.onmessage = m => {
    const d = JSON.parse(m.data);
    if (d.id && pend[d.id]) pend[d.id](d.result || d);
    else if (d.method === 'Runtime.exceptionThrown') errors.push('exception: ' + JSON.stringify(d.params.exceptionDetails).slice(0, 300));
    else if (d.method === 'Runtime.consoleAPICalled' && d.params.type === 'error') errors.push('console.error: ' + JSON.stringify(d.params.args).slice(0, 300));
    else if (d.method === 'Network.responseReceived' && d.params.response.status >= 400) failedReqs.push(d.params.response.status + ' ' + d.params.response.url);
  };
  const send = (method, params = {}) => new Promise(r => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params })); });
  await send('Runtime.enable'); await send('Page.enable'); await send('Network.enable');
  const api = {
    errors, failedReqs, send,
    async size(w, h) { await send('Emulation.setDeviceMetricsOverride', { width: w, height: h, deviceScaleFactor: 1, mobile: false }); },
    async goto(url, wait = 1800) { await send('Page.navigate', { url }); await sleep(wait); },
    async ev(expr) {
      const r = await send('Runtime.evaluate', { expression: expr, awaitPromise: true, returnByValue: true });
      if (r.exceptionDetails) throw new Error('eval failed: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text).slice(0, 300));
      return r.result.value;
    },
    async waitFor(expr, ms = 45000, step = 250) {
      const t0 = Date.now();
      while (Date.now() - t0 < ms) { try { if (await api.ev(expr)) return true; } catch (e) {} await sleep(step); }
      return false;
    },
    async shot(path) {
      const s = await send('Page.captureScreenshot', { format: 'png' });
      require('fs').writeFileSync(path, Buffer.from(s.data, 'base64'));
    },
    sleep,
    async close() { try { ws.close(); } catch (e) {} proc.kill(); },
  };
  await api.size(width, height);
  return api;
}
module.exports = { launch, sleep };

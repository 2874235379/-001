// 零依赖 CDP 检查脚本：Node 22 自带 WebSocket
// 用法：node tools/cdp_check.mjs <url> <输出前缀> [real|demo]
import fs from 'node:fs';
import path from 'node:path';

const url = process.argv[2] || 'http://127.0.0.1:8010/';
const prefix = process.argv[3] || 'D:/tmp/facepass-shot';
const mode = process.argv[4] || 'real';

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const targets = await fetch('http://127.0.0.1:9222/json/list').then((r) => r.json());
const page = targets.find((t) => t.type === 'page');
if (!page) throw new Error('没有找到可用的页面 target');

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = reject;
});

let seq = 0;
const pending = new Map();
const consoleErrors = [];
const exceptions = [];
const netFailures = [];

ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  if (msg.id && pending.has(msg.id)) {
    const { resolve, reject } = pending.get(msg.id);
    pending.delete(msg.id);
    if (msg.error) reject(new Error(JSON.stringify(msg.error)));
    else resolve(msg.result);
    return;
  }
  if (msg.method === 'Runtime.consoleAPICalled' && msg.params.type === 'error') {
    consoleErrors.push(msg.params.args.map((a) => a.value || a.description || '').join(' '));
  }
  if (msg.method === 'Runtime.exceptionThrown') {
    const d = msg.params.exceptionDetails;
    exceptions.push(d.exception?.description || d.text);
  }
  if (msg.method === 'Network.loadingFailed' && msg.params.errorText !== 'net::ERR_ABORTED') {
    netFailures.push(`${msg.params.errorText} ${msg.params.type}`);
  }
};

function send(method, params = {}) {
  const id = ++seq;
  return new Promise((resolve, reject) => {
    pending.set(id, { resolve, reject });
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await send('Runtime.evaluate', {
    expression,
    returnByValue: true,
    awaitPromise: true,
    userGesture: true,
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  }
  return result.result.value;
}

async function shot(name) {
  const { data } = await send('Page.captureScreenshot', { format: 'png' });
  const file = `${prefix}-${name}.png`;
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, Buffer.from(data, 'base64'));
  return file;
}

async function waitFor(expression, timeout = 20000, interval = 150) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (await evaluate(expression).catch(() => false)) return true;
    await sleep(interval);
  }
  return false;
}

await send('Page.enable');
await send('Runtime.enable');
await send('Log.enable');
await send('Network.enable');
await send('Emulation.setDeviceMetricsOverride', {
  width: 1440,
  height: 1000,
  deviceScaleFactor: 1,
  mobile: false,
});

await send('Page.navigate', { url });
await sleep(1200);
await waitFor("document.readyState === 'complete'", 10000);
await sleep(2200);

const snapshotExpr = `(() => {
  const q = (s) => document.querySelector(s);
  const txt = (s) => (q(s) ? q(s).textContent.trim() : null);
  return {
    title: document.title,
    stage: q('#stage') ? q('#stage').dataset.state : null,
    statusTitle: txt('#statusTitle'),
    statusHint: txt('#statusHint'),
    tag: txt('#stageTagText'),
    env: txt('#envLabel'),
    envTone: q('#envPill') ? q('#envPill').dataset.tone : null,
    alertVisible: q('#alertBar') ? !q('#alertBar').hidden : null,
    alertTitle: txt('#alertTitle'),
    videoWidth: q('#video') ? q('#video').videoWidth : 0,
    videoHeight: q('#video') ? q('#video').videoHeight : 0,
    records: document.querySelectorAll('.record-item').length,
    drawerRows: document.querySelectorAll('.check-row').length,
    metaChips: document.querySelectorAll('#metaRow .chip').length,
    todayCount: txt('#todayCount'),
    empCount: txt('#empCount'),
    bodyHeight: document.body.scrollHeight,
    viewportHeight: window.innerHeight
  };
})()`;

const first = await evaluate(snapshotExpr);
const shots = [];
shots.push(await shot('main'));

// 打开环境诊断
await evaluate("document.querySelector('#envPill').click(); true");
await sleep(700);
const drawer = await evaluate(`(() => {
  const d = document.querySelector('#drawer');
  return {
    open: d ? d.dataset.open : null,
    rows: document.querySelectorAll('#drawerBody .check-row').length,
    text: document.querySelector('#drawerBody') ? document.querySelector('#drawerBody').innerText.slice(0, 600) : ''
  };
})()`);
shots.push(await shot('drawer'));

// 关闭抽屉 + 切浅色主题
await evaluate("document.querySelector('#drawerMask').click(); true");
await sleep(500);
await evaluate("document.querySelector('#themeBtn').click(); true");
await sleep(600);
const light = await evaluate(
  "({ theme: document.documentElement.dataset.theme, bg: getComputedStyle(document.body).backgroundColor })"
);
shots.push(await shot('light'));

let success = null;
if (mode === 'demo') {
  await evaluate("document.querySelector('#themeBtn').click(); true");
  const ok = await waitFor("document.querySelector('#statusTitle').textContent.includes('打卡成功')", 25000, 120);
  if (ok) {
    success = await evaluate(snapshotExpr);
    shots.push(await shot('success'));
    await waitFor(
      "document.querySelector('#stage').dataset.state === 'scanning' || document.querySelector('#stage').dataset.state === 'blink'",
      8000,
      100
    );
    shots.push(await shot('scanning'));
  } else {
    success = { reached: false, status: await evaluate("document.querySelector('#statusTitle').textContent") };
    shots.push(await shot('no-success'));
  }
}

// 移动端布局
await send('Emulation.setDeviceMetricsOverride', {
  width: 390,
  height: 844,
  deviceScaleFactor: 2,
  mobile: true,
});
await sleep(800);
shots.push(await shot('mobile'));
const mobile = await evaluate(
  "({ innerWidth: window.innerWidth, scrollWidth: document.documentElement.scrollWidth, bodyHeight: document.body.scrollHeight })"
);

console.log(
  JSON.stringify(
    {
      url,
      mode,
      first,
      drawer,
      light,
      mobile,
      success,
      shots,
      consoleErrors,
      exceptions,
      netFailures,
    },
    null,
    2
  )
);

ws.close();
process.exit(consoleErrors.length || exceptions.length || netFailures.length ? 1 : 0);

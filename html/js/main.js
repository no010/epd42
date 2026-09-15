/* Web app wiring: one Web Bluetooth connection, four content modes sharing a
 * 400x300 canvas.  All modes push through the stream protocol in epd.js -
 * PackBits in the browser, ~190 packets per plane, refresh only on the final
 * plane, never a SLEEP.
 */
'use strict';

let bleDevice = null;
let gattServer = null;
let link = null;
let pushBusy = false;
let activeTab = 'pomodoro';
let lastImage = null;

const $ = (id) => document.getElementById(id);

function setStatus(text) { $('status').innerHTML = text; }

function addLog(html) {
  const log = $('log');
  const now = new Date();
  const time = `${String(now.getHours()).padStart(2, '0')}:`
             + `${String(now.getMinutes()).padStart(2, '0')}:`
             + `${String(now.getSeconds()).padStart(2, '0')} `;
  log.innerHTML += `<span class="time">${time}</span>${html}<br>`;
  log.scrollTop = log.scrollHeight;
  while ((log.innerHTML.match(/<br>/g) || []).length > 40) {
    log.innerHTML = log.innerHTML.substring(log.innerHTML.search('<br>') + 4);
  }
}

function stampNow() {
  const d = new Date();
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} `
       + `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* ── preview ──────────────────────────────────────────────────────────── */

function renderPreview() {
  const canvas = $('screen');
  if (activeTab === 'pomodoro') {
    composeFace(timer.state, canvas, stampNow());
    const s = timer.state;
    const left = `${String(Math.floor(s.remaining / 60)).padStart(2, '0')}:`
               + `${String(s.remaining % 60).padStart(2, '0')}`;
    $('pomo-summary').textContent =
      `今日完成 ${s.cycleTotal} 个番茄 · 本轮 ${s.pomodoroCount}/${s.rounds} · `
      + `${PHASE_NAMES[s.phase][0]}剩 ${left}${s.running ? '' : '（暂停）'}`;
  } else if (activeTab === 'cards') {
    composeCards(loadCards(), canvas, stampNow());
  }
  // image/debug tabs keep whatever is on the canvas
}

/* ── pushing ──────────────────────────────────────────────────────────── */

async function pushCanvas(label) {
  if (!link) { setStatus('未连接墨水屏'); return false; }
  if (pushBusy) { setStatus('上一次推送仍在进行'); return false; }
  pushBusy = true;
  try {
    if (link.planeBytes !== PLANE_BYTES) {
      throw new EpdError(`设备平面为 ${link.planeBytes} 字节,本页生成 ${PLANE_BYTES} 字节`);
    }
    const canvas = $('screen');
    const planes = [{ index: 0, data: canvasToPlane(canvas) }];
    if (link.driverId === 3) planes.push({ index: 1, data: allPaperPlane() });

    for (let i = 0; i < planes.length; i++) {
      const plane = planes[i];
      const last = i === planes.length - 1;
      const stat = await link.streamPlane(plane.index, plane.data, last,
        (sent, total) => setStatus(`${label}: ${sent}/${total} 包…`));
      addLog(`${label} 平面${plane.index}: ${stat.raw}B → ${stat.encoded}B `
             + `(${Math.round(stat.encoded * 100 / stat.raw)}%,${stat.packets} 包) `
             + `begin ${stat.beginS.toFixed(2)}s + 数据 ${stat.dataS.toFixed(1)}s `
             + `+ 刷新 ${stat.endS.toFixed(1)}s`);
    }
    setStatus(`${label}: 推送完成,面板已刷新`);
    addLog(`<span class="action">✓</span> ${label} 推送完成`);
    return true;
  } catch (e) {
    console.error(e);
    addLog(`<span class="action">✗</span> 推送失败: ${e.message}`);
    setStatus(`推送失败: ${e.message}`);
    if (link) await link.abort();
    return false;
  } finally {
    pushBusy = false;
  }
}

/* ── connection ───────────────────────────────────────────────────────── */

function updateConnUi() {
  const connected = !!(gattServer && gattServer.connected);
  $('connectbutton').textContent = connected ? '断开' : '连接墨水屏';
  ['push-pomodoro', 'push-cards', 'push-image', 'set-driver', 'clearscreen',
   'sendcmdbutton'].forEach((id) => { $(id).disabled = !connected; });
}

async function connect() {
  bleDevice = await navigator.bluetooth.requestDevice({
    filters: [{ namePrefix: 'NRF_EPD_' }],
    optionalServices: [EPD_SERVICE_UUID],
  });
  addLog(`正在连接: ${bleDevice.name}`);
  bleDevice.addEventListener('gattserverdisconnected', onDisconnected);
  gattServer = await bleDevice.gatt.connect();
  const service = await gattServer.getPrimaryService(EPD_SERVICE_UUID);
  const char = await service.getCharacteristic(EPD_CHAR_UUID);
  await char.startNotifications();
  char.addEventListener('characteristicvaluechanged', (event) => {
    link.handleNotify(event.target.value);
  });
  link = new EpdLink(char);
  await link.queryStatus();
  $('driver').value = String(link.driverId);
  $('device-info').textContent =
    `${bleDevice.name} · 驱动 ${link.driverId} (${DRIVER_NAMES[link.driverId] || '未知'})`
    + ` · 平面 ${link.planeBytes}B`;
  addLog(`已连接,驱动 ${link.driverId},平面 ${link.planeBytes} 字节`);
  updateConnUi();
}

function onDisconnected() {
  gattServer = null;
  link = null;
  $('device-info').textContent = '未连接 — Chrome/Edge 桌面版，Web Bluetooth';
  addLog('已断开连接');
  updateConnUi();
}

async function connectButton() {
  if (gattServer && gattServer.connected) {
    bleDevice.gatt.disconnect();
    return;
  }
  try {
    await connect();
    renderPreview();
  } catch (e) {
    console.error(e);
    addLog(`连接失败: ${e.message}`);
    setStatus(`连接失败: ${e.message}`);
  }
}

/* ── tabs ─────────────────────────────────────────────────────────────── */

function switchTab(name) {
  activeTab = name;
  document.querySelectorAll('.tabs button').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('section.tab').forEach((s) => {
    s.classList.toggle('active', s.id === `tab-${name}`);
  });
  renderPreview();
}

/* ── pomodoro tab ─────────────────────────────────────────────────────── */

let timer;

function bindPomodoro() {
  timer = new PomodoroTimer((state, phaseChanged) => {
    if (activeTab === 'pomodoro') renderPreview();
    if (phaseChanged && link && $('pomo-autopush').checked) {
      void pushCanvas('番茄钟');
    }
  });

  $('start').addEventListener('click', () => timer.startPause());
  $('skip').addEventListener('click', () => timer.skip());
  $('reset').addEventListener('click', () => timer.reset());
  $('wipe').addEventListener('click', () => timer.wipe());
  $('push-pomodoro').addEventListener('click', () => {
    renderPreview();
    void pushCanvas('番茄钟');
  });

  for (const key of ['work', 'short', 'long', 'rounds']) {
    $(key).value = timer.cfg[key === 'work' ? 'workMinutes'
      : key === 'short' ? 'shortMinutes'
      : key === 'long' ? 'longMinutes' : 'rounds'];
    $(key).addEventListener('change', () => {
      const cfg = loadCfg();
      cfg[{ work: 'workMinutes', short: 'shortMinutes',
            long: 'longMinutes', rounds: 'rounds' }[key]] = Number($(key).value);
      saveCfg(cfg);
      timer.cfgChanged();
      renderPreview();
    });
  }
  $('auto-advance').addEventListener('change', () => {
    timer.autoAdvance = $('auto-advance').checked;
  });
}

/* ── cards tab ────────────────────────────────────────────────────────── */

const CARD_FIELDS = [
  ['planName', '名称', 'text'],
  ['unit', '单位', 'text'],
  ['quotaUsed', '已用', 'number'],
  ['quotaTotal', '总量', 'number'],
  ['balanceYuan', '余额（元）', 'number'],
  ['extra', '指标行附加', 'text'],
  ['note', '备注行', 'text'],
];

function buildCardEditors() {
  const host = $('card-editors');
  host.innerHTML = '';
  loadCards().forEach((card, index) => {
    const box = document.createElement('div');
    box.className = 'card-editor';
    box.innerHTML = `<strong>卡 ${index + 1}</strong> `
      + `<label class="hint"><input type="checkbox" data-field="showBar" `
      + `${card.showBar !== false ? 'checked' : ''} /> 显示进度条</label>`;
    for (const [field, label, type] of CARD_FIELDS) {
      const el = document.createElement('label');
      el.innerHTML = `${label} `
        + `<input type="${type}" data-field="${field}" value="${card[field] ?? ''}" />`;
      box.appendChild(el);
    }
    box.addEventListener('input', () => {
      const cards = loadCards();
      box.querySelectorAll('[data-field]').forEach((input) => {
        const field = input.dataset.field;
        cards[index][field] = input.type === 'checkbox' ? input.checked
          : input.type === 'number' ? Number(input.value) : input.value;
      });
      saveCards(cards);
      renderPreview();
    });
    host.appendChild(box);
  });
}

function bindCards() {
  buildCardEditors();
  $('cards-demo').addEventListener('click', () => {
    localStorage.removeItem(CARDS_STORE_KEY);
    buildCardEditors();
    renderPreview();
  });
  $('push-cards').addEventListener('click', () => {
    composeCards(loadCards(), $('screen'), stampNow());
    void pushCanvas('用量卡');
  });
}

/* ── image tab ────────────────────────────────────────────────────────── */

function applyDithering() {
  const canvas = $('screen');
  const ctx = canvas.getContext('2d');
  if (lastImage) {
    ctx.fillStyle = '#fff';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(lastImage, 0, 0, lastImage.width, lastImage.height,
                  0, 0, canvas.width, canvas.height);
  }
  const mode = $('dithering').value;
  if (mode !== 'none') {
    dithering(ctx, canvas.width, canvas.height,
              parseInt($('threshold').value, 10) || 128, mode);
  }
}

function bindImage() {
  $('image_file').addEventListener('change', (event) => {
    const file = event.target.files[0];
    if (!file) return;
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(image.src);
      lastImage = image;
      applyDithering();
    };
    image.src = URL.createObjectURL(file);
  });
  $('apply-dither').addEventListener('click', applyDithering);
  $('push-image').addEventListener('click', () => void pushCanvas('图片'));
}

/* ── debug tab ────────────────────────────────────────────────────────── */

function hex2bytes(hex) {
  const bytes = [];
  for (let c = 0; c < hex.length; c += 2) {
    bytes.push(parseInt(hex.substr(c, 2), 16));
  }
  return Uint8Array.from(bytes);
}

function bindDebug() {
  $('set-driver').addEventListener('click', async () => {
    try {
      await link.setDriver(Number($('driver').value));
      $('device-info').textContent =
        `${bleDevice.name} · 驱动 ${link.driverId} `
        + `(${DRIVER_NAMES[link.driverId] || '未知'}) · 平面 ${link.planeBytes}B`;
      addLog(`驱动已切换为 ${link.driverId}`);
    } catch (e) {
      addLog(`切换驱动失败: ${e.message}`);
    }
  });
  $('clearscreen').addEventListener('click', async () => {
    if (confirm('确认清除屏幕内容?')) {
      await link.clear();
      addLog('已发送清屏命令');
    }
  });
  $('sendcmdbutton').addEventListener('click', async () => {
    const text = $('cmdTXT').value.trim();
    if (!text) return;
    const bytes = hex2bytes(text);
    await link.sendRaw(bytes);
    addLog(`<span class="action">⇑</span> ${text}`);
  });
}

/* ── boot ─────────────────────────────────────────────────────────────── */

document.body.onload = () => {
  $('connectbutton').addEventListener('click', () => void connectButton());
  document.querySelectorAll('.tabs button').forEach((button) => {
    button.addEventListener('click', () => switchTab(button.dataset.tab));
  });
  bindPomodoro();
  bindCards();
  bindImage();
  bindDebug();
  updateConnUi();
  renderPreview();
};

/* EPD42 stream protocol + Web Bluetooth link.
 *
 * Mirrors tools/epd-monitor/protocol.py and ble_client.py: the host composes
 * a 400x300 1-bit plane, PackBits-encodes it, and streams it as 19-byte DATA
 * packets (BLE 4.1 has no MTU exchange).  BEGIN and END block until the
 * device notifies its ack - write responses only prove the packet reached
 * the link layer.
 *
 * Wire convention (must match EPD/EPD_4in2*.c): bit 1 = white paper, bit 0 =
 * black ink, MSB is the leftmost pixel of a byte.
 *
 * No SLEEP flag is ever sent: a panel put into deep sleep (0x07 0xA5) stopped
 * taking later refresh commands on hardware, so it rests in hardware reset
 * instead - the firmware holds RST low on disconnect, and the next
 * connection's rising edge plus Init()'s reset pulses wake it for sure.
 */
'use strict';

const EPD_SERVICE_UUID = '62750001-d828-918d-fb46-b6c11c675aec';
const EPD_CHAR_UUID = '62750002-d828-918d-fb46-b6c11c675aec';

const CMD_INIT = 0x01;
const CMD_CLEAR = 0x02;
const CMD_SEND_CMD = 0x03;
const CMD_SLEEP = 0x06;
const CMD_SET_POWER = 0x93;
const CMD_STREAM_BEGIN = 0xB0;
const CMD_STREAM_DATA = 0xB1;
const CMD_STREAM_END = 0xB2;
const CMD_STREAM_ABORT = 0xB3;
const CMD_GET_STATUS = 0xB5;

const FLAG_REFRESH = 0x01;

const POWER_RESIDENT = 0x00;    // stays advertising for periodic-push apps
const POWER_DEEP_SLEEP = 0x01;  // MCU off after each frame; wake = reset/wakeup pin

const STATUS_OK = 0x00;
const STATUS_NAMES = {
  0x00: 'ok',
  0x01: 'bad command / no plane open',
  0x02: 'decoded length does not match the plane',
  0x03: 'checksum mismatch (corrupted bytes)',
  0x04: 'panel busy timeout',
};

const DATA_CHUNK = 19;          // command byte + 19 payload bytes = 20 ATT
const SCREEN_WIDTH = 400;
const SCREEN_HEIGHT = 300;
const LINE_BYTES = SCREEN_WIDTH / 8;
const PLANE_BYTES = LINE_BYTES * SCREEN_HEIGHT;
const ACK_TIMEOUT_MS = 10000;

const DRIVER_NAMES = {
  1: '4.2in BW (UC8176)',
  2: '4.2in V2 (BW)',
  3: '4.2in B V2 (BWR)',
};

class EpdError extends Error {}

/* ── packing ──────────────────────────────────────────────────────────── */

/** TIFF PackBits, no end-of-line marker (see protocol.py for the contract). */
function packbitsEncode(plane) {
  const out = [];
  let index = 0;
  const total = plane.length;
  while (index < total) {
    const byte = plane[index];
    let run = 1;
    while (run < 128 && index + run < total && plane[index + run] === byte) run++;
    if (run >= 3) {
      out.push(257 - run, byte);
      index += run;
      continue;
    }
    const literal = [];
    while (index < total && literal.length < 128) {
      let lookahead = 1;
      while (lookahead < 128 && index + lookahead < total
             && plane[index + lookahead] === plane[index]) lookahead++;
      if (lookahead >= 3) break;
      literal.push(plane[index]);
      index++;
    }
    out.push(literal.length - 1, ...literal);
  }
  return Uint8Array.from(out);
}

/** Running byte sum the firmware compares against in STREAM_END. */
function checksum(plane) {
  let sum = 0;
  for (let i = 0; i < plane.length; i++) sum += plane[i];
  return sum >>> 0;                      // sum(15000 bytes) < 2^32
}

/** Pack an "L" canvas (drawn black-on-white) into one 15000-byte plane. */
function canvasToPlane(canvas) {
  if (canvas.width !== SCREEN_WIDTH || canvas.height !== SCREEN_HEIGHT) {
    throw new EpdError(`画布应为 ${SCREEN_WIDTH}x${SCREEN_HEIGHT},实际 `
                       + `${canvas.width}x${canvas.height}`);
  }
  const pixels = canvas.getContext('2d').getImageData(
    0, 0, SCREEN_WIDTH, SCREEN_HEIGHT).data;
  const plane = new Uint8Array(PLANE_BYTES);
  for (let y = 0; y < SCREEN_HEIGHT; y++) {
    const rowBase = y * SCREEN_WIDTH * 4;
    for (let byteIndex = 0; byteIndex < LINE_BYTES; byteIndex++) {
      let bits = 0;
      for (let bit = 0; bit < 8; bit++) {
        const o = rowBase + (byteIndex * 8 + bit) * 4;
        const luma = 0.299 * pixels[o] + 0.587 * pixels[o + 1]
                   + 0.114 * pixels[o + 2];
        if (luma > 127) bits |= 0x80 >> bit;
      }
      plane[y * LINE_BYTES + byteIndex] = bits;
    }
  }
  return plane;
}

function allPaperPlane() {
  return new Uint8Array(PLANE_BYTES).fill(0xFF);
}

/** STREAM_END request from the raw plane: the device verifies decoded bytes. */
function endRequest(plane, flags) {
  const packet = new Uint8Array(8);
  packet[0] = CMD_STREAM_END;
  packet[1] = plane.length & 0xFF;
  packet[2] = (plane.length >> 8) & 0xFF;
  const sum = checksum(plane);
  packet[3] = sum & 0xFF;
  packet[4] = (sum >>> 8) & 0xFF;
  packet[5] = (sum >>> 16) & 0xFF;
  packet[6] = (sum >>> 24) & 0xFF;
  packet[7] = flags;
  return packet;
}

function* dataChunks(encoded) {
  for (let offset = 0; offset < encoded.length; offset += DATA_CHUNK) {
    const chunk = new Uint8Array(1 + Math.min(DATA_CHUNK, encoded.length - offset));
    chunk[0] = CMD_STREAM_DATA;
    chunk.set(encoded.subarray(offset, offset + chunk.length - 1), 1);
    yield chunk;
  }
}

/* ── shared canvas text helpers (PIL-compatible anchoring) ────────────── */

const MONO_FONT = `Consolas, 'Courier New', monospace`;
const CJK_FONT = `'Microsoft YaHei', 'SimHei', 'Noto Sans CJK SC', sans-serif`;

function setFont(ctx, px, family) {
  ctx.font = `${px}px ${family}`;
}

function textWidth(ctx, text) {
  return ctx.measureText(text).width;
}

/** Line height this font actually produces (render.py _metrics). */
function metricsOf(ctx, px, family) {
  setFont(ctx, px, family);
  const m = ctx.measureText('AgjyQ');
  return Math.max((m.actualBoundingBoxAscent || px) + (m.actualBoundingBoxDescent || 0), 1) + 2;
}

/** Fill text with PIL's "la" anchor: y is the font's ascender line. */
function drawText(ctx, text, x, y, px, family) {
  setFont(ctx, px, family);
  const m = ctx.measureText(text);
  const ascent = m.fontBoundingBoxAscent || m.actualBoundingBoxAscent || px;
  ctx.fillText(text, x, y + ascent);
}

/* ── BLE link ─────────────────────────────────────────────────────────── */

class EpdLink {
  constructor(characteristic) {
    this._char = characteristic;
    this._acks = [];
    this._waiters = [];
    this.driverId = 0;
    this.planeBytes = 0;
    this.streaming = 0;
  }

  handleNotify(data) {
    const packet = new Uint8Array(data.buffer, data.byteOffset, data.byteLength);
    const waiter = this._waiters.find((w) => w.command === packet[0]);
    if (waiter) {
      this._waiters.splice(this._waiters.indexOf(waiter), 1);
      clearTimeout(waiter.timer);
      waiter.resolve(packet);
    } else {
      this._acks.push(packet);           // unsolicited: keep for later readers
    }
  }

  _waitForAck(command) {
    const queued = this._acks.findIndex((p) => p[0] === command);
    if (queued >= 0) return Promise.resolve(this._acks.splice(queued, 1)[0]);
    return new Promise((resolve, reject) => {
      const entry = { command, resolve, reject, timer: null };
      entry.timer = setTimeout(() => {
        const i = this._waiters.indexOf(entry);
        if (i >= 0) this._waiters.splice(i, 1);
        reject(new EpdError(`命令 0x${command.toString(16)} 在 `
                            + `${ACK_TIMEOUT_MS / 1000}s 内无应答`));
      }, ACK_TIMEOUT_MS);
      this._waiters.push(entry);
    });
  }

  _checkStatus(packet, what) {
    const status = packet.length > 1 ? packet[1] : 0xFF;
    if (status !== STATUS_OK) {
      throw new EpdError(`${what}: ${STATUS_NAMES[status] || '0x' + status.toString(16)}`);
    }
  }

  async _write(payload, withResponse = true) {
    if (withResponse && this._char.writeValueWithResponse) {
      await this._char.writeValueWithResponse(payload);
    } else if (!withResponse && this._char.writeValueWithoutResponse) {
      await this._char.writeValueWithoutResponse(payload);
    } else {
      await this._char.writeValue(payload);
    }
  }

  async queryStatus() {
    await this._write(Uint8Array.of(CMD_GET_STATUS));
    const packet = await this._waitForAck(CMD_GET_STATUS);
    if (packet.length < 8) throw new EpdError('状态回复过短');
    this.streaming = packet[1];
    this.planeBytes = packet[5] | (packet[6] << 8);
    this.driverId = packet[7];
    this.powerMode = packet.length > 8 ? packet[8] : POWER_RESIDENT;
    return packet;
  }

  async setDriver(driverId) {
    await this._write(Uint8Array.of(CMD_INIT, driverId));
    await this.queryStatus();
  }

  async setPowerMode(deep) {
    await this._write(Uint8Array.of(CMD_SET_POWER,
                                    deep ? POWER_DEEP_SLEEP : POWER_RESIDENT));
    await this.queryStatus();
  }

  async _begin(index) {
    const started = performance.now();
    await this._write(Uint8Array.of(CMD_STREAM_BEGIN, index));
    this._checkStatus(await this._waitForAck(CMD_STREAM_BEGIN), 'STREAM_BEGIN');
    return (performance.now() - started) / 1000;
  }

  /** Stream one packed plane; only the final plane triggers a refresh. */
  async streamPlane(index, raw, last, onProgress) {
    const flags = last ? FLAG_REFRESH : 0;
    const encoded = packbitsEncode(raw);
    const beginS = await this._begin(index);

    const started = performance.now();
    let sent = 0;
    for (const chunk of dataChunks(encoded)) {
      await this._write(chunk, true);
      sent++;
      if (onProgress && (sent % 20 === 0 || sent * DATA_CHUNK >= encoded.length)) {
        onProgress(sent, -(-encoded.length / DATA_CHUNK | 0));
      }
    }
    const dataS = (performance.now() - started) / 1000;

    const endStarted = performance.now();
    await this._write(endRequest(raw, flags));
    this._checkStatus(await this._waitForAck(CMD_STREAM_END), 'STREAM_END');
    const endS = (performance.now() - endStarted) / 1000;

    return {
      raw: raw.length,
      encoded: encoded.length,
      packets: sent,
      beginS, dataS,
      endS,                            // ≈ the panel refresh duration
    };
  }

  async abort() {
    try {
      await this._write(Uint8Array.of(CMD_STREAM_ABORT));
      await this._waitForAck(CMD_STREAM_ABORT);
    } catch (e) {                      // best effort - the link may be gone
      console.debug('abort was not acked', e);
    }
  }

  /** Raw debug command (hex bytes), as the old panel debugger sent them. */
  async sendRaw(bytes) {
    await this._write(bytes);
  }

  async clear() {
    await this._write(Uint8Array.of(CMD_CLEAR));
  }
}

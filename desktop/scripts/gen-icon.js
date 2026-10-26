/* ============================================================
   scripts/gen-icon.js — 生成应用图标（纯 Node，无外部依赖）
   ------------------------------------------------------------
   icons/icon.png（256×256 窗口/通知）、icons/tray.png（32×32 托盘）。
   视觉与控制台 favicon 同构：深空底 #0B1120 圆 + 状态绿 #22C55E 环 + 核。
   PNG 编码：RGBA 扫描线（filter 0）→ zlib deflate → CRC32 校验块。
   ============================================================ */
"use strict";

const zlib = require("zlib");
const fs = require("fs");
const path = require("path");

/* ---------- PNG 最小编码器 ---------- */
const CRC_TABLE = (() => {
  const t = new Int32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1;
    t[n] = c;
  }
  return t;
})();
function crc32(buf) {
  let c = 0 ^ -1;
  for (let i = 0; i < buf.length; i++) c = (c >>> 8) ^ CRC_TABLE[(c ^ buf[i]) & 0xff];
  return (c ^ -1) >>> 0;
}
function chunk(type, data) {
  const len = Buffer.alloc(4);
  len.writeUInt32BE(data.length);
  const typeBuf = Buffer.from(type, "ascii");
  const crc = Buffer.alloc(4);
  crc.writeUInt32BE(crc32(Buffer.concat([typeBuf, data])));
  return Buffer.concat([len, typeBuf, data, crc]);
}
function png(width, height, rgba) {
  const sig = Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]);
  const ihdr = Buffer.alloc(13);
  ihdr.writeUInt32BE(width, 0);
  ihdr.writeUInt32BE(height, 4);
  ihdr[8] = 8; ihdr[9] = 6;           // 8bit RGBA
  const raw = Buffer.alloc((width * 4 + 1) * height);
  for (let y = 0; y < height; y++) {
    raw[y * (width * 4 + 1)] = 0;     // filter: none
    rgba.copy(raw, y * (width * 4 + 1) + 1, y * width * 4, (y + 1) * width * 4);
  }
  return Buffer.concat([
    sig,
    chunk("IHDR", ihdr),
    chunk("IDAT", zlib.deflateSync(raw, { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

/* ---------- 绘制：深空底圆 + 绿环 + 核（边缘 1px 抗锯齿） ---------- */
function draw(size) {
  const buf = Buffer.alloc(size * size * 4);
  const cx = (size - 1) / 2, cy = (size - 1) / 2;
  const R = size * 0.5 - 1;            // 底圆半径
  const ringR = size * 0.36;           // 绿环半径
  const ringW = Math.max(2, size * 0.075);
  const coreR = size * 0.14;
  const aa = x => Math.max(0, Math.min(1, x));    // 简易软化
  for (let y = 0; y < size; y++) {
    for (let x = 0; x < size; x++) {
      const d = Math.hypot(x - cx, y - cy);
      const i = (y * size + x) * 4;
      let r = 0x0B, g = 0x11, b = 0x20, a = 0;
      const baseA = aa(R - d + 0.5);             // 底圆
      if (baseA > 0) { a = Math.round(255 * baseA); }
      const ringA = aa(ringW / 2 - Math.abs(d - ringR) + 0.5);
      if (ringA > 0) { r = 0x22; g = 0xC5; b = 0x5E; a = Math.round(255 * Math.max(baseA, ringA)); }
      const coreA = aa(coreR - d + 0.5);
      if (coreA > 0) { r = 0x22; g = 0xC5; b = 0x5E; a = Math.round(255 * Math.max(baseA, coreA)); }
      buf[i] = r; buf[i + 1] = g; buf[i + 2] = b; buf[i + 3] = a;
    }
  }
  return buf;
}

const outDir = path.join(__dirname, "..", "icons");
fs.mkdirSync(outDir, { recursive: true });
fs.writeFileSync(path.join(outDir, "icon.png"), png(256, 256, draw(256)));
fs.writeFileSync(path.join(outDir, "tray.png"), png(32, 32, draw(32)));
console.log("icons written:", fs.readdirSync(outDir).join(", "));

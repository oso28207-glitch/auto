#!/usr/bin/env node
/*
 * vidsp_decrypt.js — فك تشفير رابط التشغيل الحقيقي (sp=300) لمشغّل vidsp.net
 * ─────────────────────────────────────────────────────────────────────────────
 * مشغّل vidsp يرسل POST إلى /dl (op=playerddl) ويعيد JSON فيه:
 *     [{ file: "<مشفّر>", seed: "<بذرة>", image, tracks }]
 * ثم يطبّق خريطة استبدال على البذرة، يستدعي decrypt(file, seed) من tear.js،
 * ويطبّق نفس الخريطة على الناتج للحصول على رابط m3u8 الحقيقي (sp=300 بدل sp=1).
 *
 * الاستخدام:
 *     node vidsp_decrypt.js <encrypted_file> <seed>
 *   أو تمرير JSON عبر stdin:  {"file":"...","seed":"..."}
 *
 * المخرجات: الرابط المفكوك على stdout (سطر واحد)، وأي خطأ على stderr مع رمز خروج != 0.
 */
'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');
const vm = require('vm');
const https = require('https');

const TEAR_URL = 'https://v.vidsp.net/js/tear.js';
const CACHE = path.join(os.tmpdir(), 'vidsp_tear_cache.js');
// خريطة الاستبدال كما في كود المشغّل
const CHARS = { '0': '5', '1': '6', '2': '7', '5': '0', '6': '1', '7': '2' };
const subst = (s) => String(s).replace(/[012567]/g, (m) => CHARS[m]);

function httpGet(url, headers, timeoutMs) {
  return new Promise((resolve, reject) => {
    const req = https.get(url, { headers }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        return reject(new Error('HTTP ' + res.statusCode));
      }
      let data = '';
      res.setEncoding('utf8');
      res.on('data', (c) => { data += c; });
      res.on('end', () => resolve(data));
    });
    req.on('error', reject);
    req.setTimeout(timeoutMs || 20000, () => req.destroy(new Error('timeout')));
  });
}

async function loadTear() {
  // 1) محاولة جلب أحدث نسخة من الشبكة
  try {
    const code = await httpGet(
      TEAR_URL,
      { 'User-Agent': 'Mozilla/5.0', 'Referer': 'https://v.vidsp.net/' },
      20000
    );
    if (code && code.length > 5000) {
      try { fs.writeFileSync(CACHE, code); } catch (e) { /* ignore */ }
      return code;
    }
  } catch (e) {
    process.stderr.write('warn: fetch tear.js failed: ' + e.message + '\n');
  }
  // 2) الرجوع إلى نسخة مخزّنة مؤقتاً
  try {
    const cached = fs.readFileSync(CACHE, 'utf8');
    if (cached && cached.length > 5000) return cached;
  } catch (e) { /* ignore */ }
  // 3) نسخة مرفقة داخل المستودع (احتياطي دائم)
  const localCandidates = [
    path.join(__dirname, 'assets', 'vidsp_tear.js'),
    path.join(__dirname, 'vidsp_tear.js'),
  ];
  for (const p of localCandidates) {
    try {
      const c = fs.readFileSync(p, 'utf8');
      if (c && c.length > 5000) return c;
    } catch (e) { /* ignore */ }
  }
  throw new Error('tear.js unavailable');
}

function buildSandbox() {
  const noop = () => {};
  const sandbox = {
    window: {},
    document: {
      createElement: () => ({ style: {}, setAttribute: noop, appendChild: noop }),
      getElementsByTagName: () => [],
      documentElement: {},
      cookie: '',
    },
    navigator: { userAgent: 'Mozilla/5.0' },
    location: { href: 'https://v.vidsp.net/' },
    console: { log: noop, warn: noop, error: noop, info: noop, debug: noop },
    atob: (s) => Buffer.from(s, 'base64').toString('binary'),
    btoa: (s) => Buffer.from(s, 'binary').toString('base64'),
    setTimeout, clearTimeout, setInterval, clearInterval,
    Math, Date, JSON, String, Array, Object, Number, Boolean,
    parseInt, parseFloat, isNaN, encodeURIComponent, decodeURIComponent,
    RegExp, Error, Function, Buffer,
    alert: noop, prompt: () => null, confirm: () => true,
  };
  sandbox.self = sandbox;
  sandbox.globalThis = sandbox;
  return sandbox;
}

function readInput() {
  const a = process.argv.slice(2);
  if (a.length >= 2) return { file: a[0], seed: a[1] };
  // قراءة JSON من stdin
  try {
    const raw = fs.readFileSync(0, 'utf8');
    const j = JSON.parse(raw);
    const o = Array.isArray(j) ? j[0] : j;
    if (o && o.file && o.seed) return { file: o.file, seed: o.seed };
  } catch (e) { /* ignore */ }
  return null;
}

(async () => {
  const inp = readInput();
  if (!inp) {
    process.stderr.write('usage: node vidsp_decrypt.js <encrypted> <seed>  (or JSON on stdin)\n');
    process.exit(2);
  }

  let code;
  try {
    code = await loadTear();
  } catch (e) {
    process.stderr.write('error: ' + e.message + '\n');
    process.exit(3);
  }

  const sandbox = buildSandbox();
  vm.createContext(sandbox);
  try {
    vm.runInContext(code, sandbox, { timeout: 20000 });
  } catch (e) {
    process.stderr.write('error running tear.js: ' + e.message + '\n');
    process.exit(4);
  }
  if (typeof sandbox.decrypt !== 'function') {
    process.stderr.write('error: decrypt() not found in tear.js\n');
    process.exit(5);
  }

  try {
    const seed = subst(inp.seed);
    const dec = sandbox.decrypt(inp.file, seed);
    const url = subst(dec);
    process.stdout.write(url + '\n');
    process.exit(0);
  } catch (e) {
    process.stderr.write('error decrypting: ' + e.message + '\n');
    process.exit(6);
  }
})();

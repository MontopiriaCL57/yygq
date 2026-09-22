/* ============================================================
   yygq面馆 · 每日谜题引擎 puzzle-engine.js
   纯前端可用（未登录可玩、离线可算），算法与后端 site_api.py 完全一致：
     - fnv1a + mulberry32 + Fisher-Yates 稳定洗牌
     - dayIndex 自 2026-01-01（北京时间）
     - 每日 1 枚属性对照 + 1 枚其他形式
   数据驱动：加题只加 JSON，不改这里的代码。
   ============================================================ */
(function (global) {
  'use strict';
  var YQ = global.YQ = global.YQ || {};
  var P = YQ.puzzle = {};

  /* ---------------- 计分配置（与后端 CONFIG 对齐） ---------------- */
  P.CONFIG = {
    BASE: 100,          /* 猜中得分 */
    PENALTY: 15,        /* 每次错误扣分 */
    FLOOR: 40,          /* 单题下限 */
    COMBO_STEP: 0.10,   /* 连击每天加成 10% */
    COMBO_MAX: 1.00,    /* 加成上限 +100% */
    DAILY: 2            /* 每天题数 */
  };

  /* ---------------- 确定性随机（务必与 Python 端逐位一致） ----------------
     注意：必须按 UTF-8 字节哈希。JS 的 charCodeAt 是 UTF-16 码元，
     与 Python 的 s.encode('utf-8') 在非 ASCII 上不一致（曾测出分歧）。 */
  P.utf8 = function (str) {
    if (typeof TextEncoder !== 'undefined') return new TextEncoder().encode(str);
    var out = [], i, c, c2, cp;
    for (i = 0; i < str.length; i++) {
      c = str.charCodeAt(i);
      if (c < 0x80) out.push(c);
      else if (c < 0x800) out.push(0xC0 | (c >> 6), 0x80 | (c & 63));
      else if (c >= 0xD800 && c <= 0xDBFF) {
        c2 = str.charCodeAt(++i);
        cp = 0x10000 + ((c - 0xD800) << 10) + (c2 - 0xDC00);
        out.push(0xF0 | (cp >> 18), 0x80 | ((cp >> 12) & 63), 0x80 | ((cp >> 6) & 63), 0x80 | (cp & 63));
      } else out.push(0xE0 | (c >> 12), 0x80 | ((c >> 6) & 63), 0x80 | (c & 63));
    }
    return out;
  };
  P.fnv1a = function (str) {
    var h = 2166136261 >>> 0, bytes = P.utf8(String(str));
    for (var i = 0; i < bytes.length; i++) { h ^= bytes[i]; h = Math.imul(h, 16777619) >>> 0; }
    return h >>> 0;
  };
  P.mulberry32 = function (a) {
    a = a >>> 0;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      var t = (a ^ (a >>> 15)) >>> 0;
      t = Math.imul(t, (1 | a) >>> 0) >>> 0;
      var t2 = Math.imul((t ^ (t >>> 7)) >>> 0, (61 | t) >>> 0) >>> 0;
      t = ((t + t2) ^ t) >>> 0;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  };
  P.order = function (n, salt) {          /* 稳定洗牌：返回下标序列 */
    var r = P.mulberry32(P.fnv1a(salt)), a = [], i, j, tmp;
    for (i = 0; i < n; i++) a.push(i);
    for (i = n - 1; i > 0; i--) { j = Math.floor(r() * (i + 1)); tmp = a[i]; a[i] = a[j]; a[j] = tmp; }
    return a;
  };

  /* ---------------- 日期（北京时间） ---------------- */
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  P.today = function () {
    var c = new Date(Date.now() + 8 * 3600 * 1000);   /* 东八区墙上时间（与时区无关） */
    return c.getUTCFullYear() + '-' + pad(c.getUTCMonth() + 1) + '-' + pad(c.getUTCDate());
  };
  P.dayIndex = function (d) {
    var p = String(d).split('-');
    var y = +p[0], m = +p[1], dd = +p[2];
    if (!y || !m || !dd) return 0;
    return Math.floor((Date.UTC(y, m - 1, dd) - Date.UTC(2026, 0, 1)) / 86400000);
  };
  P.dateFromIndex = function (idx) {
    var c = new Date(Date.UTC(2026, 0, 1) + idx * 86400000);
    return c.getUTCFullYear() + '-' + pad(c.getUTCMonth() + 1) + '-' + pad(c.getUTCDate());
  };

  /* ---------------- 题库加载 ---------------- */
  P.BANK_URL = '/puzzle/bank/all.json';
  P._bank = null;
  P.load = function () {
    if (P._bank) return Promise.resolve(P._bank);
    return fetch(P.BANK_URL, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('题库加载失败 HTTP ' + r.status);
      return r.json();
    }).then(function (b) {
      var qs = (b && b.questions) ? b.questions.slice() : [];
      qs.sort(function (x, y) { return x.id < y.id ? -1 : (x.id > y.id ? 1 : 0); });
      P._bank = { version: b.version || 0, questions: qs };
      return P._bank;
    });
  };
  P._pools = {};
  P.pool = function (cat) {
    if (P._pools[cat]) return Promise.resolve(P._pools[cat]);
    return fetch('/puzzle/bank/pools/' + cat + '.json', { credentials: 'same-origin' })
      .then(function (r) { if (!r.ok) throw new Error('属性池加载失败'); return r.json(); })
      .then(function (p) { P._pools[cat] = p; return p; });
  };

  /* ---------------- 每日抽取（与后端同算法） ---------------- */
  P.pickDaily = function (bank, dateStr) {
    var day = dateStr || P.today();
    var attr = [], other = [], i;
    for (i = 0; i < bank.questions.length; i++) {
      (bank.questions[i].type === 'attr' ? attr : other).push(bank.questions[i]);
    }
    if (!attr.length || !other.length) return [];
    var di = P.dayIndex(day);
    var oa = P.order(attr.length, 'yygq-attr-v2');
    /* Python 取模对负数与 JS 不同，这里统一成非负 */
    var ia = ((di % attr.length) + attr.length) % attr.length;
    var q1 = attr[oa[ia]];
    /* 第二题：在全局轮转顺序里，从「当天位置」开始往后取第一枚
       分类与第一题不同的题 —— 既保证两题不同分类（观感更丰富），
       又保持逐日轮转、长期不重复的性质。 */
    var ob = P.order(other.length, 'yygq-other-v2');
    var start = ((di % other.length) + other.length) % other.length;
    var q2 = null;
    for (var k = 0; k < other.length; k++) {
      var cand = other[ob[(start + k) % other.length]];
      if (cand.cat !== q1.cat) { q2 = cand; break; }
    }
    if (!q2) q2 = other[ob[start]];      /* 极端兜底：其他题全是同一分类 */
    return [q1, q2];
  };

  /* ---------------- 答案归一化与判定 ---------------- */
  P.norm = function (s) {
    return String(s == null ? '' : s)
      .replace(/[\uFF01-\uFF5E]/g, function (ch) { return String.fromCharCode(ch.charCodeAt(0) - 0xFEE0); }) /* 全角→半角 */
      .toLowerCase()
      .replace(/[\s·・.。、,，\-_'"「」《》()（）!！?？:：;；]/g, '');
  };
  P.match = function (guess, q, pool) {
    var g = P.norm(guess);
    if (!g) return false;
    var cands = [q.answer];
    if (q.aliases && q.aliases.length) cands = cands.concat(q.aliases);
    if (q.type === 'attr' && pool) {
      for (var i = 0; i < pool.entities.length; i++) {
        if (pool.entities[i].name === q.answer) {
          cands = cands.concat(pool.entities[i].aliases || []);
          break;
        }
      }
    }
    for (var j = 0; j < cands.length; j++) if (P.norm(cands[j]) === g) return true;
    return false;
  };
  /* 属性题：把输入解析成池中实体（支持别名） */
  P.resolveEntity = function (guess, pool) {
    var g = P.norm(guess);
    if (!g) return null;
    for (var i = 0; i < pool.entities.length; i++) {
      var e = pool.entities[i];
      if (P.norm(e.name) === g) return e;
      var al = e.aliases || [];
      for (var j = 0; j < al.length; j++) if (P.norm(al[j]) === g) return e;
    }
    return null;
  };

  /* ---------------- 属性对照 ---------------- */
  P.compare = function (field, guessV, ansV) {
    var t = field.t;
    if (t === 'num') {
      var a = Number(guessV), b = Number(ansV);
      if (a === b) return { state: 'hit', txt: String(guessV) };
      return a > b ? { state: 'down', txt: String(guessV) + ' ↓' } : { state: 'up', txt: String(guessV) + ' ↑' };
    }
    if (t === 'set') {
      var ga = Array.isArray(guessV) ? guessV : [guessV];
      var aa = Array.isArray(ansV) ? ansV : [ansV];
      var inter = ga.filter(function (x) { return aa.indexOf(x) >= 0; });
      if (inter.length === ga.length && ga.length === aa.length) return { state: 'hit', txt: ga.join('/') };
      if (inter.length) return { state: 'part', txt: ga.join('/') + ' ◐' + inter.join('/') };
      return { state: 'miss', txt: ga.join('/') };
    }
    var same = String(guessV) === String(ansV);
    return { state: same ? 'hit' : 'miss', txt: String(guessV) };
  };
  P.compareRow = function (pool, guessEnt, ansEnt) {
    var out = [];
    for (var i = 0; i < pool.fields.length; i++) {
      var r = P.compare(pool.fields[i], guessEnt.v[i], ansEnt.v[i]);
      r.k = pool.fields[i].k;
      out.push(r);
    }
    return out;
  };

  /* ---------------- 计分 ----------------
     口径（与后端 site_api.py 的 _puzzle_score 完全一致）：
       streak 参数 = 连续答题天数【含当天】
       连击加成从第 2 天开始算：bonus = (streak-1) × 10%，上限 +100%
       → 第 1 天猜中就是 100 分（符合需求「猜中得 100 分」），第 11 天起 ×2.0
  */
  P.baseScore = function (wrong) {
    var C = P.CONFIG;
    return Math.max(C.FLOOR, C.BASE - C.PENALTY * Math.max(0, wrong));
  };
  P.comboDays = function (streak) {          /* 计入加成的天数 = 连击天数 - 1 */
    return Math.max(0, Math.floor(streak || 0) - 1);
  };
  P.comboMultiplier = function (comboDays) {
    var C = P.CONFIG;
    var s = Math.max(0, Math.min(comboDays || 0, Math.round(C.COMBO_MAX / C.COMBO_STEP)));
    return 1 + s * C.COMBO_STEP;
  };
  P.finalScore = function (wrong, streak) {
    return Math.round(P.baseScore(wrong) * P.comboMultiplier(P.comboDays(streak)));
  };

  /* ---------------- 战绩分享文本 ---------------- */
  P.shareText = function (day, rows, total, streak) {
    var lines = ['yygq面馆 · 每日谜题 ' + day];
    for (var i = 0; i < rows.length; i++) {
      var r = rows[i];
      if (!r.done) { lines.push('⬜ ' + r.label + ' 未挑战'); continue; }
      lines.push(r.emoji + ' ' + r.label + ' ' + r.wrong + '错 ' + r.gain + '分');
    }
    lines.push('总分 ' + total + (streak > 1 ? ' · 连击 x' + streak : ''));
    lines.push(location.origin + '/puzzle/');
    return lines.join('\n');
  };

  /* ---------------- 今日进度本地存档（未登录也能看到自己的记录） ---------------- */
  var KEY = 'yq_puzzle_progress_v1';
  P.local = {
    all: function () { return YQ.store.get(KEY, {}); },
    get: function (day) { return P.local.all()[day] || null; },
    save: function (day, obj) {
      var a = P.local.all(); a[day] = obj;
      var keys = Object.keys(a).sort();
      while (keys.length > 120) { delete a[keys.shift()]; }   /* 只留最近 120 天 */
      YQ.store.set(KEY, a);
    }
  };

  /* ---------------- 连击（本地估算，登录后以后端为准） ---------------- */
  P.localStreak = function (day) {
    var a = P.local.all(), n = 0, idx = P.dayIndex(day);
    for (var i = 1; i <= 60; i++) {
      var d = P.dateFromIndex(idx - i), rec = a[d];
      if (rec && rec.total > 0) n++; else break;
    }
    return n;
  };
})(window);

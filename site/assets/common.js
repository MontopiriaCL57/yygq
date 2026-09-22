/* ============================================================
   yygq面馆 · 公共脚本 common.js
   零依赖；提供：DOM 助手 / HTML 转义 / fetch 封装 / 粒子背景 /
   顶部导航 / 登录态 / 时间格式化 / 图片压缩 / 空态 / 复制
   所有页面在 </body> 前引入，并在页面脚本前调用 YQ.boot()
   ============================================================ */
(function (global) {
  'use strict';
  var YQ = global.YQ = global.YQ || {};

  /* ---------------- DOM ---------------- */
  YQ.$ = function (sel, root) { return (root || document).querySelector(sel); };
  YQ.$$ = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };

  /* ---------------- 转义（所有用户内容必须过这里） ---------------- */
  YQ.esc = function (s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };
  /* 转义后把换行变 <br>（正文展示用） */
  YQ.escBr = function (s) { return YQ.esc(s).replace(/\n/g, '<br>'); };

  /* ---------------- URL 参数 ---------------- */
  YQ.qs = function (name, def) {
    try {
      var v = new URLSearchParams(location.search).get(name);
      return v === null ? (def === undefined ? '' : def) : v;
    } catch (e) { return def === undefined ? '' : def; }
  };

  /* ---------------- localStorage 安全封装 ---------------- */
  YQ.store = {
    get: function (k, d) { try { var v = localStorage.getItem(k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    del: function (k) { try { localStorage.removeItem(k); } catch (e) {} }
  };

  /* ---------------- toast ---------------- */
  YQ.toast = function (msg, isErr) {
    var el = YQ.$('#toast');
    if (!el) { el = document.createElement('div'); el.id = 'toast'; document.body.appendChild(el); }
    el.textContent = msg;
    el.className = 'show' + (isErr ? ' err' : '');
    clearTimeout(YQ._tt);
    YQ._tt = setTimeout(function () { el.className = isErr ? 'err' : ''; }, 2600);
  };

  /* ---------------- fetch 封装 ----------------
     统一：JSON、超时、错误归一化、401 回调。
     永不抛未捕获异常；失败返回 {ok:false, code, msg}。 */
  YQ.api = function (path, opts) {
    opts = opts || {};
    var ctl = ('AbortController' in global) ? new AbortController() : null;
    var timer = null;
    if (ctl) timer = setTimeout(function () { try { ctl.abort(); } catch (e) {} }, opts.timeout || 15000);

    var init = { method: opts.method || 'GET', headers: opts.headers || {}, credentials: 'same-origin' };
    if (ctl) init.signal = ctl.signal;
    if (opts.body !== undefined && opts.body !== null) {
      /* Blob 用 typeof 探测：老环境/无 Blob 的宿主里直接 instanceof 会抛 ReferenceError */
      var isBlob = (typeof Blob !== 'undefined') && (opts.body instanceof Blob);
      if (typeof opts.body === 'string' || isBlob) { init.body = opts.body; }
      else { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    }
    return fetch(path, init).then(function (r) {
      if (timer) clearTimeout(timer);
      return r.text().then(function (t) {
        var d = null;
        try { d = t ? JSON.parse(t) : {}; } catch (e) { d = { code: 1, msg: '返回格式异常' }; }
        if (!r.ok) {
          if (r.status === 401 && YQ.on401) { try { YQ.on401(d); } catch (e) {} }
          return { ok: false, code: d.code === undefined ? r.status : d.code, msg: d.msg || ('HTTP ' + r.status), data: d };
        }
        return { ok: d.code === 0 || d.code === undefined, code: d.code, msg: d.msg || '', data: d };
      });
    }).catch(function (e) {
      if (timer) clearTimeout(timer);
      return { ok: false, code: -1, msg: (e && e.name === 'AbortError') ? '请求超时' : '网络异常', data: {} };
    });
  };

  /* ---------------- 空态 / 错误态（不白屏） ---------------- */
  YQ.empty = function (el, text, sub, icon) {
    if (typeof el === 'string') el = YQ.$(el);
    if (!el) return;
    el.innerHTML = '<div class="empty"><span class="big">' + (icon || '✧') + '</span>' +
      YQ.esc(text || '这里空空如也') +
      (sub ? '<span class="sub">' + YQ.esc(sub) + '</span>' : '') + '</div>';
  };
  YQ.loading = function (el, text) {
    if (typeof el === 'string') el = YQ.$(el);
    if (el) el.innerHTML = '<div class="loading">' + YQ.esc(text || '加载中') + '</div>';
  };

  /* ---------------- 时间 ---------------- */
  YQ.fmtTime = function (s) {
    if (!s) return '';
    var t = String(s).replace('T', ' ');
    var m = t.match(/^(\d{4})-(\d{2})-(\d{2})[ ]?(\d{2}:\d{2})?/);
    if (!m) return t;
    var now = Date.now();
    var d = new Date(m[1] + '/' + m[2] + '/' + m[3] + ' ' + (m[4] ? m[4] : '00:00') + ':00 GMT+0800');
    var diff = (now - d.getTime()) / 1000;
    if (!isNaN(diff) && diff >= 0 && diff < 60) return '刚刚';
    if (!isNaN(diff) && diff >= 0 && diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
    if (!isNaN(diff) && diff >= 0 && diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
    return m[1] + '-' + m[2] + '-' + m[3] + (m[4] ? ' ' + m[4] : '');
  };

  /* ---------------- 复制 ---------------- */
  YQ.copy = function (text) {
    var done = function () { YQ.toast('已复制到剪贴板'); };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () { YQ.copyFallback(text, done); });
    } else { YQ.copyFallback(text, done); }
  };
  YQ.copyFallback = function (text, cb) {
    try {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select(); document.execCommand('copy');
      document.body.removeChild(ta); if (cb) cb();
    } catch (e) { YQ.toast('复制失败，请手动选择', true); }
  };

  /* ---------------- 粒子背景 v2（霓虹星尘 + 数据流，性能优先） ----------------
     成本控制（这层是全站唯一持续逐帧的东西，必须抠）：
       · 桌面 84 粒子 / 手机 36；连线只在桌面画，并用廉价剪枝
       · 粒子用 additive（'lighter'）合成，自带辉光观感，不需要 shadowBlur（后者极贵）
       · 数据流桌面 7 条 / 手机 3 条，只画竖线 + 拖尾渐变
       · 页面切后台就停；系统开了"减少动态"直接不启动
     ------------------------------------------------------------------ */
  YQ.particles = function (canvas) {
    if (typeof canvas === 'string') canvas = YQ.$('#bg');
    if (!canvas || !canvas.getContext) return;
    /* 装饰性背景绝不能把页面搞崩：某些环境（隐私模式/无 canvas）getContext 会抛异常或返回 null */
    var ctx = null;
    try { ctx = canvas.getContext('2d'); } catch (e) { ctx = null; }
    if (!ctx) return;

    var COLORS = [[0, 255, 213], [255, 0, 229], [139, 92, 255], [0, 200, 255]];
    var W = 0, H = 0, ps = [], streams = [], raf = null, running = true, last = 0;
    var small = global.innerWidth < 680;
    var N = small ? 36 : 84;            /* 粒子数 */
    var LINK = small ? 0 : 116;         /* 手机不画连线，省一半以上开销 */
    var NS = small ? 3 : 7;             /* 数据流条数 */

    function resize() {
      var dpr = Math.min(global.devicePixelRatio || 1, small ? 1.5 : 2);   /* 限制 DPR，省像素填充 */
      W = global.innerWidth; H = global.innerHeight;
      canvas.width = Math.floor(W * dpr); canvas.height = Math.floor(H * dpr);
      canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    function pick() { return COLORS[(Math.random() * COLORS.length) | 0]; }
    function seed() {
      ps = [];
      for (var i = 0; i < N; i++) {
        var c = pick();
        ps.push({
          x: Math.random() * W, y: Math.random() * H, r: Math.random() * 1.7 + .5,
          vx: (Math.random() - .5) * .34, vy: (Math.random() - .5) * .34,
          a: Math.random() * .5 + .32, c: c, tw: Math.random() * 6.28
        });
      }
      streams = [];
      for (var k = 0; k < NS; k++) {
        streams.push({
          x: Math.random() * W, y: Math.random() * H,
          len: 70 + Math.random() * 150, sp: .6 + Math.random() * 1.1,
          c: pick(), a: .18 + Math.random() * .22
        });
      }
    }
    function frame(t) {
      if (!running) return;
      ctx.globalCompositeOperation = 'source-over';
      ctx.clearRect(0, 0, W, H);
      ctx.globalCompositeOperation = 'lighter';        /* 叠加混色 = 自带霓虹辉光 */
      var i, j, p;
      /* 数据流（竖线 + 渐隐拖尾） */
      for (i = 0; i < streams.length; i++) {
        var s = streams[i];
        s.y += s.sp;
        if (s.y - s.len > H) { s.y = -s.len; s.x = Math.random() * W; s.c = pick(); }
        var g = ctx.createLinearGradient(s.x, s.y - s.len, s.x, s.y);
        g.addColorStop(0, 'rgba(' + s.c[0] + ',' + s.c[1] + ',' + s.c[2] + ',0)');
        g.addColorStop(1, 'rgba(' + s.c[0] + ',' + s.c[1] + ',' + s.c[2] + ',' + s.a + ')');
        ctx.strokeStyle = g; ctx.lineWidth = 1.4;
        ctx.beginPath(); ctx.moveTo(s.x, s.y - s.len); ctx.lineTo(s.x, s.y); ctx.stroke();
      }
      /* 粒子（带轻微明暗呼吸） */
      for (i = 0; i < ps.length; i++) {
        p = ps[i]; p.x += p.vx; p.y += p.vy; p.tw += .02;
        if (p.x < -20) p.x = W + 20; else if (p.x > W + 20) p.x = -20;
        if (p.y < -20) p.y = H + 20; else if (p.y > H + 20) p.y = -20;
        var alpha = p.a * (0.72 + 0.28 * Math.sin(p.tw));
        ctx.beginPath(); ctx.arc(p.x, p.y, p.r, 0, 6.283);
        ctx.fillStyle = 'rgba(' + p.c[0] + ',' + p.c[1] + ',' + p.c[2] + ',' + alpha + ')';
        ctx.fill();
      }
      /* 邻居连线（仅桌面） */
      if (LINK) {
        for (i = 0; i < ps.length; i++) for (j = i + 1; j < ps.length; j++) {
          var a = ps[i], b = ps[j], dx = a.x - b.x, dy = a.y - b.y;
          if (dx > LINK || dx < -LINK || dy > LINK || dy < -LINK) continue;
          var d = Math.sqrt(dx * dx + dy * dy);
          if (d < LINK) {
            ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y);
            ctx.strokeStyle = 'rgba(0,255,213,' + (.10 * (1 - d / LINK)) + ')'; ctx.lineWidth = 1; ctx.stroke();
          }
        }
      }
      ctx.globalCompositeOperation = 'source-over';
      raf = requestAnimationFrame(frame);
    }
    resize(); seed();
    var rt = null;
    global.addEventListener('resize', function () {
      clearTimeout(rt);                                  /* 防抖：拖动窗口时不反复重建 */
      rt = setTimeout(function () { resize(); seed(); }, 180);
    });
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { running = false; if (raf) cancelAnimationFrame(raf); raf = null; }
      else if (!running) { running = true; frame(); }
    });
    frame();
  };

  /* ---------------- 背景画布（自动创建） ---------------- */
  YQ.bg = function () {
    var c = YQ.$('#bg');
    if (!c) { c = document.createElement('canvas'); c.id = 'bg'; document.body.insertBefore(c, document.body.firstChild); }
    YQ.particles(c);
  };

  /* ---------------- 顶部导航 ---------------- */
  var NAV = [
    { href: '/', txt: '主页', key: 'home' },
    { href: '/forum/', txt: '花果山会议', key: 'forum' },
    { href: '/puzzle/', txt: '每日谜题', key: 'puzzle' },
    { href: '/puzzle/leaderboard', txt: '榜单', key: 'board' },
    { href: '/xiaoheishu', txt: '留言板', key: 'gb' },
    { href: '/cs2bp/', txt: 'CS2 选边', key: 'cs2bp' },
    { href: '/forum/user', txt: '代表档案', key: 'user' }
  ];
  YQ.nav = function (active) {
    var host = YQ.$('#nav');
    if (!host) {
      host = document.createElement('div'); host.id = 'nav';
      document.body.insertBefore(host, document.body.firstChild);
    }
    var html = '<div class="topbar"><a class="logo" href="/">yygq面馆</a><nav>';
    for (var i = 0; i < NAV.length; i++) {
      var n = NAV[i], href = n.href;
      /* 代表档案：登录后直接指向自己的档案，未登录指向登录页 */
      if (n.key === 'user') href = YQ.user ? ('/forum/user?name=' + encodeURIComponent(YQ.user)) : '/forum/login';
      html += '<a href="' + href + '"' + (n.key === active ? ' class="on"' : '') + '>' + n.txt + '</a>';
    }
    html += '</nav><span class="who" id="who"></span></div>';
    host.innerHTML = html;
    YQ.renderWho();
  };
  YQ.renderWho = function () {
    var el = YQ.$('#who');
    if (!el) return;
    var u = YQ.user;
    if (u) {
      var av = YQ.avatarUrl
        ? '<img class="avatar-sm" src="' + YQ.esc(YQ.avatarUrl) + '" alt="" style="width:22px;height:22px">'
        : '';
      el.innerHTML = av + '代表 <b>' + YQ.esc(u) + '</b>' +
        '<a href="/forum/settings">设置</a><a href="javascript:;" id="logoutBtn">退出</a>';
      var b = YQ.$('#logoutBtn');
      if (b) b.onclick = function () { YQ.logout(); };
    } else {
      el.innerHTML = '<a href="/forum/login">登录</a> · <a href="/forum/register">注册</a>';
    }
  };

  /* ---------------- 登录态 ---------------- */
  YQ.user = null;
  YQ.me = function (force) {
    if (YQ._mePromise && !force) return YQ._mePromise;
    YQ._mePromise = YQ.api('/api/auth/me').then(function (r) {
      /* 后端未部署时静默视为未登录，不报错、不白屏 */
      var was = YQ.user;
      YQ.user = (r.ok && r.data && r.data.name) ? r.data.name : null;
      YQ.avatarUrl = (r.ok && r.data && r.data.avatar_url) ? r.data.avatar_url : '';
      if (was !== YQ.user && YQ.$('#nav')) YQ.nav(YQ._activeNav);
      YQ.renderWho();
      return YQ.user;
    });
    return YQ._mePromise;
  };
  YQ.logout = function () {
    YQ.api('/api/auth/logout', { method: 'POST' }).then(function () {
      YQ.user = null; YQ._mePromise = null;
      YQ.toast('已退出');
      setTimeout(function () { location.href = '/forum/'; }, 500);
    });
  };
  YQ.on401 = function () { YQ.user = null; YQ.renderWho(); };

  /* ---------------- 图片压缩（canvas，压到 ≤2MB 再上传） ---------------- */
  YQ.compressImage = function (file, maxBytes, maxSide) {
    maxBytes = maxBytes || 2 * 1024 * 1024;
    maxSide = maxSide || 1600;
    return new Promise(function (resolve, reject) {
      if (!file || !/^image\//.test(file.type)) { reject(new Error('不是图片')); return; }
      if (file.size <= maxBytes && !/png|webp/.test(file.type)) { resolve(file); return; }
      var url = URL.createObjectURL(file);
      var img = new Image();
      img.onload = function () {
        var w = img.naturalWidth, h = img.naturalHeight;
        var scale = Math.min(1, maxSide / Math.max(w, h));
        var cw = Math.max(1, Math.round(w * scale)), ch = Math.max(1, Math.round(h * scale));
        var c = document.createElement('canvas'); c.width = cw; c.height = ch;
        c.getContext('2d').drawImage(img, 0, 0, cw, ch);
        URL.revokeObjectURL(url);
        var q = 0.82, tries = 0;
        (function attempt() {
          c.toBlob(function (blob) {
            if (!blob) { reject(new Error('压缩失败')); return; }
            if (blob.size <= maxBytes || tries >= 5 || q <= 0.4) {
              if (blob.size > maxBytes) { reject(new Error('图片太大，压缩后仍超过 2MB')); return; }
              resolve(blob);
            } else { q -= 0.14; tries++; attempt(); }
          }, 'image/jpeg', q);
        })();
      };
      img.onerror = function () { URL.revokeObjectURL(url); reject(new Error('图片读取失败')); };
      img.src = url;
    });
  };
  YQ.blobToBase64 = function (blob) {
    return new Promise(function (res, rej) {
      var fr = new FileReader();
      fr.onload = function () { var s = String(fr.result); res(s.slice(s.indexOf(',') + 1)); };
      fr.onerror = function () { rej(new Error('读取失败')); };
      fr.readAsDataURL(blob);
    });
  };

  /* ---------------- 简易输入限长计数器 ---------------- */
  YQ.counter = function (inputSel, counterSel, max) {
    var i = YQ.$(inputSel), c = YQ.$(counterSel);
    if (!i || !c) return;
    function up() {
      var n = i.value.length;
      c.textContent = n + ' / ' + max;
      c.className = 'counter' + (n > max ? ' over' : '');
    }
    i.addEventListener('input', up); up();
  };

  /* ---------------- 启动 ---------------- */
  YQ.boot = function (activeNavKey) {
    if (YQ._booted) return; YQ._booted = true;
    YQ._activeNav = activeNavKey;
    YQ.nav(activeNavKey);
    YQ.bg();
    YQ.me();
  };
})(window);

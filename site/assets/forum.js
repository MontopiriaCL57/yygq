/* ============================================================
   yygq面馆 · 花果山会议 forum.js
   术语：帖子=议题、评论=发言、个人主页=代表档案（仅 UI 文案）
   ============================================================ */
(function (global) {
  'use strict';
  var YQ = global.YQ = global.YQ || {};
  var F = YQ.forum = {};

  /* 后端未部署 / 未就绪时的统一提示（绝不白屏） */
  F.serverHint = function (el, msg) {
    if (typeof el === 'string') el = YQ.$(el);
    if (!el) return;
    el.innerHTML = '<div class="empty"><span class="big">🛠</span>' +
      YQ.esc(msg || '会议大厅还在装修（后端服务未就绪）') +
      '<span class="sub">留言板与打卡不受影响，可正常使用</span></div>';
  };
  F.isDown = function (r) { return !r.ok && (r.code === 404 || r.code === 503 || r.code === -1 || r.code === 500); };

  /* 需要登录：没有登录态就跳登录页，并带上回跳地址 */
  F.needLogin = function (next) {
    if (YQ.user) return Promise.resolve(YQ.user);
    return YQ.me(true).then(function (u) {
      if (u) return u;
      location.href = '/forum/login?next=' + encodeURIComponent(next || location.pathname + location.search);
      return null;
    });
  };

  /* 正文渲染：转义后把 [[img:文件名]] 替换为内嵌图（仅限本议题已挂载的图片） */
  F.bodyHtml = function (text, media) {
    var html = YQ.escBr(text || '');
    var map = {};
    (media || []).forEach(function (m) {
      if (m && m.kind === 'image' && m.url) map[String(m.url).split('/').pop().toLowerCase()] = m.url;
    });
    return html.replace(/(?:<br>\s*)?\[\[img:([0-9a-fA-F]{16,64}\.(?:jpg|png|gif|webp))\]\](?:\s*<br>)?/g, function (all, fn) {
      var u = map[String(fn).toLowerCase()];
      if (!u) return all;
      return '<div class="inline-img"><a href="' + YQ.esc(u) + '" target="_blank" rel="noopener">' +
        '<img loading="lazy" src="' + YQ.esc(u) + '" alt="配图"></a></div>';
    });
  };

  /* 媒体渲染（正文内已嵌的图不再重复展示） */
  F.mediaHtml = function (media, text) {
    if (!media || !media.length) return '';
    var used = {};
    var ms = String(text || '').match(/\[\[img:[0-9a-fA-F]{16,64}\.(?:jpg|png|gif|webp)\]\]/g);
    if (ms) ms.forEach(function (t) { used[t.slice(6, -2).toLowerCase()] = true; });
    if (Object.keys(used).length) {
      media = media.filter(function (m) {
        return !(m && m.url && used[String(m.url).split('/').pop().toLowerCase()]);
      });
      if (!media.length) return '';
    }
    var imgs = media.filter(function (m) { return m.kind === 'image'; });
    var vids = media.filter(function (m) { return m.kind === 'video'; });
    var h = '';
    if (imgs.length) {
      h += '<div class="media-grid">' + imgs.map(function (m) {
        return '<a href="' + YQ.esc(m.url) + '" target="_blank" rel="noopener">' +
          '<img loading="lazy" src="' + YQ.esc(m.url) + '" alt="议题配图"></a>';
      }).join('') + '</div>';
    }
    if (vids.length) {
      h += '<div class="media-grid">' + vids.map(function (m) {
        return '<video controls preload="metadata" src="' + YQ.esc(m.url) + '"></video>';
      }).join('') + '</div>';
    }
    return h;
  };

  /* 头像：有图用图，没图用首字母圆牌 */
  F.avatarHtml = function (name, url, cls) {
    cls = cls || 'avatar-sm';
    if (url) return '<img class="' + cls + '" src="' + YQ.esc(url) + '" alt="" loading="lazy">';
    return '<span class="' + cls + ' avatar-fb">' + YQ.esc(String(name || '?').slice(0, 1)) + '</span>';
  };

  /* 议题卡片 */
  F.postHtml = function (p) {
    return '<a class="item" href="/forum/thread?id=' + encodeURIComponent(p.id) + '">' +
      '<div class="gb-hd">' + F.avatarHtml(p.author, p.avatar) +
        '<span class="gb-name">' + YQ.esc(p.author) + '</span>' +
        '<span class="gb-time">' + YQ.esc(YQ.fmtTime(p.created)) + '</span></div>' +
      '<div class="t">' + YQ.esc(p.title) + '</div>' +
      '<div class="snip">' + YQ.esc(p.snippet || '') + (p.len > 120 ? '…' : '') + '</div>' +
      '<div class="meta mt8"><span>发言 <b>' + (p.comments | 0) + '</b></span></div></a>';
  };

  /* 分页控件 */
  F.pager = function (el, page, total, size, go) {
    if (typeof el === 'string') el = YQ.$(el);
    if (!el) return;
    var pages = Math.max(1, Math.ceil(total / size));
    if (pages <= 1) { el.innerHTML = ''; return; }
    var h = '';
    h += page > 1 ? '<a href="javascript:;" data-p="' + (page - 1) + '">‹ 上一页</a>' : '<span class="off">‹ 上一页</span>';
    var from = Math.max(1, page - 2), to = Math.min(pages, from + 4);
    from = Math.max(1, to - 4);
    for (var i = from; i <= to; i++) {
      h += i === page ? '<span class="cur">' + i + '</span>' : '<a href="javascript:;" data-p="' + i + '">' + i + '</a>';
    }
    h += page < pages ? '<a href="javascript:;" data-p="' + (page + 1) + '">下一页 ›</a>' : '<span class="off">下一页 ›</span>';
    h += '<span class="off">共 ' + total + ' 条 / ' + pages + ' 页</span>';
    el.innerHTML = h;
    YQ.$$('a[data-p]', el).forEach(function (a) {
      a.onclick = function () { go(parseInt(a.getAttribute('data-p'), 10)); };
    });
  };

  /* 上传：图片先 canvas 压缩，再 base64 JSON 传；视频 raw 流式传 */
  F.upload = function (file, kind, onProgress) {
    if (kind === 'image') {
      return YQ.compressImage(file, 2 * 1024 * 1024, 1600).then(function (blob) {
        if (onProgress) onProgress('压缩完成 ' + Math.round(blob.size / 1024) + 'KB，上传中…');
        return YQ.blobToBase64(blob).then(function (b64) {
          return YQ.api('/api/upload', {
            method: 'POST', timeout: 60000,
            body: { kind: 'image', data: b64, name: file.name }
          });
        });
      }).then(function (r) {
        if (!r.ok) throw new Error(r.msg || '上传失败');
        return r.data.file.file;
      });
    }
    /* 视频：JSON 太膨胀，走原始流（Content-Type 直接用文件的） */
    if (file.size > 20 * 1024 * 1024) return Promise.reject(new Error('视频超过 20MB'));
    if (onProgress) onProgress('上传中 ' + (file.size / 1048576).toFixed(1) + 'MB…');
    return YQ.api('/api/upload', {
      method: 'POST', timeout: 180000,
      headers: { 'Content-Type': file.type || 'application/octet-stream' },
      body: file
    }).then(function (r) {
      if (!r.ok) throw new Error(r.msg || '上传失败');
      return r.data.file.file;
    });
  };

  /* 组装一条发言 */
  F.commentHtml = function (c) {
    return '<div class="cmt" id="c' + c.id + '">' +
      '<div class="cmt-h">' + F.avatarHtml(c.author, c.avatar, 'avatar-sm') +
      '<b>' + YQ.esc(c.author) + '</b>' +
      '<span>' + YQ.esc(YQ.fmtTime(c.created)) + '</span>' +
      '<a class="cmt-u" href="/forum/user?name=' + encodeURIComponent(c.author) + '">档案</a></div>' +
      '<div class="cmt-b">' + YQ.escBr(c.body) + '</div></div>';
  };

  /* 代表档案链接 */
  F.userLink = function (name) {
    return '<a href="/forum/user?name=' + encodeURIComponent(name) + '">' + YQ.esc(name) + '</a>';
  };
})(window);

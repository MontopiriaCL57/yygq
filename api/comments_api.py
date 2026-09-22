#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# yygq面馆 · 全站 API（v3）
#   留言板 + 每日打卡（与 v2 行为完全一致）
#   + 统一账号 / 花果山会议论坛 / 上传 / 每日谜题
# 零第三方依赖（仅标准库）；单文件部署，容器内路径 /app/comments_api.py
# 数据：/data/comments.json、/data/checkin.json（沿用）+ /data/site.db（SQLite WAL）
#      /data/uploads/（上传文件）+ /data/puzzle/（题库只读副本）
#
# v3 变更（相对 v2）：
#   (1) HTTPServer -> 有界线程池（8 并发 + 16 排队），慢连接不再拖死全站
#   (2) 共享状态加锁：限流器、JSON 文件写、SQLite 写
#   (3) 统一账号：scrypt 加盐哈希 + HMAC 签名 Cookie（服务端零会话表）
#   (4) 论坛 / 上传 / 谜题 接口
#   (5) 谜题计分以后端为准（HMAC 结果票据，客户端无法直接篡改分数）
import base64
import hashlib
import hmac
import json
import os
import random
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ---------------------------------------------------------------- 基础配置
_TZ = timezone(timedelta(hours=8))


def _now():
    return datetime.now(_TZ)


def _now_str():
    return _now().strftime('%Y-%m-%d %H:%M:%S')


def _today():
    return _now().strftime('%Y-%m-%d')


DATA_FILE = os.environ.get('DATA_FILE', '/data/comments.json')
CHECKIN_FILE = os.environ.get('CHECKIN_FILE', '/data/checkin.json')
POEMS_FILE = os.environ.get('POEMS_FILE', '/data/poems.json')
DB_FILE = os.environ.get('DB_FILE', '/data/site.db')
SCHEMA_FILE = os.environ.get('SCHEMA_FILE', '/data/schema.sql')
UPLOAD_DIR = os.environ.get('UPLOAD_DIR', '/data/uploads')
BANK_DIR = os.environ.get('BANK_DIR', '/data/puzzle')
PORT = int(os.environ.get('PORT', '8808'))
SECRET = os.environ.get('SECRET', '')
MAX_BODY = 1000000          # JSON 请求体上限 1MB（沿用 v2）
READ_TIMEOUT = 15           # 单连接读超时（秒）
MAX_IMAGE = 2 * 1024 * 1024     # 上传图片 2MB
MAX_VIDEO = 20 * 1024 * 1024    # 上传视频 20MB
BIG_BODY = MAX_VIDEO + 4096     # 原始流式上传的硬顶
UPLOAD_JSON_CAP = 3 * 1024 * 1024   # base64 后的 JSON 体积上限（图片 2MB → base64 ≈ 2.7MB）
# 论坛限额（2026-09-12 站长要求放开；同时取消视频投稿）
LIMIT_TITLE = 80         # 议题标题字数
LIMIT_BODY = 50000       # 议题正文字数
LIMIT_COMMENT = 5000     # 发言字数
LIMIT_POST_IMAGES = 20   # 单个议题最多配图张数（UC 2026-09-12 上调 9→20）
DRAIN_LIMIT = 8 * 1024 * 1024   # 超限时最多再读掉 8MB 再回 413（让客户端收得到消息）
SESSION_DAYS = 30
POOL_WORKERS = int(os.environ.get('POOL_WORKERS', '8'))

# 谜题计分（与前端 assets/puzzle-engine.js 的 P.CONFIG 一致）
PUZZLE_CONF = {'BASE': 100, 'PENALTY': 15, 'FLOOR': 40,
               'COMBO_STEP': 0.10, 'COMBO_MAX': 1.00, 'DAILY': 2}

if not SECRET:
    # 没给 SECRET 时用固定文件里的随机密钥兜底，避免重启后全体掉线
    _sf = os.path.join(os.path.dirname(DB_FILE) or '/data', '.site_secret')
    try:
        if os.path.exists(_sf):
            with open(_sf, 'r', encoding='utf-8') as _f:
                SECRET = _f.read().strip()
        if not SECRET:
            SECRET = secrets.token_hex(24)
            with open(_sf, 'w', encoding='utf-8') as _f:
                _f.write(SECRET)
    except Exception:
        SECRET = SECRET or secrets.token_hex(24)

# 会话签名密钥由管理密钥派生（域分离）：管理密钥走 URL query 会进 nginx 日志，
# 即使泄漏也不能用来伪造他人登录态（见审查 P1-5）。
_SIGN_KEY = hashlib.sha256(('yygq/session/v1|' + SECRET).encode('utf-8')).digest()

# ---------------------------------------------------------------- 诗库 / 彩蛋
_POEMS_FALLBACK = ["醉后不知天在水，满船清梦压星河", "曾经沧海难为水，除却巫山不是云",
                   "行到水穷处，坐看云起时", "人生如逆旅，我亦是行人"]


def _load_poems():
    if os.path.exists(POEMS_FILE):
        try:
            with open(POEMS_FILE, 'r', encoding='utf-8') as f:
                arr = json.load(f)
            if isinstance(arr, list) and arr:
                return arr
        except Exception:
            pass
    return _POEMS_FALLBACK


POEMS = _load_poems()
EGGS = {117: '/checkin/117.jpg', 125: '/checkin/125.png', 218: '/checkin/218.png', 305: '/checkin/305.jpg'}

# ---------------------------------------------------------------- 文件读写锁
_FILE_LOCK = threading.RLock()


def _load_file(path, default):
    if os.path.exists(path):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            return default
    return default


def _save_file(path, obj):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def get_egg(days):
    best = None
    for d in sorted(EGGS):
        if days >= d:
            best = EGGS[d]
    return best


# ---------------------------------------------------------------- 限流（滑窗）
_RATE = {}
_RATE_LOCK = threading.Lock()


def _rate_ok(key, limit, window=60):
    now = time.time()
    with _RATE_LOCK:
        if len(_RATE) > 5000:
            _RATE.clear()
        h = _RATE.setdefault(key, [])
        h[:] = [t for t in h if now - t < window]
        if len(h) >= limit:
            return False
        h.append(now)
        return True


# ---------------------------------------------------------------- SQLite
SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,
  name_lower TEXT NOT NULL UNIQUE,
  salt       TEXT NOT NULL,
  hash       TEXT NOT NULL,
  created    TEXT NOT NULL,
  last_login TEXT,
  ip         TEXT,
  avatar     TEXT,                 -- 头像文件名（uploads 内，160×160 小图）
  bio        TEXT                  -- 个人简介 ≤200 字
);
CREATE TABLE IF NOT EXISTS posts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,
  body          TEXT NOT NULL,
  author        TEXT NOT NULL,
  created       TEXT NOT NULL,
  ip            TEXT,
  deleted       INTEGER NOT NULL DEFAULT 0,
  comment_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_alive ON posts(deleted, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author, id DESC);
CREATE TABLE IF NOT EXISTS comments (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id  INTEGER NOT NULL,
  body     TEXT NOT NULL,
  author   TEXT NOT NULL,
  created  TEXT NOT NULL,
  ip       TEXT,
  deleted  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id, deleted, id);
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(author, id DESC);
CREATE TABLE IF NOT EXISTS post_media (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL,
  kind    TEXT NOT NULL,
  file    TEXT NOT NULL,
  size    INTEGER NOT NULL DEFAULT 0,
  mime    TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_post ON post_media(post_id);
CREATE TABLE IF NOT EXISTS uploads (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  file    TEXT NOT NULL UNIQUE,
  kind    TEXT NOT NULL,
  size    INTEGER NOT NULL DEFAULT 0,
  mime    TEXT,
  owner   TEXT NOT NULL,
  created TEXT NOT NULL,
  used    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_uploads_owner ON uploads(owner, id DESC);
CREATE TABLE IF NOT EXISTS puzzle_results (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  day     TEXT NOT NULL,
  qid     TEXT NOT NULL,
  user    TEXT NOT NULL,
  cat     TEXT,
  type    TEXT,
  wrong   INTEGER NOT NULL DEFAULT 0,
  solved  INTEGER NOT NULL DEFAULT 0,
  score   INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL,
  UNIQUE(day, qid, user)
);
CREATE INDEX IF NOT EXISTS idx_pr_day ON puzzle_results(day, score DESC);
CREATE INDEX IF NOT EXISTS idx_pr_user ON puzzle_results(user, day DESC);
CREATE INDEX IF NOT EXISTS idx_pr_total ON puzzle_results(solved, score DESC);
"""

_DB_LOCAL = threading.local()
_DB_INIT_LOCK = threading.Lock()
_DB_READY = [False]


def _db():
    """每线程一个连接（线程池 ≤8，故连接数有界）。"""
    conn = getattr(_DB_LOCAL, 'conn', None)
    if conn is None:
        conn = sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)
        conn.execute('PRAGMA busy_timeout=8000')
        conn.execute('PRAGMA journal_mode=WAL')
        _DB_LOCAL.conn = conn
    return conn


def _migrate(conn):
    """给已存在的老库补列（幂等）——CREATE TABLE IF NOT EXISTS 不会修改老表。"""
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(users)').fetchall()}
        if 'avatar' not in cols:
            conn.execute('ALTER TABLE users ADD COLUMN avatar TEXT')
            print('  migrate: users.avatar added', flush=True)
        if 'bio' not in cols:
            conn.execute('ALTER TABLE users ADD COLUMN bio TEXT')
            print('  migrate: users.bio added', flush=True)
        if 'uid' not in cols:
            # UC: 随机 UID（8 位整数，全局唯一）——账号身份与显示名解耦
            conn.execute('ALTER TABLE users ADD COLUMN uid INTEGER')
            used = set()
            for (rid,) in conn.execute('SELECT id FROM users').fetchall():
                while True:
                    u = random.randint(10000000, 99999999)
                    if u not in used:
                        break
                used.add(u)
                conn.execute('UPDATE users SET uid=? WHERE id=?', (u, rid))
            print('  migrate: users.uid added', flush=True)
        # 索引在新旧库都确保存在（不能放进 schema.sql：老库执行 schema 时该列还不存在，会中断整段初始化）
        conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_users_uid ON users(uid)')
    except Exception:
        traceback.print_exc()


def _db_init():
    with _DB_INIT_LOCK:
        if _DB_READY[0]:
            return
        d = os.path.dirname(DB_FILE)
        if d and not os.path.isdir(d):
            os.makedirs(d, exist_ok=True)
        sql = SCHEMA_SQL
        if os.path.exists(SCHEMA_FILE):
            try:
                with open(SCHEMA_FILE, 'r', encoding='utf-8') as f:
                    ext = f.read()
                if 'CREATE TABLE' in ext:
                    sql = ext
            except Exception:
                pass
        conn = sqlite3.connect(DB_FILE, timeout=10, isolation_level=None)
        try:
            conn.executescript(sql)
            _migrate(conn)
        finally:
            conn.close()
        _DB_READY[0] = True


def _q(sql, args=(), one=False):
    _db_init()
    cur = _db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    return (rows[0] if rows else None) if one else rows


def _x(sql, args=()):
    _db_init()
    cur = _db().execute(sql, args)
    rid = cur.lastrowid
    cur.close()
    return rid


def _run(sql, args=()):
    """执行写语句并返回受影响行数（用于「原子占用」这类需要判断 rowcount 的场景）"""
    _db_init()
    cur = _db().execute(sql, args)
    n = cur.rowcount
    cur.close()
    return n


# ---------------------------------------------------------------- 口令 / 会话
_NAME_RE = re.compile(r'[0-9A-Za-z_\u4e00-\u9fff]{3,16}\Z')


def _hash_pw(password, salt_hex=None):
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    dk = hashlib.scrypt(password.encode('utf-8'), salt=salt, n=2 ** 14, r=8, p=1,
                        dklen=32, maxmem=64 * 1024 * 1024)
    return salt.hex(), dk.hex()


def _verify_pw(password, salt_hex, hash_hex):
    try:
        _, got = _hash_pw(password, salt_hex)
        return hmac.compare_digest(got, hash_hex)
    except Exception:
        return False


def _sign(payload):
    raw = base64.urlsafe_b64encode(json.dumps(payload, ensure_ascii=False,
                                               separators=(',', ':')).encode('utf-8')).decode('ascii')
    sig = hmac.new(_SIGN_KEY, ('p1:' + raw).encode('utf-8'), hashlib.sha256).hexdigest()
    return raw + '.' + sig


def _unsign(token):
    try:
        raw, sig = token.split('.', 1)
        want = hmac.new(_SIGN_KEY, ('p1:' + raw).encode('utf-8'), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, want):
            return None
        return json.loads(base64.urlsafe_b64decode(raw.encode('ascii')).decode('utf-8'))
    except Exception:
        return None


_CAP_USED = {}
_CAP_LOCK = threading.Lock()


def _captcha_new():
    a = random.randint(2, 9)
    b = random.randint(2, 9)
    op = random.choice(['+', '-', '×'])
    if op == '+':
        ans = a + b
    elif op == '-':
        if a < b:
            a, b = b, a
        ans = a - b
    else:
        ans = a * b
    tok = _sign({'a': ans, 'e': int(time.time()) + 900, 'n': secrets.token_hex(4)})
    return '%d %s %d = ?' % (a, op, b), tok


def _captcha_check(token, answer):
    d = _unsign(token or '')
    if not d or 'a' not in d:
        return False, '验证码已失效，请刷新'
    if int(d.get('e', 0)) < int(time.time()):
        return False, '验证码过期，请刷新'
    key = str(d.get('n', ''))
    with _CAP_LOCK:
        if key in _CAP_USED:
            return False, '验证码已用过，请刷新'
        if len(_CAP_USED) > 4000:
            _CAP_USED.clear()
        _CAP_USED[key] = time.time()
    try:
        if int(str(answer).strip()) != int(d['a']):
            return False, '验证码不对哦'
    except Exception:
        return False, '验证码不对哦'
    return True, ''


def _session_token(name):
    return _sign({'n': name, 'e': int(time.time()) + SESSION_DAYS * 86400})


def _session_name(token):
    d = _unsign(token or '')
    if not d or 'n' not in d or int(d.get('e', 0)) < int(time.time()):
        return None
    name = str(d['n'])
    row = _q('SELECT name FROM users WHERE name_lower=?', (name.lower(),), one=True)
    return row[0] if row else None


# ---------------------------------------------------------------- 题库（服务端只读副本）
_BANK = {'mtime': 0.0, 'data': None}
_BANK_LOCK = threading.Lock()


def _load_bank():
    path = os.path.join(BANK_DIR, 'all.json')
    try:
        mt = os.path.getmtime(path)
    except Exception:
        return None
    with _BANK_LOCK:
        if _BANK['data'] is not None and _BANK['mtime'] == mt:
            return _BANK['data']
        try:
            with open(path, 'r', encoding='utf-8') as f:
                b = json.load(f)
            qs = sorted(b.get('questions', []), key=lambda q: str(q.get('id', '')))
            bank = {'version': b.get('version', 0), 'questions': qs}
        except Exception:
            return None
        _BANK['mtime'] = mt
        _BANK['data'] = bank
        return bank


def _load_pool(cat):
    path = os.path.join(BANK_DIR, 'pools', str(cat) + '.json')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return None


def _fnv1a(s):
    h = 2166136261
    for ch in s.encode('utf-8'):
        h ^= ch
        h = (h * 16777619) & 0xFFFFFFFF
    return h


def _mulberry32(seed):
    a = seed & 0xFFFFFFFF

    def rnd():
        nonlocal a
        a = (a + 0x6D2B79F5) & 0xFFFFFFFF
        t = (a ^ (a >> 15)) & 0xFFFFFFFF
        t = (t * ((1 | a) & 0xFFFFFFFF)) & 0xFFFFFFFF
        t2 = ((t ^ (t >> 7)) * ((61 | t) & 0xFFFFFFFF)) & 0xFFFFFFFF
        t = ((t + t2) ^ t) & 0xFFFFFFFF
        return ((t ^ (t >> 14)) & 0xFFFFFFFF) / 4294967296.0
    return rnd


def _order(n, salt):
    r = _mulberry32(_fnv1a(salt))
    a = list(range(n))
    i = n - 1
    while i > 0:
        j = int(r() * (i + 1))
        a[i], a[j] = a[j], a[i]
        i -= 1
    return a


def _day_index(day):
    try:
        y, m, d = [int(x) for x in str(day).split('-')]
        return (datetime(y, m, d).date() - datetime(2026, 1, 1).date()).days
    except Exception:
        return 0


def _pick_daily(bank, day):
    """每日两题：一枚属性对照 + 一枚其他形式，且两题分类不同。
    第二题在全局轮转顺序里从「当天位置」往后取第一枚异分类的题——
    既保证跨分类多样性，又保持逐日轮转、长期不重复。
    必须与前端 assets/puzzle-engine.js 的 P.pickDaily 完全一致。"""
    attr = [q for q in bank['questions'] if q.get('type') == 'attr']
    other = [q for q in bank['questions'] if q.get('type') != 'attr']
    if not attr or not other:
        return []
    di = _day_index(day)
    oa = _order(len(attr), 'yygq-attr-v2')
    q1 = attr[oa[di % len(attr)]]
    ob = _order(len(other), 'yygq-other-v2')
    start = di % len(other)
    q2 = None
    for k in range(len(other)):
        cand = other[ob[(start + k) % len(other)]]
        if cand.get('cat') != q1.get('cat'):
            q2 = cand
            break
    if q2 is None:
        q2 = other[ob[start]]
    return [q1, q2]


def _norm(s):
    s = str(s if s is not None else '')
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            o -= 0xFEE0
        ch = chr(o)
        if ch.isspace() or ch in "·・.。、,，-_'\"「」《》()（）!！?？:：;；":
            continue
        out.append(ch.lower())
    return ''.join(out)


def _answer_ok(guess, q, pool_ent):
    g = _norm(guess)
    if not g:
        return False
    cands = [q.get('answer', '')]
    cands.extend(q.get('aliases', []) or [])
    if pool_ent:
        cands.extend(pool_ent.get('aliases', []) or [])
    for c in cands:
        if _norm(c) == g:
            return True
    return False


def _resolve_entity(guess, pool):
    g = _norm(guess)
    if not g or not pool:
        return None
    for e in pool.get('entities', []):
        if _norm(e.get('name', '')) == g:
            return e
        for al in (e.get('aliases') or []):
            if _norm(al) == g:
                return e
    return None


def _compare(field, gv, av):
    t = field.get('t', 'eq')
    if t == 'num':
        try:
            a = float(gv)
            b = float(av)
        except Exception:
            a = b = 0.0
        if a == b:
            return {'state': 'hit', 'txt': str(gv)}
        if a > b:
            return {'state': 'down', 'txt': str(gv) + ' ↓'}
        return {'state': 'up', 'txt': str(gv) + ' ↑'}
    if t == 'set':
        ga = gv if isinstance(gv, list) else [gv]
        aa = av if isinstance(av, list) else [av]
        inter = [x for x in ga if x in aa]
        if len(inter) == len(ga) and len(ga) == len(aa):
            return {'state': 'hit', 'txt': '/'.join(str(x) for x in ga)}
        if inter:
            return {'state': 'part', 'txt': '/'.join(str(x) for x in ga) + ' ◐' + '/'.join(str(x) for x in inter)}
        return {'state': 'miss', 'txt': '/'.join(str(x) for x in ga)}
    same = str(gv) == str(av)
    return {'state': 'hit' if same else 'miss', 'txt': str(gv)}


def _puzzle_score(wrong, streak):
    base = max(PUZZLE_CONF['FLOOR'], PUZZLE_CONF['BASE'] - PUZZLE_CONF['PENALTY'] * max(0, int(wrong)))
    cap = int(PUZZLE_CONF['COMBO_MAX'] / PUZZLE_CONF['COMBO_STEP'])
    mult = 1.0 + max(0, min(int(streak), cap)) * PUZZLE_CONF['COMBO_STEP']
    # 必须用「四舍五入」而不是 Python 内置 round()（银行家舍入）：
    # base=55, mult=1.5 → 82.5，round() 给 82，前端 Math.round 给 83，两边会不一致。
    return int(base * mult + 0.5), base


# ---- 谜题猜测的服务端记账（内存，用于成绩权威化；重启后记为零，属可接受退化） ----
_GUESS = {}
_GUESS_LOCK = threading.Lock()


def _guess_slot(user, day, qid):
    k = '%s\x00%s\x00%s' % (user, day, qid)
    with _GUESS_LOCK:
        if len(_GUESS) > 20000:
            _GUESS.clear()
        return _GUESS.setdefault(k, {'w': 0, 'ok': False, 'seen': False})


def _new_uid():
    """生成全局唯一的 8 位随机 UID。"""
    for _ in range(64):
        u = random.randint(10000000, 99999999)
        if not _q('SELECT id FROM users WHERE uid=?', (u,), one=True):
            return u
    return random.randint(100000000, 999999999)


def _avatars_for(names):
    """批量取「用户名小写 → 头像文件名」，一次查询搞定，避免 N+1。"""
    out, uniq, seen = {}, [], set()
    for n in names:
        k = str(n or '').strip().lower()
        if k and k not in seen:
            seen.add(k); uniq.append(k)
    for i in range(0, len(uniq), 400):
        chunk = uniq[i:i + 400]
        ph = ','.join('?' * len(chunk))
        try:
            for r in _q('SELECT name_lower, avatar FROM users WHERE avatar IS NOT NULL AND name_lower IN (%s)' % ph, tuple(chunk)):
                out[r[0]] = r[1]
        except Exception:
            pass
    return out


def _date_from_index(idx):
    d = datetime(2026, 1, 1).date() + timedelta(days=int(idx))
    return d.strftime('%Y-%m-%d')


# ---------------------------------------------------------------- 上传文件
_IMG_EXT = {'jpg': 'image/jpeg', 'png': 'image/png', 'gif': 'image/gif', 'webp': 'image/webp'}
_VID_EXT = {'mp4': 'video/mp4', 'webm': 'video/webm'}
_FILE_RE = re.compile(r'[0-9a-f]{16,64}\.(?:jpg|png|gif|webp|mp4|webm)\Z')
_MEDIA_SEM = threading.BoundedSemaphore(4)      # 同时最多 4 路【视频】流（图片不限，见 _serve_media）


def _sniff(b):
    if b[:3] == b'\xff\xd8\xff':
        return 'jpg'
    if b[:8] == b'\x89PNG\r\n\x1a\n':
        return 'png'
    if b[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if b[:4] == b'RIFF' and b[8:12] == b'WEBP':
        return 'webp'
    if len(b) > 12 and b[4:8] == b'ftyp':
        return 'mp4'
    if b[:4] == b'\x1a\x45\xdf\xa3':
        return 'webm'
    return None


def _save_upload(kind, data, owner):
    ext = _sniff(data[:32])
    if not ext:
        return None, '文件格式不认识（只收 jpg/png/gif/webp 图片，mp4/webm 视频）'
    if kind == 'image' and ext not in _IMG_EXT:
        return None, '这不是图片'
    if kind == 'video' and ext not in _VID_EXT:
        return None, '这不是视频'
    if kind == 'image' and len(data) > MAX_IMAGE:
        return None, '图片超过 2MB'
    if kind == 'video' and len(data) > MAX_VIDEO:
        return None, '视频超过 20MB'
    name = secrets.token_hex(16) + '.' + ext
    if not os.path.isdir(UPLOAD_DIR):
        os.makedirs(UPLOAD_DIR, exist_ok=True)
    path = os.path.join(UPLOAD_DIR, name)
    tmp = path + '.part'
    with open(tmp, 'wb') as f:
        f.write(data)
    os.replace(tmp, path)
    mime = _IMG_EXT.get(ext) or _VID_EXT.get(ext) or 'application/octet-stream'
    _x('INSERT INTO uploads(file,kind,size,mime,owner,created,used) VALUES(?,?,?,?,?,?,0)',
       (name, kind, len(data), mime, owner, _now_str()))
    return {'file': name, 'kind': kind, 'size': len(data), 'mime': mime,
            'url': '/api/media/' + name}, ''


def _gc_uploads(max_age_h=48):
    """回收超期且没有被议题引用的上传文件。2 核 2G 的小盘必须自己收垃圾：
    否则单个账号就能把磁盘灌满，SQLite 写失败会连带旧接口一起 500。"""
    n = 0
    try:
        limit = (_now() - timedelta(hours=int(max_age_h))).strftime('%Y-%m-%d %H:%M:%S')
        rows = _q('SELECT file FROM uploads WHERE used=0 AND created < ?', (limit,))
        for row in rows:
            fn = row[0]
            if not _FILE_RE.match(fn or ''):
                continue
            try:
                os.remove(os.path.join(UPLOAD_DIR, fn))
            except Exception:
                pass
            try:
                _x('DELETE FROM uploads WHERE file=? AND used=0', (fn,))
            except Exception:
                pass
            n += 1
    except Exception:
        return n
    return n


def _gc_loop():
    while True:
        time.sleep(6 * 3600)
        try:
            _gc_uploads()
        except Exception:
            pass


# ================================================================ HTTP 处理
class Handler(BaseHTTPRequestHandler):
    # 用 HTTP/1.0 + 每次响应后关连接：
    #   v2 线上就是这个模型，稳；而且能一次性消除 keep-alive 的三类风险
    #   （连接空闲占满线程池、GET 带 body 造成请求错位、204/413 语义问题）。
    #   上游是同机 nginx，连接开销可忽略；小站 8 并发足够。
    protocol_version = 'HTTP/1.0'
    server_version = 'yygq/3.0'

    # ---------------- 基础设施 ----------------
    def setup(self):
        super().setup()
        try:
            self.request.settimeout(READ_TIMEOUT)
        except Exception:
            pass

    def send_response(self, code, message=None):
        BaseHTTPRequestHandler.send_response(self, code, message)
        self.close_connection = True
        self.send_header('Connection', 'close')

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        try:
            body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        except Exception:
            body = b'{"code":1,"msg":"server error"}'
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _err(self, code, msg):
        self._send(code, {'code': code if code >= 400 else 1, 'msg': msg})

    def _drain(self, length, limit=DRAIN_LIMIT):
        """把超限的请求体读掉丢弃，让客户端能把话说完、从而收到我们的 413，
        而不是写到一半被 RST（原先直连后端时会看到 "connection reset" 而非友好提示）。
        超过 limit 就放弃并关连接，避免被巨量 body 拖住。"""
        left = length
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                return False
            left -= len(chunk)
            if length - left > limit:
                self.close_connection = True
                return False
        return True

    def _body(self, cap=None):
        """返回 None 表示超限；{} 表示空或无效（沿用 v2 语义）"""
        cap = MAX_BODY if cap is None else cap
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            length = 0
        if length > cap:
            self.close_connection = True
            if length <= DRAIN_LIMIT:
                self._drain(length)
            return None
        if not length:
            return {}
        try:
            d = json.loads(self.rfile.read(length).decode('utf-8'))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _read_raw(self, cap):
        """流式读原始 body，超过 cap 返回 (None, True)"""
        try:
            length = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b'', False
        if length > cap:
            self.close_connection = True
            if length <= DRAIN_LIMIT:
                self._drain(length)
            return None, True
        buf = bytearray()
        left = length
        while left > 0:
            chunk = self.rfile.read(min(65536, left))
            if not chunk:
                break
            buf.extend(chunk)
            left -= len(chunk)
        return bytes(buf), False

    def _client_ip(self):
        ip = (self.headers.get('X-Real-IP') or '').strip()
        if not ip:
            xff = self.headers.get('X-Forwarded-For') or ''
            parts = [p.strip() for p in xff.split(',') if p.strip()]
            if parts:
                ip = parts[-1]
        return (ip or self.client_address[0])[:64]

    def _cookie(self, name):
        raw = self.headers.get('Cookie') or ''
        for part in raw.split(';'):
            if '=' in part:
                k, v = part.split('=', 1)
                if k.strip() == name:
                    return unquote(v.strip())
        return ''

    def _me(self):
        return _session_name(self._cookie('yq_user'))

    def _is_admin(self, q):
        if not SECRET:
            return False
        got = q.get('key', [''])[0]
        try:
            return hmac.compare_digest(got, SECRET)     # 恒定时间比较，避免计时侧信道
        except Exception:
            return False

    # ---------------- 路由 ----------------
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Max-Age', '86400')
        self.end_headers()

    def do_GET(self):
        try:
            self._route_get()
        except Exception:
            traceback.print_exc()
            self._err(500, '服务器内部错误')

    def do_POST(self):
        try:
            self._route_post()
        except Exception:
            traceback.print_exc()
            self._err(500, '服务器内部错误')

    def do_DELETE(self):
        try:
            self._route_delete()
        except Exception:
            traceback.print_exc()
            self._err(500, '服务器内部错误')

    # ============================================ GET
    def _route_get(self):
        parsed = urlparse(self.path)
        path = parsed.path
        q = parse_qs(parsed.query)

        # ---- 旧接口：留言板（行为与 v2 完全一致） ----
        if path == '/api/comments':
            show_ip = self._is_admin(q)
            with _FILE_LOCK:
                c = _load_file(DATA_FILE, [])
                c.reverse()
            c = [dict(x) for x in c]
            # UC: 头像只挂「登录绑定」的留言（acct）；匿名留言一律不挂头像，防冒名
            bd = [x for x in c[:300] if x.get('acct')]
            amap3 = _avatars_for([x['acct'] for x in bd]) if bd else {}
            for x in bd:
                x['bound'] = 1
                av = amap3.get(str(x.get('acct', '')).lower(), '')
                if av:
                    x['avatar'] = '/api/media/' + av
            if not show_ip:
                # UC: 公共视图隐藏 ip / acct / uid（bound、avatar 保留）
                c = [{k: v for k, v in x.items() if k not in ('ip', 'acct', 'uid')} for x in c]
            self._send(200, {"code": 0, "comments": c})
            return

        # ---- 旧接口：打卡榜（行为与 v2 完全一致） ----
        if path == '/api/checkin/board':
            with _FILE_LOCK:
                data = _load_file(CHECKIN_FILE, {})
            today = _today()
            board = []
            for v in data.values():
                if not v:
                    continue
                b = {"name": v.get('name', ''), "days": v.get('days', 0)}
                hit = (v.get('last_date') == today)
                b['today'] = '数据更新成功' if hit else '你是？查无此人！'
                b['time'] = v.get('last_time', '') if hit else None
                board.append(b)
            board.sort(key=lambda x: -x['days'])
            self._send(200, {"code": 0, "board": board})
            return

        # ---- 旧接口：打卡状态 ----
        if path == '/api/checkin/status':
            card = q.get('card', [''])[0]
            with _FILE_LOCK:
                data = _load_file(CHECKIN_FILE, {})
            rec = data.get(card)
            if rec:
                self._send(200, {"code": 0, "name": rec.get('name'), "days": rec.get('days', 0),
                                 "egg": get_egg(rec.get('days', 0))})
            else:
                self._send(200, {"code": 0, "name": None, "days": 0, "egg": None})
            return

        # ---- 静态媒体（上传文件对外访问；无需改 nginx） ----
        if path.startswith('/api/media/'):
            self._serve_media(path[len('/api/media/'):])
            return

        # ---- 账号 ----
        if path == '/api/auth/me':
            name = self._me()
            if not name:
                self._send(200, {"code": 0, "name": None})
                return
            row = _q('SELECT name, created, avatar, bio FROM users WHERE name_lower=?', (name.lower(),), one=True)
            if not row:
                self._send(200, {"code": 0, "name": None})
                return
            self._send(200, {"code": 0, "name": row[0], "created": row[1],
                             "avatar": row[2] or '', "bio": row[3] or '',
                             "avatar_url": ('/api/media/' + row[2]) if row[2] else ''})
            return

        if path == '/api/auth/captcha':
            qt, tok = _captcha_new()
            self._send(200, {"code": 0, "q": qt, "token": tok})
            return

        # ---- 论坛 ----
        if path == '/api/forum/posts':
            self._forum_list(q)
            return

        m = re.match(r'^/api/forum/posts/(\d+)$', path)
        if m:
            self._forum_detail(int(m.group(1)), q)
            return

        m = re.match(r'^/api/forum/posts/(\d+)/comments$', path)
        if m:
            self._forum_comments(int(m.group(1)), q)
            return

        m = re.match(r'^/api/forum/users/(.+)$', path)
        if m:
            self._forum_user(unquote(m.group(1)), q)
            return

        # ---- 谜题 ----
        if path == '/api/puzzle/today':
            self._puzzle_today(q)
            return

        if path == '/api/puzzle/leaderboard':
            self._puzzle_leaderboard(q)
            return

        if path == '/api/puzzle/stats':
            self._puzzle_stats(q)
            return

        if path == '/api/puzzle/archive':
            self._puzzle_archive(q)
            return

        self._send(404, {"code": 1, "msg": "not found"})

    # ============================================ POST
    def _route_post(self):
        path = urlparse(self.path).path

        # 原始流式上传（视频）不走 JSON 解析
        if path == '/api/upload':
            ctype = (self.headers.get('Content-Type') or '').lower()
            if 'application/json' in ctype:
                body = self._body(cap=UPLOAD_JSON_CAP)
                if body is None:
                    self._send(413, {"code": 1, "msg": "内容太大啦"})
                    return
                self._upload_json(body)
            else:
                self._upload_raw(ctype)
            return

        body = self._body()
        if body is None:
            self._send(413, {"code": 1, "msg": "内容太大啦"})
            return

        # ---- 旧接口：发留言（行为与 v2 完全一致） ----
        if path == '/api/comments':
            self._post_comment(body)
            return

        # ---- 旧接口：打卡（行为与 v2 完全一致） ----
        if path == '/api/checkin':
            self._post_checkin(body)
            return

        # ---- 账号 ----
        if path == '/api/auth/register':
            self._auth_register(body)
            return
        if path == '/api/auth/login':
            self._auth_login(body)
            return
        if path == '/api/auth/logout':
            self._auth_logout()
            return

        # ---- 个人资料（头像 + 简介） ----
        if path == '/api/profile':
            self._profile_save(body)
            return

        # ---- 论坛 ----
        if path == '/api/forum/posts':
            self._forum_create(body)
            return
        m = re.match(r'^/api/forum/posts/(\d+)/comments$', path)
        if m:
            self._forum_add_comment(int(m.group(1)), body)
            return

        # ---- 谜题 ----
        if path == '/api/puzzle/guess':
            self._puzzle_guess(body)
            return
        if path == '/api/puzzle/result':
            self._puzzle_result(body)
            return

        self._send(404, {"code": 1, "msg": "not found"})

    # ============================================ DELETE
    def _route_delete(self):
        q = parse_qs(urlparse(self.path).query)
        path = urlparse(self.path).path

        # ---- 旧接口：删留言（行为与 v2 完全一致） ----
        if path == '/api/comments':
            if not self._is_admin(q):
                self._send(403, {"code": 1, "msg": "无权删除"})
                return
            try:
                cid = int(q.get('id', ['0'])[0])
            except ValueError:
                cid = 0
            with _FILE_LOCK:
                c = _load_file(DATA_FILE, [])
                c = [x for x in c if x['id'] != cid]
                _save_file(DATA_FILE, c)
            self._send(200, {"code": 0, "msg": "已删除"})
            return

        # ---- 管理：删议题 ----
        m = re.match(r'^/api/forum/posts/(\d+)$', path)
        if m:
            if not self._is_admin(q):
                self._send(403, {"code": 1, "msg": "无权删除"})
                return
            pid = int(m.group(1))
            _x('UPDATE posts SET deleted=1 WHERE id=?', (pid,))
            _x('UPDATE comments SET deleted=1 WHERE post_id=?', (pid,))
            self._send(200, {"code": 0, "msg": "已删除"})
            return

        # ---- 管理：删发言 ----
        m = re.match(r'^/api/forum/comments/(\d+)$', path)
        if m:
            if not self._is_admin(q):
                self._send(403, {"code": 1, "msg": "无权删除"})
                return
            cid = int(m.group(1))
            row = _q('SELECT post_id FROM comments WHERE id=?', (cid,), one=True)
            _x('UPDATE comments SET deleted=1 WHERE id=?', (cid,))
            if row:
                _x('UPDATE posts SET comment_count=(SELECT COUNT(*) FROM comments WHERE post_id=? AND deleted=0) WHERE id=?',
                   (row[0], row[0]))
            self._send(200, {"code": 0, "msg": "已删除"})
            return

        # ---- 管理：删谜题成绩 ----
        m = re.match(r'^/api/puzzle/results/(\d+)$', path)
        if m:
            if not self._is_admin(q):
                self._send(403, {"code": 1, "msg": "无权删除"})
                return
            _x('DELETE FROM puzzle_results WHERE id=?', (int(m.group(1)),))
            self._send(200, {"code": 0, "msg": "已删除"})
            return

        self._send(404, {"code": 1, "msg": "not found"})

    # ============================================ 旧接口实现（v2 原样）
    def _post_comment(self, body):
        ip = self._client_ip()
        if not _rate_ok('c:' + ip, 15):
            self._send(429, {"code": 1, "msg": "发射太快啦，60秒内最多15条，歇口气再来"})
            return
        name = str(body.get('name', '')).strip()[:30]
        content = str(body.get('content', '')).strip()[:100000]
        me = self._me()   # UC: 登录态从签名 Cookie 解析，客户端不可伪造
        if me:
            name = me     # UC: 登录留言一律以账号身份落款（身份与 UID 绑定，不接受自定义昵称）
        if not name:
            self._send(400, {"code": 1, "msg": "至少留个名字吧"})
            return
        if not content:
            self._send(400, {"code": 1, "msg": "内容不能为空"})
            return
        if not me and _q('SELECT id FROM users WHERE name_lower=?', (name.lower(),), one=True):
            # UC: 匿名留言不得冒用已注册账号的名称（本人请登录后留言）
            self._send(400, {"code": 1, "msg": "「%s」是已注册账号的名称：如果是你本人，请先登录再留言；否则请换个名字" % name})
            return
        with _FILE_LOCK:
            c = _load_file(DATA_FILE, [])
            rec = {"id": int(time.time() * 1000), "name": name, "content": content,
                   "time": _now_str(), "ip": ip}
            if me:
                rec["acct"] = me
                row = _q('SELECT uid FROM users WHERE name_lower=?', (me.lower(),), one=True)
                if row and row[0]:
                    rec["uid"] = int(row[0])
            c.append(rec)
            _save_file(DATA_FILE, c)
        self._send(200, {"code": 0, "msg": "留言成功"})

    def _post_checkin(self, body):
        ip = self._client_ip()
        if not _rate_ok('k:' + ip, 20):
            self._send(429, {"code": 1, "msg": "打卡太快啦，稍等一会儿"})
            return
        card = str(body.get('card', '')).strip()[:40]
        name = str(body.get('name', '')).strip()[:20]
        if not card:
            self._send(400, {"code": 1, "msg": "得有个打卡口令"})
            return
        today = _today()
        # 注意：所有 _send 都放到文件锁之外，避免慢客户端把锁占住 15s 拖累读接口
        resp = None
        with _FILE_LOCK:
            data = _load_file(CHECKIN_FILE, {})
            rec = data.get(card)
            if rec is None:
                if not name:
                    resp = (400, {"code": 3, "msg": "首次打卡要留下封号，之后就能只输口令打卡了"})
                else:
                    _dup = [c for c, r in data.items() if r.get('name') == name and c != card]
                    if _dup:
                        resp = (400, {"code": 4, "msg": "这个封号名已经被占了，换一个"})
                    else:
                        rec = {"name": name, "days": 1, "last_date": today, "dates": [today],
                               "last_time": _now_str(), "ip": ip, "ip_history": [{"date": today, "ip": ip}]}
                        data[card] = rec
                        _save_file(CHECKIN_FILE, data)
                        resp = (200, {"code": 0, "msg": "口令与封号已绑定，打卡成功", "name": rec['name'],
                                      "days": rec['days'], "poem": random.choice(POEMS),
                                      "egg": get_egg(rec['days']), "bind": True})
            elif rec.get('last_date') == today:
                resp = (200, {"code": 2, "msg": "今天已经打过了，明天再来", "name": rec.get('name'),
                              "days": rec.get('days', 0), "poem": random.choice(POEMS),
                              "egg": get_egg(rec.get('days', 0))})
            else:
                rec['days'] = rec.get('days', 0) + 1
                rec['last_date'] = today
                rec['last_time'] = _now_str()
                rec['ip'] = ip
                rec.setdefault('dates', []).append(today)
                hist = rec.setdefault('ip_history', [])
                hist.append({"date": today, "ip": ip})
                del hist[:-400]
                _save_file(CHECKIN_FILE, data)
                resp = (200, {"code": 0, "msg": "打卡成功", "name": rec['name'], "days": rec['days'],
                              "poem": random.choice(POEMS), "egg": get_egg(rec['days']), "bind": False})
        if resp:
            self._send(resp[0], resp[1])

    # ============================================ 账号
    def _set_cookie(self, name, value, max_age):
        ck = 'yq_user=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Lax' % (value, max_age)
        if (self.headers.get('X-Forwarded-Proto') or '').lower() == 'https':
            ck += '; Secure'
        self._extra_cookie = ck

    def _send_ck(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'no-store')
        ck = getattr(self, '_extra_cookie', None)
        if ck:
            self.send_header('Set-Cookie', ck)
            self._extra_cookie = None
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _auth_register(self, body):
        ip = self._client_ip()
        # 12 次/小时/IP：真实场景里"朋友群体在同一校园网/NAT 后一起注册"很常见，
        # 6 次会误伤第 7 个人；12 次既留足余量，又仍然挡得住批量注册。
        if not _rate_ok('rg:' + ip, 12, 3600):
            self._send(429, {"code": 1, "msg": "这个网络注册太频繁啦，请过一小时再试（同一个 WiFi 下的朋友共用一个名额池）"})
            return
        name = str(body.get('name', '')).strip()
        pw = str(body.get('password', ''))
        # 先校验格式再消费验证码：手滑写错用户名不该浪费一次验证码
        if not _NAME_RE.match(name):
            self._send(400, {"code": 1, "msg": "用户名要 3~16 位，只能是中英文、数字、下划线"})
            return
        if len(pw) < 8 or len(pw) > 64:
            self._send(400, {"code": 1, "msg": "密码要 8~64 位"})
            return
        ok, why = _captcha_check(str(body.get('captcha_token', '')), str(body.get('captcha', '')))
        if not ok:
            self._send(400, {"code": 1, "msg": why})
            return
        if _q('SELECT id FROM users WHERE name_lower=?', (name.lower(),), one=True):
            self._send(400, {"code": 1, "msg": "这个用户名已经有人用了"})
            return
        salt, h = _hash_pw(pw)
        uid = _new_uid()   # UC: 随机 UID，与账号绑定（身份标识）
        _x('INSERT INTO users(name,name_lower,salt,hash,uid,created,last_login,ip) VALUES(?,?,?,?,?,?,?,?)',
           (name, name.lower(), salt, h, uid, _now_str(), _now_str(), ip))
        self._set_cookie('yq_user', _session_token(name), SESSION_DAYS * 86400)
        self._send_ck(200, {"code": 0, "msg": "注册成功", "name": name, "uid": uid})

    def _auth_login(self, body):
        ip = self._client_ip()
        if not _rate_ok('lg:' + ip, 10, 300):
            self._send(429, {"code": 1, "msg": "登录尝试太频繁，歇 5 分钟再来"})
            return
        name = str(body.get('name', '')).strip()
        pw = str(body.get('password', ''))
        row = _q('SELECT name,salt,hash FROM users WHERE name_lower=?', (name.lower(),), one=True)
        # 用户不存在时也跑一次假哈希：抹平 30~50ms 的计时差，避免被枚举用户名
        salt, h = (row[1], row[2]) if row else ('00' * 16, '00' * 32)
        pw_ok = _verify_pw(pw, salt, h)
        if not row or not pw_ok:
            self._send(400, {"code": 1, "msg": "用户名或密码不对"})
            return
        _x('UPDATE users SET last_login=?, ip=? WHERE name_lower=?', (_now_str(), ip, name.lower()))
        self._set_cookie('yq_user', _session_token(row[0]), SESSION_DAYS * 86400)
        self._send_ck(200, {"code": 0, "msg": "登录成功", "name": row[0]})

    def _auth_logout(self):
        self._set_cookie('yq_user', '', 0)
        self._send_ck(200, {"code": 0, "msg": "已退出"})

    # ============================================ 上传
    def _upload_json(self, body):
        name = self._me()
        ip = self._client_ip()
        if not name:
            self._send(401, {"code": 1, "msg": "先登录再上传"})
            return
        if not _rate_ok('up:' + ip, 20, 600):
            self._send(429, {"code": 1, "msg": "上传太频繁，歇一会儿"})
            return
        kind = str(body.get('kind', 'image')).lower()
        if kind == 'video':
            self._send(400, {"code": 1, "msg": "本站已取消视频投稿，发图片就好"})
            return
        if kind != 'image':
            kind = 'image'
        data_b64 = body.get('data', '')
        if not isinstance(data_b64, str) or not data_b64:
            self._send(400, {"code": 1, "msg": "没有收到文件内容"})
            return
        if len(data_b64) > (MAX_IMAGE // 3 + 1024) * 4:
            self._send(413, {"code": 1, "msg": "图片超过 2MB，请先压缩"})
            return
        try:
            raw = base64.b64decode(data_b64, validate=False)
        except Exception:
            self._send(400, {"code": 1, "msg": "文件内容不是合法 base64"})
            return
        info, err = _save_upload(kind, raw, name)
        if err:
            self._send(400, {"code": 1, "msg": err})
            return
        self._send(200, {"code": 0, "msg": "上传成功", "file": info})

    def _upload_raw(self, ctype):
        # 2026-09-12 起取消视频投稿：这个入口只用于视频，一律拒绝（先排空 body 再回，客户端才能收到提示）
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
        except ValueError:
            n = 0
        if n > 0:
            self._drain(n)
        self._send(400, {"code": 1, "msg": "本站已取消视频投稿，发图片就好"})
        return
        # ↓ 以下为历史实现，保留备查
        name = self._me()
        ip = self._client_ip()
        if not name:
            self._send(401, {"code": 1, "msg": "先登录再上传"})
            return
        if not _rate_ok('up:' + ip, 20, 600):
            self._send(429, {"code": 1, "msg": "上传太频繁，歇一会儿"})
            return
        data, too_big = self._read_raw(BIG_BODY)
        if too_big:
            self._send(413, {"code": 1, "msg": "视频超过 20MB"})
            return
        if not data:
            self._send(400, {"code": 1, "msg": "没有收到文件内容"})
            return
        info, err = _save_upload('video', data, name)
        if err:
            self._send(400, {"code": 1, "msg": err})
            return
        self._send(200, {"code": 0, "msg": "上传成功", "file": info})

    def _serve_media(self, fname):
        fname = unquote(fname)
        if not _FILE_RE.match(fname):
            self._send(404, {"code": 1, "msg": "not found"})
            return
        path = os.path.join(UPLOAD_DIR, fname)
        if not os.path.isfile(path):
            self._send(404, {"code": 1, "msg": "not found"})
            return
        # 只给视频限流：图片 ≤2MB 且磁盘直出（压测 6 并发 0 个 503），
        # 视频可能几十 MB、慢客户端会把 worker 占住，所以最多 4 路，保证还有 worker 服务 API。
        if not fname.lower().endswith((".mp4", ".webm")):
            self._send_media_file(path, fname)
            return
        if not _MEDIA_SEM.acquire(blocking=False):
            self._send(503, {"code": 1, "msg": "视频通道忙，稍后再试"})
            return
        try:
            self._send_media_file(path, fname)
        finally:
            _MEDIA_SEM.release()

    def _send_media_file(self, path, fname):
        ext = fname.rsplit('.', 1)[1]
        mime = _IMG_EXT.get(ext) or _VID_EXT.get(ext) or 'application/octet-stream'
        size = os.path.getsize(path)
        start, end = 0, size - 1
        code = 200
        rng = self.headers.get('Range') or ''
        m = re.match(r'^bytes=(\d*)-(\d*)$', rng.strip())
        if m and ext in _VID_EXT and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
            else:
                # 后缀范围 bytes=-N 表示"最后 N 字节"
                start = max(0, size - int(m.group(2)))
                end = size - 1
            if size <= 0 or start > end or start >= size:
                self.send_response(416)
                self.send_header('Content-Range', 'bytes */%d' % size)
                self.send_header('Accept-Ranges', 'bytes')
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if code == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, size))
        self.send_header('Cache-Control', 'public, max-age=31536000, immutable')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        try:
            with open(path, 'rb') as f:
                f.seek(start)
                left = length
                while left > 0:
                    chunk = f.read(min(65536, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            pass

    # ============================================ 论坛
    def _profile_save(self, body):
        name = self._me()
        if not name:
            self._send(401, {'code': 1, 'msg': '先登录再改资料'})
            return
        ip = self._client_ip()
        if not _rate_ok('pf:' + ip, 30, 600):
            self._send(429, {'code': 1, 'msg': '改得太频繁啦，歇一会儿'})
            return
        row = _q('SELECT avatar, bio FROM users WHERE name_lower=?', (name.lower(),), one=True)
        if not row:
            self._send(401, {'code': 1, 'msg': '先登录再改资料'})
            return
        cur_avatar, cur_bio = row[0] or '', row[1] or ''
        bio = body.get('bio', None)
        bio = cur_bio if bio is None else str(bio).strip()[:200]
        avatar = body.get('avatar', None)
        if avatar is None:
            new_avatar = cur_avatar                     # 未提交头像 → 保持不变
        elif avatar == '':
            new_avatar = ''                             # 显式清空
        else:
            fn = str(avatar)
            if not _FILE_RE.match(fn):
                self._send(400, {'code': 1, 'msg': '头像文件名不合法'})
                return
            up = _q('SELECT kind, owner FROM uploads WHERE file=?', (fn,), one=True)
            if not up or up[1] != name:
                self._send(403, {'code': 1, 'msg': '只能用自己的上传做头像'})
                return
            if up[0] != 'image':
                self._send(400, {'code': 1, 'msg': '头像必须是图片'})
                return
            new_avatar = fn
            _x('UPDATE uploads SET used=1 WHERE file=?', (fn,))
        _x('UPDATE users SET avatar=?, bio=? WHERE name_lower=?', (new_avatar, bio, name.lower()))
        self._send(200, {'code': 0, 'msg': '资料已更新', 'avatar': new_avatar, 'bio': bio,
                         'avatar_url': ('/api/media/' + new_avatar) if new_avatar else ''})

    def _forum_list(self, q):
        try:
            page = min(100000, max(1, int(q.get('page', ['1'])[0])))
        except ValueError:
            page = 1
        try:
            size = min(50, max(1, int(q.get('size', ['20'])[0])))
        except ValueError:
            size = 20
        total = _q('SELECT COUNT(*) FROM posts WHERE deleted=0', one=True)[0]
        rows = _q('SELECT id,title,body,author,created,comment_count FROM posts '
                  'WHERE deleted=0 ORDER BY id DESC LIMIT ? OFFSET ?', (size, (page - 1) * size))
        amap = _avatars_for([r[3] for r in rows])
        posts = []
        for r in rows:
            body = r[2] or ''
            av = amap.get(str(r[3]).lower(), '')
            posts.append({'id': r[0], 'title': r[1], 'author': r[3], 'created': r[4],
                          'comments': r[5], 'snippet': re.sub(r'\s+', ' ', re.sub(r'\[\[img:[0-9a-fA-F]{16,64}\.(?:jpg|png|gif|webp)\]\]', ' ', body)).strip()[:120],
                          'len': len(body), 'avatar': ('/api/media/' + av) if av else ''})
        self._send(200, {'code': 0, 'posts': posts, 'total': total, 'page': page, 'size': size})

    def _forum_detail(self, pid, q):
        row = _q('SELECT id,title,body,author,created,comment_count FROM posts WHERE id=? AND deleted=0',
                 (pid,), one=True)
        if not row:
            self._send(404, {'code': 1, 'msg': '议题不存在或已被删除'})
            return
        media = [{'kind': m[0], 'url': '/api/media/' + m[1], 'size': m[2], 'mime': m[3]}
                 for m in _q('SELECT kind,file,size,mime FROM post_media WHERE post_id=? ORDER BY id', (pid,))]
        au = _q('SELECT avatar, bio FROM users WHERE name_lower=?', (str(row[3]).lower(),), one=True)
        av = (au[0] if au and au[0] else '')
        self._send(200, {'code': 0, 'post': {'id': row[0], 'title': row[1], 'body': row[2],
                                            'author': row[3], 'created': row[4], 'comments': row[5],
                                            'media': media, 'avatar': ('/api/media/' + av) if av else '',
                                            'bio': (au[1] if au and au[1] else '')}})

    def _forum_comments(self, pid, q):
        try:
            page = min(100000, max(1, int(q.get('page', ['1'])[0])))
        except ValueError:
            page = 1
        try:
            size = min(100, max(1, int(q.get('size', ['30'])[0])))
        except ValueError:
            size = 30
        total = _q('SELECT COUNT(*) FROM comments WHERE post_id=? AND deleted=0', (pid,), one=True)[0]
        rows = _q('SELECT id,body,author,created FROM comments WHERE post_id=? AND deleted=0 '
                  'ORDER BY id ASC LIMIT ? OFFSET ?', (pid, size, (page - 1) * size))
        amap2 = _avatars_for([r[2] for r in rows])
        items = []
        for r in rows:
            av = amap2.get(str(r[2]).lower(), '')
            items.append({'id': r[0], 'body': r[1], 'author': r[2], 'created': r[3],
                          'avatar': ('/api/media/' + av) if av else ''})
        self._send(200, {'code': 0, 'comments': items, 'total': total, 'page': page, 'size': size})

    def _forum_create(self, body):
        name = self._me()
        ip = self._client_ip()
        if not name:
            self._send(401, {'code': 1, 'msg': '发议题要先登录'})
            return
        if not _rate_ok('fp:' + ip, 6, 600):
            self._send(429, {'code': 1, 'msg': '发得太快啦，歇 10 分钟'})
            return
        title = str(body.get('title', '')).strip()
        text = str(body.get('body', '')).strip()
        media = body.get('media', [])
        if len(title) < 2 or len(title) > LIMIT_TITLE:
            self._send(400, {'code': 1, 'msg': '标题要 2~%d 个字' % LIMIT_TITLE})
            return
        if len(text) < 1 or len(text) > LIMIT_BODY:
            self._send(400, {'code': 1, 'msg': '正文要 1~%d 个字' % LIMIT_BODY})
            return
        if not isinstance(media, list):
            media = []
        media = media[:LIMIT_POST_IMAGES + 2]
        ok_media = []
        img_n = 0
        for it in media:
            if not isinstance(it, dict):
                continue
            fn = str(it.get('file', ''))
            if not _FILE_RE.match(fn):
                continue
            row = _q('SELECT kind,size,mime,owner,used FROM uploads WHERE file=?', (fn,), one=True)
            if not row or row[3] != name or row[4]:
                continue
            kind = row[0]
            if kind != 'image':          # 已取消视频投稿：老视频文件也不再接受挂载
                continue
            if img_n >= LIMIT_POST_IMAGES:
                continue
            # 原子占用：并发发帖时只有一个请求能拿到这个文件（避免 TOCTOU 复用）
            if _run('UPDATE uploads SET used=1 WHERE file=? AND owner=? AND used=0', (fn, name)) != 1:
                continue
            img_n += 1
            ok_media.append((fn, kind, row[1], row[2]))
        pid = _x('INSERT INTO posts(title,body,author,created,ip,deleted,comment_count) VALUES(?,?,?,?,?,0,0)',
                 (title, text, name, _now_str(), ip))
        for fn, kind, size, mime in ok_media:
            _x('INSERT INTO post_media(post_id,kind,file,size,mime) VALUES(?,?,?,?,?)', (pid, kind, fn, size, mime))
        self._send(200, {'code': 0, 'msg': '议题已发布', 'id': pid})

    def _forum_add_comment(self, pid, body):
        name = self._me()
        ip = self._client_ip()
        if not name:
            self._send(401, {'code': 1, 'msg': '发言要先登录'})
            return
        if not _rate_ok('fc:' + ip, 20, 600):
            self._send(429, {'code': 1, 'msg': '发言太快啦，歇 10 分钟'})
            return
        text = str(body.get('body', '')).strip()
        if not text:
            self._send(400, {'code': 1, 'msg': '发言不能为空'})
            return
        if len(text) > LIMIT_COMMENT:
            self._send(400, {'code': 1, 'msg': '发言最多 %d 字' % LIMIT_COMMENT})
            return
        row = _q('SELECT id FROM posts WHERE id=? AND deleted=0', (pid,), one=True)
        if not row:
            self._send(404, {'code': 1, 'msg': '议题不存在'})
            return
        _x('INSERT INTO comments(post_id,body,author,created,ip,deleted) VALUES(?,?,?,?,?,0)',
           (pid, text, name, _now_str(), ip))
        _x('UPDATE posts SET comment_count=(SELECT COUNT(*) FROM comments WHERE post_id=? AND deleted=0) WHERE id=?',
           (pid, pid))
        self._send(200, {'code': 0, 'msg': '发言成功'})

    def _forum_user(self, name, q):
        name = str(name).strip()[:16]
        row = _q('SELECT name,created,uid FROM users WHERE name_lower=?', (name.lower(),), one=True)
        if not row:
            self._send(404, {'code': 1, 'msg': '查无此代表'})
            return
        try:
            page = min(100000, max(1, int(q.get('page', ['1'])[0])))
        except ValueError:
            page = 1
        try:
            size = min(50, max(1, int(q.get('size', ['10'])[0])))
        except ValueError:
            size = 10
        pc = _q('SELECT COUNT(*) FROM posts WHERE author=? AND deleted=0', (row[0],), one=True)[0]
        cc = _q('SELECT COUNT(*) FROM comments WHERE author=? AND deleted=0', (row[0],), one=True)[0]
        prows = _q('SELECT id,title,created,comment_count FROM posts WHERE author=? AND deleted=0 '
                   'ORDER BY id DESC LIMIT ? OFFSET ?', (row[0], size, (page - 1) * size))
        crows = _q('SELECT c.id,c.post_id,c.body,c.created,p.title FROM comments c '
                   'LEFT JOIN posts p ON p.id=c.post_id WHERE c.author=? AND c.deleted=0 '
                   'ORDER BY c.id DESC LIMIT ?', (row[0], size))
        posts = [{'id': r[0], 'title': r[1], 'created': r[2], 'comments': r[3]} for r in prows]
        comments = [{'id': r[0], 'post_id': r[1], 'body': r[2], 'created': r[3], 'title': r[4]} for r in crows]
        pu = _q('SELECT avatar, bio FROM users WHERE name_lower=?', (row[0].lower(),), one=True)
        av = (pu[0] if pu and pu[0] else '')
        self._send(200, {'code': 0, 'name': row[0], 'created': row[1], 'uid': int(row[2] or 0),
                         'post_count': pc, 'comment_count': cc, 'posts': posts, 'comments': comments,
                         'page': page, 'size': size,
                         'avatar': ('/api/media/' + av) if av else '',
                         'bio': (pu[1] if pu and pu[1] else '')})

    # ============================================ 谜题
    def _puzzle_today(self, q):
        day = q.get('d', [''])[0] or _today()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
            self._send(400, {'code': 1, 'msg': '日期格式不对'})
            return
        bank = _load_bank()
        if not bank:
            self._send(503, {'code': 1, 'msg': '题库还没就绪'})
            return
        qs = _pick_daily(bank, day)
        if not qs:
            self._send(503, {'code': 1, 'msg': '题库是空的'})
            return
        out = []
        for it in qs:
            o = {'id': it.get('id'), 'type': it.get('type'), 'cat': it.get('cat'),
                 'answer': it.get('answer'), 'aliases': it.get('aliases', []),
                 'note': it.get('note', ''), 'hint': it.get('hint', '')}
            if it.get('type') == 'attr':
                pool = _load_pool(it.get('cat'))
                if pool:
                    o['pool'] = it.get('cat')
                    o['fields'] = pool.get('fields')
                    o['entities'] = pool.get('entities')
            else:
                o['image'] = it.get('image', '')
                o['text'] = it.get('text', '')
            out.append(o)
        players = _q('SELECT COUNT(DISTINCT user) FROM puzzle_results WHERE day=?', (day,), one=True)[0]
        # UC: 已登录时返回本人当日成绩（前端用于跨设备防重玩 / 恢复「已交卷」状态）
        mine = []
        try:
            me = self._me()
            if me:
                for r in _q('SELECT qid, wrong, solved, score FROM puzzle_results WHERE day=? AND user=? ORDER BY id',
                            (day, me)):
                    mine.append({'qid': r[0], 'wrong': int(r[1] or 0),
                                 'solved': int(r[2] or 0), 'score': int(r[3] or 0)})
        except Exception:
            mine = []
        self._send(200, {'code': 0, 'day': day, 'questions': out, 'players': players,
                         'mine': mine, 'version': bank.get('version', 0)})

    def _puzzle_pool_entry(self, cat, answer):
        pool = _load_pool(cat)
        if not pool:
            return None, None
        for e in pool.get('entities', []):
            if e.get('name') == answer:
                return pool, e
        return pool, None

    def _puzzle_guess(self, body):
        ip = self._client_ip()
        if not _rate_ok('pz:' + ip, 120, 60):
            self._send(429, {'code': 1, 'msg': '猜得太快啦，歇口气'})
            return
        day = str(body.get('day', ''))[:10]
        qid = str(body.get('qid', ''))[:40]
        guess = str(body.get('guess', ''))[:40]
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', day) or not qid or not guess.strip():
            self._send(400, {'code': 1, 'msg': '参数不完整'})
            return
        bank = _load_bank()
        if not bank:
            self._send(503, {'code': 1, 'msg': '题库还没就绪'})
            return
        q = None
        for it in bank['questions']:
            if it.get('id') == qid:
                q = it
                break
        if not q:
            self._send(404, {'code': 1, 'msg': '没有这道题'})
            return
        me = self._me()
        pool = ent = None
        if q.get('type') == 'attr':
            pool, ent = self._puzzle_pool_entry(q.get('cat'), q.get('answer'))
            ge = _resolve_entity(guess, pool) if pool else None
            if not ge:
                # 不在候选名单不算一次猜测，也不计分
                self._send(200, {'code': 0, 'correct': False, 'known': False,
                                 'msg': '不在候选名单里，换一个试试'})
                return
            correct = _norm(ge.get('name')) == _norm(q.get('answer'))
            row = []
            if pool:
                ae = None
                for e in pool.get('entities', []):
                    if e.get('name') == q.get('answer'):
                        ae = e
                        break
                if ae:
                    for i, f in enumerate(pool.get('fields', [])):
                        r = _compare(f, ge['v'][i] if i < len(ge.get('v', [])) else '',
                                     ae['v'][i] if i < len(ae.get('v', [])) else '')
                        r['k'] = f.get('k')
                        row.append(r)
            if me:
                slot = _guess_slot(me, day, qid)
                slot['seen'] = True
                if correct:
                    slot['ok'] = True
                else:
                    slot['w'] += 1
            self._send(200, {'code': 0, 'correct': correct, 'known': True,
                             'entity': ge.get('name'), 'row': row})
            return
        correct = _answer_ok(guess, q, None)
        if me:
            slot = _guess_slot(me, day, qid)
            slot['seen'] = True
            if correct:
                slot['ok'] = True
            else:
                slot['w'] += 1
        self._send(200, {'code': 0, 'correct': correct, 'known': True})

    def _puzzle_result(self, body):
        ip = self._client_ip()
        if not _rate_ok('pz:' + ip, 120, 60):
            self._send(429, {'code': 1, 'msg': '提交太快啦'})
            return
        day = str(body.get('day', ''))[:10]
        qid = str(body.get('qid', ''))[:40]
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', day) or not qid:
            self._send(400, {'code': 1, 'msg': '参数不完整'})
            return
        name = self._me()
        if not name:
            # 未登录不上榜；前端本来就不会调，这里是服务端兜底
            self._send(401, {'code': 1, 'msg': '登录后才计入榜单'})
            return
        if day != _today():
            # 需求：往期归档可玩，但不计入当日榜
            self._send(400, {'code': 1, 'msg': '往期题目不计入榜单'})
            return
        bank = _load_bank()
        if not bank:
            self._send(503, {'code': 1, 'msg': '题库还没就绪'})
            return
        today_ids = set()
        for x in _pick_daily(bank, day):
            today_ids.add(x.get('id'))
        if qid not in today_ids:
            self._send(400, {'code': 1, 'msg': '这不是今天的题目'})
            return
        q = None
        for it in bank['questions']:
            if it.get('id') == qid:
                q = it
                break
        if not q:
            self._send(404, {'code': 1, 'msg': '没有这道题'})
            return
        # UC: 今日已交卷的题不可重玩（防跨设备重刷）——直接返回已有成绩，不覆盖
        row = _q('SELECT wrong, solved, score FROM puzzle_results WHERE day=? AND qid=? AND user=?',
                 (day, qid, name), one=True)
        if row:
            self._send(200, {'code': 0, 'already': True, 'score': int(row[2] or 0),
                             'wrong': int(row[0] or 0), 'solved': int(row[1] or 0),
                             'msg': '今日已交过卷，成绩保持不变'})
            return
        # 分数以服务端记账为准：客户端上报的 wrong/solved 一律不采信（防刷分）
        slot = _guess_slot(name, day, qid)
        if not slot['seen']:
            self._send(400, {'code': 1, 'msg': '成绩要先经过服务端判定（重新猜一次即可）'})
            return
        wrong = slot['w']
        solved = 1 if slot['ok'] else 0
        # 连击：昨天起往前连续有成绩的天数，再算上今天这一题（与前端 streak() 口径一致）
        streak = 0
        try:
            di = _day_index(day)
            for i in range(1, 61):
                d = _date_from_index(di - i)
                r = _q('SELECT COUNT(*) FROM puzzle_results WHERE user=? AND day=? AND solved=1',
                       (name, d), one=True)
                if r and r[0]:
                    streak += 1
                else:
                    break
        except Exception:
            streak = 0
        effective = streak + 1 if solved else streak       # 连续天数（含当天），用于展示与分享
        # 计分传入「计入加成的天数」= 连续天数 - 1：
        # 第 1 天猜中就是 100 分，第 2 天起每天 +10%，第 11 天起封顶 ×2.0
        score, base = _puzzle_score(wrong, (effective - 1) if solved else 0)
        if not solved:
            score = 0
        token = _sign({'k': 'pr', 'd': day, 'q': qid, 'w': wrong, 's': solved,
                       'u': name, 'e': int(time.time()) + 86400})
        _x('INSERT INTO puzzle_results(day,qid,user,cat,type,wrong,solved,score,created) '
           'VALUES(?,?,?,?,?,?,?,?,?) '
           'ON CONFLICT(day,qid,user) DO UPDATE SET wrong=excluded.wrong, solved=excluded.solved, '
           'score=excluded.score, created=excluded.created',
           (day, qid, name, q.get('cat'), q.get('type'), wrong, solved, score, _now_str()))
        self._send(200, {'code': 0, 'score': score, 'base': base, 'wrong': wrong, 'solved': solved,
                         'streak': effective, 'saved': True, 'token': token})

    def _puzzle_leaderboard(self, q):
        typ = q.get('type', ['daily'])[0]
        day = q.get('day', [''])[0] or _today()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
            day = _today()
        if typ == 'daily':
            rows = _q('SELECT user, SUM(score) s, COUNT(*) n FROM puzzle_results WHERE day=? AND solved=1 '
                      'GROUP BY user ORDER BY s DESC, n DESC LIMIT 50', (day,))
            data = [{'name': r[0], 'score': r[1], 'solved': r[2]} for r in rows]
        elif typ == 'total':
            rows = _q('SELECT user, SUM(score) s, COUNT(*) n, COUNT(DISTINCT day) d FROM puzzle_results '
                      'WHERE solved=1 GROUP BY user ORDER BY s DESC LIMIT 50')
            data = [{'name': r[0], 'score': r[1], 'solved': r[2], 'days': r[3]} for r in rows]
        elif typ == 'streak':
            rows = _q('SELECT user, day, COUNT(*) n FROM puzzle_results WHERE solved=1 '
                      'GROUP BY user, day ORDER BY user, day DESC')
            best = {}
            cur = {}
            for r in rows:
                u = r[0]
                d = r[1]
                n = r[2]
                if n <= 0:
                    continue
                di = _day_index(d)
                if u in cur and cur[u][1] == di + 1:
                    cur[u] = (cur[u][0] + 1, di)
                else:
                    cur[u] = (1, di)
                if cur[u][0] > best.get(u, 0):
                    best[u] = cur[u][0]
            data = [{'name': k, 'streak': v} for k, v in best.items()]
            data.sort(key=lambda x: -x['streak'])
            data = data[:50]
        elif typ == 'cat':
            cat = q.get('cat', ['anime'])[0]
            rows = _q('SELECT user, SUM(score) s, COUNT(*) n FROM puzzle_results WHERE cat=? AND solved=1 '
                      'GROUP BY user ORDER BY s DESC LIMIT 50', (cat,))
            data = [{'name': r[0], 'score': r[1], 'solved': r[2]} for r in rows]
        else:
            self._send(400, {'code': 1, 'msg': '榜单类型不对'})
            return
        self._send(200, {'code': 0, 'type': typ, 'day': day, 'list': data})

    def _puzzle_stats(self, q):
        day = q.get('day', [''])[0] or _today()
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', day):
            day = _today()
        try:
            row = _q('SELECT COUNT(DISTINCT user), SUM(solved) FROM puzzle_results WHERE day=?', (day,), one=True)
            players = row[0] if row and row[0] else 0
            solved = row[1] if row and row[1] else 0
        except Exception:
            players, solved = 0, 0
        self._send(200, {'code': 0, 'day': day, 'players': players, 'solved': solved})

    def _puzzle_archive(self, q):
        bank = _load_bank()
        if not bank:
            self._send(503, {'code': 1, 'msg': '题库还没就绪'})
            return
        try:
            days = min(120, max(1, int(q.get('days', ['30'])[0])))
        except ValueError:
            days = 30
        today = _today()
        di = _day_index(today)
        out = []
        for i in range(0, days):
            d = _date_from_index(di - i)
            qs = _pick_daily(bank, d)
            row = _q('SELECT COUNT(DISTINCT user) FROM puzzle_results WHERE day=?', (d,), one=True)
            out.append({'day': d, 'players': (row[0] if row else 0),
                        'titles': [x.get('type') for x in qs]})
        self._send(200, {'code': 0, 'days': out, 'today': today})


# ================================================================ 服务器
class BoundedServer(ThreadingHTTPServer):
    """有界线程池。压测实测：8 worker + 16 排队时，40 并发会有 16 个连接被硬关，
    客户端只看到 connection reset（前端只能显示"网络异常"）。
    现在：排队深度放宽到 worker+48，且真的排满时回一个 JSON 503 + Retry-After，
    让前端能优雅提示"服务器繁忙"，而不是断连。"""
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
    QUEUE_SLACK = 48

    def __init__(self, addr, handler, max_workers=POOL_WORKERS):
        ThreadingHTTPServer.__init__(self, addr, handler)
        self._pool = ThreadPoolExecutor(max_workers=max_workers)
        self._sem = threading.BoundedSemaphore(max_workers + self.QUEUE_SLACK)

    def _busy(self, request):
        body = b'{"code":1,"msg":"\xe6\x9c\x8d\xe5\x8a\xa1\xe5\x99\xa8\xe7\xb9\x81\xe5\xbf\x99\xef\xbc\x8c\xe8\xaf\xb7\xe7\xa8\x8d\xe5\x90\x8e\xe9\x87\x8d\xe8\xaf\x95"}'
        try:
            request.settimeout(2)
            request.sendall(b'HTTP/1.0 503 Service Unavailable\r\n'
                            b'Content-Type: application/json; charset=utf-8\r\n'
                            b'Retry-After: 1\r\n'
                            b'Connection: close\r\n'
                            b'Content-Length: ' + str(len(body)).encode('ascii') + b'\r\n\r\n' + body)
        except Exception:
            pass

    def process_request(self, request, client_address):
        if not self._sem.acquire(blocking=False):
            self._busy(request)
            try:
                request.close()
            except Exception:
                pass
            return
        try:
            self._pool.submit(self._run, request, client_address)
        except Exception:
            self._sem.release()
            self._busy(request)
            try:
                request.close()
            except Exception:
                pass

    def _run(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            try:
                self.shutdown_request(request)
            except Exception:
                pass
            self._sem.release()


def _selftest():
    """无网络自检：语法/配置/加密/题库算法。容器内可执行：python comments_api.py --selftest"""
    print('yygq site_api selftest')
    print('  python', sys.version.split()[0])
    print('  DB_FILE', DB_FILE, 'UPLOAD_DIR', UPLOAD_DIR, 'BANK_DIR', BANK_DIR)
    _db_init()
    _q('SELECT COUNT(*) FROM users', one=True)
    _q('SELECT COUNT(*) FROM posts', one=True)
    _q('SELECT COUNT(*) FROM comments', one=True)
    _q('SELECT COUNT(*) FROM puzzle_results', one=True)
    print('  sqlite ok')
    salt, h = _hash_pw('test-password-123')
    assert _verify_pw('test-password-123', salt, h)
    assert not _verify_pw('wrong', salt, h)
    print('  scrypt ok')
    tok = _session_token('测试代表')
    assert _unsign(tok) is not None
    assert _unsign(tok + 'x') is None
    print('  hmac session ok')
    a = _captcha_new()
    print('  captcha ok:', a[0])
    # 计分口径：必须与前端 Math.round 一致（55*1.5=82.5 → 83，不是银行家舍入的 82）
    assert _puzzle_score(3, 5)[0] == 83, _puzzle_score(3, 5)
    assert _puzzle_score(0, 0)[0] == 100
    assert _puzzle_score(4, 0)[0] == 40
    assert _puzzle_score(0, 10)[0] == 200
    print('  puzzle score ok (100 / -15 / floor40 / combo +10% cap +100%)')
    g = _guess_slot('测试代表', '2026-09-12', 'anime-attr-01')
    g['seen'] = True
    g['w'] = 2
    g['ok'] = True
    assert _guess_slot('测试代表', '2026-09-12', 'anime-attr-01')['w'] == 2
    print('  guess slot ok')
    cols = {r[1] for r in _q('PRAGMA table_info(users)')}
    assert 'avatar' in cols and 'bio' in cols and 'uid' in cols, cols
    print('  profile columns ok')
    print('  forum limits: title<=%d body<=%d comment<=%d images<=%d'
          % (LIMIT_TITLE, LIMIT_BODY, LIMIT_COMMENT, LIMIT_POST_IMAGES))
    try:
        _gc_uploads()
        print('  upload gc ok')
    except Exception as e:
        print('  upload gc skipped:', e)
    bank = _load_bank()
    if bank:
        qs = _pick_daily(bank, '2026-09-12')
        print('  bank ok:', len(bank['questions']), 'questions; today pick =',
              [x.get('id') for x in qs])
    else:
        print('  bank NOT FOUND at', BANK_DIR, '(谜题接口会返回 503)')
    print('SELFTEST OK')


def main():
    try:
        _db_init()
    except Exception:
        # SQLite 打不开也必须继续服务：旧接口（留言板/打卡）不依赖 DB，
        # 绝不能因为新功能把老功能一起拖死。
        traceback.print_exc()
        print('WARN: sqlite 初始化失败，新接口将返回 500，旧接口照常', flush=True)
    try:
        _gc_uploads()
    except Exception:
        pass
    try:
        t = threading.Thread(target=_gc_loop, name='upload-gc', daemon=True)
        t.start()
    except Exception:
        pass
    bank = _load_bank()
    n = len(bank['questions']) if bank else 0
    print('yygq site_api v3 on %d | db=%s | bank=%d questions | workers=%d'
          % (PORT, DB_FILE, n, POOL_WORKERS), flush=True)
    BoundedServer(('0.0.0.0', PORT), Handler).serve_forever()


if __name__ == '__main__':
    if '--selftest' in sys.argv:
        _selftest()
    else:
        main()

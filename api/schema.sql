-- yygq面馆 · SQLite schema（v3）
-- 容器内路径 /data/site.db（宿主 /opt/yygq-guestbook/data/site.db）
-- 初始化：sqlite3 site.db < schema.sql   （或由 site_api.py 启动时自动执行，二者等价）
-- 说明：site_api.py 内嵌了同一份 DDL 作为兜底；此文件为交付/手工初始化用，语句全部幂等。

PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- 统一账号（一次注册，论坛 + 谜题榜单通用）
CREATE TABLE IF NOT EXISTS users (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT NOT NULL UNIQUE,              -- 展示名（原文，3~16 位）
  name_lower TEXT NOT NULL UNIQUE,              -- 唯一性判定用（大小写不敏感）
  salt       TEXT NOT NULL,                     -- 每用户独立盐（hex）
  hash       TEXT NOT NULL,                     -- hashlib.scrypt(n=2^14,r=8,p=1,dklen=32) hex
  created    TEXT NOT NULL,
  last_login TEXT,
  ip         TEXT,
  avatar     TEXT,                              -- 头像文件名（/data/uploads 内，160×160 小图）
  bio        TEXT,                              -- 个人简介 ≤200 字
  uid        INTEGER                            -- 随机 UID（8 位，全局唯一；身份与显示名解耦）
);

-- 议题（论坛帖子）
CREATE TABLE IF NOT EXISTS posts (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT NOT NULL,                  -- ≤80 字
  body          TEXT NOT NULL,                  -- ≤50000 字
  author        TEXT NOT NULL,                  -- users.name
  created       TEXT NOT NULL,
  ip            TEXT,
  deleted       INTEGER NOT NULL DEFAULT 0,     -- 软删（管理员）
  comment_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_posts_alive  ON posts(deleted, id DESC);
CREATE INDEX IF NOT EXISTS idx_posts_author ON posts(author, id DESC);

-- 发言（论坛评论）
CREATE TABLE IF NOT EXISTS comments (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id  INTEGER NOT NULL,
  body     TEXT NOT NULL,                       -- ≤5000 字
  author   TEXT NOT NULL,
  created  TEXT NOT NULL,
  ip       TEXT,
  deleted  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_comments_post   ON comments(post_id, deleted, id);
CREATE INDEX IF NOT EXISTS idx_comments_author ON comments(author, id DESC);

-- 议题媒体（图片 ≤9 张；2026-09-12 起取消视频）
CREATE TABLE IF NOT EXISTS post_media (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  post_id INTEGER NOT NULL,
  kind    TEXT NOT NULL,                        -- image | video
  file    TEXT NOT NULL,                        -- uploads.file（随机文件名）
  size    INTEGER NOT NULL DEFAULT 0,
  mime    TEXT
);
CREATE INDEX IF NOT EXISTS idx_media_post ON post_media(post_id);

-- 上传登记（防越权引用：只能引用自己刚传且未被使用的文件）
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

-- 谜题成绩（每人每天每题一条；未登录不写入）
CREATE TABLE IF NOT EXISTS puzzle_results (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  day     TEXT NOT NULL,                        -- 北京时间 YYYY-MM-DD
  qid     TEXT NOT NULL,                        -- 题目 id
  user    TEXT NOT NULL,
  cat     TEXT,                                 -- anime|game|esports|sports
  type    TEXT,                                 -- attr|image|draw|riddle
  wrong   INTEGER NOT NULL DEFAULT 0,
  solved  INTEGER NOT NULL DEFAULT 0,
  score   INTEGER NOT NULL DEFAULT 0,
  created TEXT NOT NULL,
  UNIQUE(day, qid, user)
);
CREATE INDEX IF NOT EXISTS idx_pr_day   ON puzzle_results(day, score DESC);
CREATE INDEX IF NOT EXISTS idx_pr_user  ON puzzle_results(user, day DESC);
CREATE INDEX IF NOT EXISTS idx_pr_total ON puzzle_results(solved, score DESC);

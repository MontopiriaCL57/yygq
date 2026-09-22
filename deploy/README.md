# 部署参考

三个部件彼此独立：静态前端 + Python 后端 + Node 工具。

## 端口约定
| 部件 | 默认端口 | 说明 |
| --- | --- | --- |
| `api/comments_api.py` | 8808 | 站点把 `/api` 反代到它 |
| `cs2bp/server.js` | 4900 | 建议反代到 `/cs2bp/`（含 WebSocket） |

## 启动示例（Docker）

### 后端
```bash
docker run -d --name yygq-api --restart unless-stopped \
  -p 127.0.0.1:8808:8808 \
  -v "$PWD/api":/app:ro \
  -v "$PWD/data":/data \
  -e PORT=8808 \
  -e SECRET='换成一串足够长的随机字符串' \
  python:3-alpine \
  python /app/comments_api.py
```
> `SECRET` 用于会话签名。不设置时会自动在 `/data/.site_secret` 生成随机值（重启不掉线）。

### CS2 BP 工具
```bash
docker run -d --name yygq-cs2bp --restart unless-stopped \
  -p 127.0.0.1:4900:4900 \
  -v "$PWD/cs2bp":/app \
  -e PORT=4900 \
  -e NODE_OPTIONS=--max-old-space-size=160 \
  node:20-alpine \
  sh -c "cd /app && npm install --omit=dev && node server.js"
```

### 前端
任意静态服务器托管 `site/` 目录即可。Nginx 参考同目录的 `nginx.conf.example`（含干净 URL、`/api` 与 `/cs2bp/` 反代）。

## 数据目录（不入库）
- `data/comments.json`、`data/checkin.json`：留言 / 打卡
- `data/site.db`：账号、论坛（SQLite；结构见 `api/schema.sql`）
- `data/uploads/`：上传文件
- `data/puzzle/`、`data/poems.json`：题库与诗库（运营内容）

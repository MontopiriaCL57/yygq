# yygq 面馆 · 自用网站

自用网站与配套工具。「面馆」主站提供留言板、每日打卡、论坛与每日谜题；`cs2bp` 是内战时用的 CS2 地图 BP / 选人工具。

## 目录

| 目录 | 内容 | 技术栈 |
| --- | --- | --- |
| `site/` | 主站前端（纯静态） | 原生 HTML / CSS / JS |
| `api/` | 主站后端（单文件） | Python 3 标准库 + SQLite（WAL） |
| `cs2bp/` | CS2 地图 BP 工具 | Node.js + Express + Socket.IO |
| `deploy/` | 部署参考（Nginx 反代示例、启动命令） | — |

## 功能一览（主站）
- 留言板 & 每日打卡
- 论坛：注册 / 发帖 / 评论 / 图片上传
- 每日谜题：电竞 / 游戏 / 体育 / 动漫 题库，计分与榜单

## 快速开始
- 主站后端：`python api/comments_api.py`（首次运行会自动初始化 `data/`）
- 主站前端：任意静态服务器托管 `site/`，把 `/api` 反代到后端
- CS2 BP 工具：`cd cs2bp && npm install && npm start`，浏览器打开 `http://localhost:4900`

详细部署说明见 `deploy/README.md`。

## 数据说明
本仓库**只包含程序本体**。一切运行期数据（数据库、留言、打卡、上传文件、常用名单、彩蛋图片等）均不入库，程序会在运行时于 `data/` 目录自动创建与管理。

## 说明
- `cs2bp/` 基于 [Andycommander66/cs2-map-bp-tool](https://github.com/Andycommander66/cs2-map-bp-tool)（MIT 许可）的本地定制版
- 站点字体使用 [Zpix 最像素](https://github.com/SolidZORO/zpix-pixel-font)，遵循其原始许可

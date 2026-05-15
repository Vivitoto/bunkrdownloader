# BunkrDownloader Docker

[BunkrDownloader](https://github.com/Lysagxra/BunkrDownloader) 容器化部署，带 WebUI 管理界面。支持 Bunkr 专辑和单文件批量下载。

## 功能

- 📋 WebUI 管理下载 URL
- ⚡ 实时进度：文件名、百分比、进度条、完成/失败/跳过计数
- 📟 事件日志：原项目所有日志输出实时展示
- ⚙️ 在线配置：代理、并发数、重试次数、忽略/包含列表、下载子目录
- 📁 已下载文件浏览
- 📜 session.log 查看
- 🔄 一次部署，WebUI 改配置即生效，无需重启

## 快速开始

```bash
cd Docker/BunkrDownloader
docker compose up -d
```

浏览器打开 `http://<局域网IP>:8877`

## 命令

```bash
# 启动
docker compose up -d

# 查看日志
docker logs -f bunkrdownloader

# 停止
docker compose down

# 重建（源码更新后）
docker compose build --no-cache && docker compose up -d
```

## 目录结构

```
BunkrDownloader/
├── Dockerfile
├── docker-compose.yml
├── requirements.txt          # Python 依赖
├── config.default.json       # 首次启动默认设置
├── main.py                   # BunkrDownloader 批量入口
├── downloader.py             # BunkrDownloader 单文件入口
├── src/                      # BunkrDownloader 源码
│   ├── config.py
│   ├── crawlers/             # 页面爬取
│   ├── downloaders/          # 下载逻辑
│   ├── managers/             # 进度/日志/统计
│   └── ...
├── webui/
│   ├── app.py                # Flask 后端 API
│   └── templates/
│       └── index.html        # 前端页面
├── scripts/
│   └── entrypoint.sh         # 容器入口
├── downloads/                # (挂载) 下载文件
├── logs/                     # (挂载) session.log
└── config/                   # (挂载) 持久化设置 + URL 列表
```

## WebUI 配置项

| 配置 | 说明 | 默认值 |
|------|------|--------|
| 并发数 | 同时下载文件数 | 3 |
| 最大重试次数 | 单文件失败重试 | 5 |
| 下载子目录 | `/data/downloads` 下的子文件夹 | 根目录 |
| 忽略列表 | 文件名含关键词则跳过 | 空 |
| 包含列表 | 仅下载含关键词的文件 | 空 |
| HTTP 代理 | 请求 Bunkr 时使用的代理 | 空 |
| HTTPS 代理 | 同上 | 空 |

所有配置修改即时生效，无需重启容器。

## 反向代理示例

### Caddy

```text
bunkr.yourdomain.com {
    reverse_proxy localhost:8877
}
```

### Nginx

```nginx
server {
    listen 80;
    server_name bunkr.yourdomain.com;
    location / {
        proxy_pass http://127.0.0.1:8877;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

## 技术栈

- **容器**：python:3.12-slim (~140MB)
- **Web**：Flask + 原生 JS，无外部前端框架
- **下载引擎**：BunkrDownloader (asyncio + requests)
- **运行用户**：bun（非 root）

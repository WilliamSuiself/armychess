# 斗地主对战台 — 阿里云部署指南

跟 [`army-chess/DEPLOY.md`](../army-chess/DEPLOY.md) 是同一套模式：2C2G ECS，Jev
走云端 API，服务器零本地推理即可跑。**这份指南默认关闭 Laya**
（`LAYA_ENABLED=0`）——本地模型要装 torch（~1-2GB）且首次加载要 15-20 秒，2C2G
机器上不建议开；等 Laya 那边效果调好了再单独评估要不要在云上启用。

## 0. 和 army-chess 共用一台服务器？

如果这台 ECS 已经在跑 army-chess（占用 8000 端口 + nginx 80/443），斗地主要用
**不同的端口**（下面用 `8002`）和**不同的 systemd 服务名**，nginx 里再加一个
`server` 块按子域名分流（见第 7 步）。两个 Flask 进程互不影响，各自的对局状态在
各自进程内存里。

## 1. 服务器准备（如果 army-chess 已经装过，跳过）

```bash
# Alibaba Cloud Linux / CentOS
sudo yum install -y python3 python3-pip git
# 或 Ubuntu/Debian:
# sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

## 2. 拉代码 + 装依赖

这个项目还没建 git 仓库，最简单的方式是直接把 `doudizhu` 目录传到服务器：

```bash
# 在本机执行（会把整个目录传过去，~70KB，很快）
scp -r /Users/suiyunhai/coding/Jev/doudizhu root@<服务器IP>:/root/doudizhu
```

```bash
# 在服务器上执行
cd /root/doudizhu
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. 配置

```bash
cat > .env <<'EOF'
LAYA_ENABLED=0
EOF
```

Jev 的 API key 已经硬编码在 `jev_client.py` 里，云上不用额外配置就能用；如果想换
key，`.env` 里加一行 `KNOX_API_KEY=...` 会覆盖硬编码的默认值。

| 环境变量 | 说明 |
|---------|------|
| `LAYA_ENABLED` | `0` 关闭本地模型选项（2C2G 必关，且前端会隐藏 Laya 选项） |
| `KNOX_API_KEY` / `KNOX_BASE_URL` | 可选，覆盖 `jev_client.py` 里硬编码的默认值 |
| `PORT` | 只在 `python3 app.py` 直跑时生效；gunicorn 下端口由 `-b` 参数决定 |

## 4. 启动（gunicorn）

⚠️ **必须 `-w 1`**：对局状态存在进程内存里（`app.py` 里的 `MATCHES` 字典 +
每局一个后台线程），多 worker 会互相看不见对方的对局。工作负载主要是转发 Jev
的 API 请求（I/O 等待），单 worker 多线程足够：

```bash
gunicorn -w 1 --threads 16 -b 0.0.0.0:8002 app:app
```

（用 `8002`，避开 army-chess 的 `8000`；如果这台机器只跑斗地主，改回 `8000`
也可以。）

## 5. 阿里云安全组

控制台 → ECS 实例 → 安全组 → 入方向放行 `8002` 端口（或走 nginx 的 80/443，见下）。

访问 `http://<服务器公网IP>:8002` 验证。

## 6. 常驻运行（systemd）

```ini
# /etc/systemd/system/doudizhu.service
[Unit]
Description=Tetris Arena
After=network.target

[Service]
WorkingDirectory=/root/doudizhu
ExecStart=/root/doudizhu/venv/bin/gunicorn -w 1 --threads 16 -b 0.0.0.0:8002 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now doudizhu
journalctl -u doudizhu -f    # 看日志
```

## 7. nginx 反代（子域名分流，和 army-chess 共存）

如果 army-chess 已经占了 `armychess.你的域名.com` -> 8000，斗地主建议用另一个
子域名，例如 `tetris.你的域名.com` -> 8002：

```nginx
server {
    listen 80;
    server_name tetris.你的域名.com;
    location / {
        proxy_pass http://127.0.0.1:8002;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 60s;   # Jev 单步最长约 20-30s，留够余量
    }
}
```

```bash
sudo nginx -t && sudo systemctl reload nginx
```

需要 HTTPS 的话（推荐，浏览器现在对 http 站点会有各种警告）：

```bash
sudo certbot --nginx -d tetris.你的域名.com
```

## 8. 验证

```bash
curl -H "X-Game-Id: test" http://127.0.0.1:8002/api/state
# → {"id":"test","winner":null,"human_side":"right","left":{...},"right":{...},...}
```

## 已知限制

- 对局状态在内存中：进程重启 = 所有进行中对局丢失，没有持久化/复盘功能（不像
  army-chess 有 `replays/`）。
- `LAYA_ENABLED=0` 时前端会隐藏 Laya 选项，选择它会被服务器拒绝（400）。
- 访问量大时观察内存：每局占用很小（两块 20x10 棋盘 + 少量元数据），
  `MAX_MATCHES=100` + 3h TTL 自动清理不活跃对局。

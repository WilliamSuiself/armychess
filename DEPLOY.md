# 军棋对战台 — 阿里云部署指南（Jev 版）

适用于 2C2G 阿里云 ECS。Jev 走云端 API，服务器只做 Web 服务和转发，
本地零推理，2C2G 绰绰有余（Laya 在此配置下不可用，已自动隐藏选项）。

## 1. 服务器准备

```bash
# Alibaba Cloud Linux / CentOS
sudo yum install -y python3 python3-pip git
# 或 Ubuntu/Debian:
# sudo apt update && sudo apt install -y python3 python3-pip python3-venv git
```

## 2. 拉代码 + 装依赖

```bash
git clone https://github.com/WilliamSuiself/armychess.git
cd armychess
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## 3. 配置

```bash
cat > .env <<'EOF'
KNOX_API_KEY=你的_knox_api_key
LAYA_ENABLED=0
EOF
```

| 环境变量 | 说明 |
|---------|------|
| `KNOX_API_KEY` | **必填**，Jev 云端 API 密钥 |
| `LAYA_ENABLED` | `0` 关闭本地模型选项（2G 内存必关） |
| `PORT` | 服务端口，gunicorn 下不生效（见下） |

## 4. 启动（gunicorn）

⚠️ **必须 `-w 1`**：对局状态存在进程内存里，多 worker 会互相看不见对方的对局。
工作负载是纯 I/O（转发 API），单 worker 多线程足够：

```bash
gunicorn -w 1 --threads 16 -b 0.0.0.0:8000 app:app
```

## 5. 阿里云安全组

控制台 → ECS 实例 → 安全组 → 入方向放行 `8000` 端口
（或 80/443 走 nginx，见下）。

访问 `http://<服务器公网IP>:8000` 验证。

## 6. 常驻运行（systemd）

```ini
# /etc/systemd/system/armychess.service
[Unit]
Description=Army Chess
After=network.target

[Service]
WorkingDirectory=/root/armychess
ExecStart=/root/armychess/venv/bin/gunicorn -w 1 --threads 16 -b 0.0.0.0:8000 app:app
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now armychess
journalctl -u armychess -f    # 看日志
```

## 7. （可选）nginx 反代到 80 端口

```nginx
server {
    listen 80;
    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_read_timeout 120s;   # Jev 单步最长 ~30s，留够余量
    }
}
```

## 8. 验证

```bash
curl -H "X-Game-Id: test" http://127.0.0.1:8000/api/state
# → {"phase":"setup","controllers":{"player":"human","ai":"jev"},"laya_enabled":false,...}
```

## 已知限制

- 对局状态在内存中：进程重启 = 所有进行中对局丢失（复盘文件持久化在
  `replays/` 不受影响）
- `weights.json`（经验统计）和 `replays/` 写本地磁盘，记得备份或定期同步
- 访问量大时观察内存：每局几 KB，`MAX_GAMES=200` + 12h TTL 自动清理

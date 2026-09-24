# 军棋对战台 (Army Chess)

标准陆战棋/军棋，浏览器对局，AI 决策后端可选 **Jev**（云端 API）或
**Laya**（本地开源模型）。支持人类 vs AI、AI vs AI 观战、复盘回放。

## 快速开始（本地）

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
echo "KNOX_API_KEY=你的key" > .env
python3 app.py
# 打开 http://127.0.0.1:5000
```

默认：蓝方（上·先手）= 玩家，红方（下）= Jev。可在页面上随时切换任一方为
人类 / Jev / Laya。

## 生产部署

见 [`DEPLOY.md`](./DEPLOY.md)（阿里云 2C2G 参考流程：venv + gunicorn +
systemd + Caddy/nginx 反代）。

## 启用 Laya 本地模型

Laya 是开源的本地决策模型（322M 参数，非自回归），作为 Jev 的离线替代。
**默认在小内存服务器上是关闭的**（`LAYA_ENABLED=0`），因为：

| 项目 | 占用 |
|------|------|
| 模型权重（fp32） | ~1.3 GB |
| 加载后进程峰值内存 | ~1.4 GB+ |
| CPU 推理延迟 | 本机 GPU ~1.5-2s/步；纯 CPU 预估 3-8s/步 |

**内存 ≥ 4GB 的机器上想启用，按以下步骤操作：**

### 1. 安装依赖

```bash
source venv/bin/activate
pip install laya
# CPU 版 torch（没有 GPU 就选这个，体积更小）
pip install torch --index-url https://download.pytorch.org/whl/cpu
```

### 2. 配置环境变量

在 `.env` 里加：

```bash
LAYA_ENABLED=1
HF_ENDPOINT=https://hf-mirror.com   # 国内访问 Hugging Face 的镜像，海外服务器可去掉
```

### 3.（推荐）预下载模型

首次调用会自动从 Hugging Face 下载模型（约 1.3GB），提前跑一次避免用户等待：

```bash
HF_ENDPOINT=https://hf-mirror.com python3 -c "import laya; laya.load('convaiinnovations/laya-multilingual')"
```

模型缓存在 `~/.cache/huggingface/`，重启服务不会重新下载。

### 4. 重启服务

```bash
systemctl restart armychess   # 或直接重启 python3 app.py / gunicorn
```

### 5. 验证

```bash
curl -H "X-Game-Id: t" http://127.0.0.1:8000/api/state | grep laya_enabled
# 应返回 "laya_enabled":true
```

刷新页面后，红/蓝方控制器下拉框里的 Laya 选项会从灰色变为可选。

## 目录结构

| 文件 | 作用 |
|------|------|
| `app.py` | Flask 路由、对局状态管理、AI 决策调度、候选剪枝 |
| `game.py` | 棋盘规则引擎：走子、战斗、终局判定、Jev/Laya 提示词构建 |
| `jev_client.py` / `laya_client.py` | 两个决策后端的统一适配层 |
| `experience.py` | 本地经验统计（`weights.json`），不会微调 Jev/Laya 本身 |
| `match.py` | Jev vs Laya 自动对战脚本（`python3 match.py <局数>`） |
| `static/`, `templates/` | 前端（对局页 `/`，复盘页 `/replay`） |

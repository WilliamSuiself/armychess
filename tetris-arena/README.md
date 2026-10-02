# 俄罗斯方块对战台 (Tetris Arena)

竞技俄罗斯方块（消行倒垫垫块对战），左右两个棋盘各自可由 **玩家 / Jev（云端大模型）/
Laya（本地小模型）** 控制。可以人类 vs Jev，也可以纯观战 Jev vs Laya。

沿用了 `army-chess` 项目的 AI 决策协议：把当前方块的所有合法落点（旋转 + 列）当作
"选项列表"，一次性交给模型的 `/systemone` 接口（state + typed questions -> typed
answers），模型选中一个后引擎再把方块动画式地落到那个位置——不需要逐帧网络请求。

## 快速开始

```bash
cd tetris-arena
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
python3 app.py
# 打开 http://127.0.0.1:5050
```

Jev 的 API key 已经硬编码在 `jev_client.py` 里（沿用 army-chess 项目里的
`KNOX_API_KEY`），开箱即用；也可以用 `.env` 里的 `KNOX_API_KEY` / `KNOX_BASE_URL`
覆盖。

## 启用 Laya 本地模型

Laya（`convaiinnovations/laya-multilingual`，322M 参数，非自回归决策模型）作为
Jev 的本地开源替代，同一份 state+questions -> answers 协议：

```bash
pip install laya
pip install torch --index-url https://download.pytorch.org/whl/cpu   # 没有 GPU 就选这个
```

首次调用会从 Hugging Face 自动下载权重（~1.3GB），国内网络访问不了的话设置：

```bash
export HF_ENDPOINT=https://hf-mirror.com
```

如果服务器内存/算力吃紧，可以设 `LAYA_ENABLED=0` 关闭 Laya 选项（此时前端会隐藏/
拒绝选择 Laya）。

## 玩法

- 页面顶部选择左右两方分别由谁控制，点"开始新对局"。
- 玩家操作：`←`/`→` 移动，`↑` 或 `X` 顺时针旋转，`Z` 逆时针旋转，`↓` 软降，
  `空格` 硬降，`C` 暂存 (Hold)。
- 每次消行会向对方棋盘倒垫垫块：消 1 行不倒垫，消 2 行倒垫 1 行，消 3 行倒垫 2 行，
  一次消 4 行 (Tetris) 倒垫 4 行，连续消行还有连击加成。
- 任意一方的方块无法在棋盘顶部生成（被自己堆高或被对方垫块顶上来）即判负。
- AI 侧的信息面板会实时展示模型这一步的落点、意图（清行/搭建 Tetris/防守/进攻等）、
  激进度、冒险与胜率信心——跟 army-chess 里"AI 内心世界"面板是同一套字段。
- 每局都会自动记录回放（每次方块落地记一帧），点右上角"历史回放 →"或直接访问
  `/replay` 查看，支持自动播放/单步/拖动进度条，回放文件持久化在 `replays/`
  目录，进程重启也不会丢（跟 army-chess 的复盘功能是同一套思路）。

## 架构简述

- `tetris.py` —— 纯规则引擎：方块形状/旋转、碰撞、消行、垫块，不依赖 Flask/AI。
- `ai_engine.py` —— 把"当前方块的所有合法落点"变成 choice 选项，构造
  `state`/`questions`，解析模型返回的选择。
- `jev_client.py` / `laya_client.py` —— 两个后端的 `system_one(state, questions)`
  客户端，接口完全一致，可互换。
- `app.py` —— Flask 服务 + 每局一个后台线程跑主循环（人类侧走重力/锁定延迟，AI 侧
  等待决策线程算完后做动画式硬降），`/api/state` 轮询给前端。

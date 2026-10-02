# 斗地主 × Jev / Laya

三人斗地主网页游戏，与 `army-chess` / `tetris-arena` 同一套架构：

- 每个座位（0/1/2）可独立选择 **玩家 / Jev / Laya** —— 人机、人人（理论上）、
  或全 AI 观战（如 Jev vs Laya vs Jev）
- 标准规则：叫分（不叫/1/2/3 分）→ 地主拿 3 张底牌先出 → 同牌型同长度压过，
  否则不出；炸弹/王炸封顶；地主或任一农民先出完即分胜负
- AI 决策走 `state + questions -> answers`（`/systemone`）契约；
  每次出牌枚举所有合法打法让模型选一个，**2 秒超时后本地启发式兜底**（不卡死）
- 每次叫分/出牌记一帧写入 `replays/*.json`（实时落盘），`/replay` 页面可回放

## 本地运行

```bash
cd doudizhu
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
# 要用本地小模型 Laya（可选）: pip install laya torch
python3 app.py            # http://127.0.0.1:5051  (PORT env 可改)
```

`.env`（可选）：`KNOX_API_KEY`（jev_client.py 已硬编码兜底）、
`KNOX_BASE_URL`、`LAYA_ENABLED=0`（彻底关掉 Laya 选项）、`PORT`。

## 已知限制

- 对局状态在进程内存里，**重启会丢进行中的对局**（回放文件不会丢）
- 因此部署时用单 worker + 多线程（gunicorn `--workers 1 --threads 16`）
- Laya 是面向分类任务的本地小模型，打斗地主表现较弱，仅作离线替代/观战用

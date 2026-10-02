# 21点 × Jev / Laya

三座位 vs 庄家。每座可选 玩家 / Jev / Laya。

- 4 副牌混洗，剩 <52 张自动重洗
- 庄家补到 17 停（含软 17）
- 要牌 / 停牌 / 加倍；天牌 1.5 倍
- 30 轮或全场筹码清零结束，筹码最多者胜
- **AI 可见完整记牌信息**：已见牌数、Hi-Lo 流水数/真数、每种点数剩余张数

## 运行

```bash
pip install -r requirements.txt
python3 app.py          # http://127.0.0.1:5053
```

`LAYA_ENABLED=0` 可关闭 Laya。KNOX_API_KEY 环境变量可覆盖内置 key。
回放：`/replay`（每局独立 JSON，实时落盘于 `replays/`）。

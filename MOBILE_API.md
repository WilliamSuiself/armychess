# 移动端 API 契约（`/api/v1/*`）

给鸿蒙客户端（或其他原生 App）用的服务端接口。**跟网页版共用同一套游戏引擎和
JSON 结构**——`/api/v1/*` 只是给 `/api/*` 套了一层设备令牌鉴权 + 每日配额,
本身不重复任何棋规/AI 逻辑（见 `app.py` 里 `# ---------- Mobile API` 那段,
路由是直接 alias 到网页版同名视图函数的）。

## 为什么需要这层，而不是直接开放 `/api/*`

网页版 `/api/*` 是同源可信前端，可以一直保持无鉴权。但一旦分发到应用商店，
每一次 `turn_move` 都会真实调用 Jev 云端 API 花钱——必须有设备级配额兜底，
否则任何人重复调用都能刷爆账单。

## 认证流程

### 1. 注册设备（App 首次启动时调用一次，本地持久化 token）

```
POST /api/v1/register
```

响应：
```json
{"ok": true, "device_token": "d3c0e333...", "daily_quota": 300}
```

`device_token` 用 HarmonyOS 的 `@ohos.data.preferences` 存起来，永久复用，
不要每次启动都重新注册。

### 2. 之后所有请求带两个 header

```
X-Device-Token: <注册拿到的 token>
X-Game-Id: mobile:<device_token>
```

`X-Device-Token` 用于鉴权 + 配额计数；`X-Game-Id` 用于服务端按客户端隔离
对局状态（跟网页版每个浏览器 tab 生成一个随机 game-id 是同一套机制,见
`app.py` 的 `get_ctx()`）。约定用 `mobile:<token>` 前缀避免和网页 tab 的
game-id 撞车。

### 3. 每个对局还可以带（可选）

```
X-Ctl-Player: human | jev | laya      # 红方(下)由谁控制
X-Ctl-Ai:     human | jev | laya      # 蓝方(上·先手)由谁控制
```

不带则用服务端默认值（蓝方=玩家, 红方=jev）。

## 接口列表（与网页版 `/api/*` 完全同构，只是加了 `/v1` 前缀）

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/register` | 注册设备，拿 token（唯一不需要 token 的接口） |
| GET  | `/api/v1/state` | 当前对局状态（棋盘、controllers、setup 信息） |
| POST | `/api/v1/setup/start` | 开始对局 |
| POST | `/api/v1/setup/place` | 布阵：放置一个棋子 `{"pos":[r,c],"type":"司令"}` |
| POST | `/api/v1/setup/remove` | 布阵：移除一个棋子 `{"pos":[r,c]}` |
| POST | `/api/v1/setup/preset` | 布阵：套用预设阵型 `{"index":0}` |
| POST | `/api/v1/setup/save_custom` | 保存当前布局为自定义阵型 `{"name":"..."}` |
| POST | `/api/v1/moves` | 查询某棋子合法走法 `{"pos":[r,c]}` |
| POST | `/api/v1/move` | 人类走子 `{"from":[r,c],"to":[r,c]}` |
| POST | `/api/v1/turn_move` | 驱动一步 AI（**计入每日配额**） |
| POST | `/api/v1/reset` | 重开一局 |
| GET  | `/api/v1/replay` | 当前对局的全知视角回放帧 |
| GET  | `/api/v1/experience` | 本地经验统计（`weights.json` 内容） |

**所有响应字段结构**跟网页版 `/api/state`、`/api/move` 等完全一致——
客户端 UI 该怎么解析,直接照抄 `static/game.js` 里对应字段的用法即可
（`board`、`side_to_move`、`ai_move.decision`、`ai_move.event` 等）。

## 配额

- 默认每设备每天 **300 次 `turn_move`**（服务端环境变量 `MOBILE_DAILY_QUOTA` 可调）
- 超额返回 `429 {"ok": false, "error": "daily quota exceeded", "quota": 300}`
- 只有 `turn_move`（真正调用 Jev/Laya）计入配额，浏览状态、布阵、人类走子都不计费
- 配额按自然日（服务器本地时区）零点重置

## 服务端存储

`device_tokens.json`（跟 `weights.json`/`custom_formations.json` 同级，
不进 git，见 `.gitignore`）：

```json
{
  "d3c0e333...": {"created": 1758..., "quota_date": "2026-09-25", "calls_today": 12}
}
```

## 错误码

| HTTP | 含义 |
|------|------|
| 401 | 缺少或未知的 `X-Device-Token` |
| 429 | 当日配额用尽 |
| 400 | 参数错误 / 当前不是该操作合法阶段（跟网页版一致） |

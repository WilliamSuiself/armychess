# 中国跳棋（Chinese Checkers，六角星棋盘）× Jev/Laya — AI 接口设计

六角星棋盘的"跳跃链"会导致单步候选数量爆炸，本局的核心设计难点就是
**候选生成与剪枝**，而不是 state/questions 的字段设计本身（字段反而比军棋
/象棋都简单——无隐藏信息、无吃子，纯粹是"谁先把 10 颗子搬到对面"的比赛）。

## 1. 棋盘与坐标

- 经典六角星棋盘，121 个孔位：中心六边形 61 孔 + 六个角的三角区各 10 孔。
- 建议用**轴向六边形坐标**（axial `(q, r)`）或"行偏移"坐标表示每个孔，
  不要用像素坐标——轴向坐标下"6 个方向"是固定的 6 个单位向量，方便枚举
  相邻孔/跳跃落点。
- 支持 2/3/4/6 人（只用相对的 2 或 3 个角）。**MVP 建议先做 2 人**（两个
  相对角），棋局时长和候选规模都最可控；3/4/6 人作为二期扩展，只是
  "玩家数量和出场顺序"的区别，棋盘拓扑和走法规则完全不变。
- 每方 10 颗棋子，起始在己方角的三角区，目标是全部 10 颗棋子移入**正对面**
  的三角区。

## 2. 走法规则

每回合只能移动**一颗**棋子，且只能二选一：

1. **单步**：沿 6 个方向之一走到相邻空孔。
2. **跳跃链**：沿任一方向，若相邻孔有棋子（己方或对方均可）且该方向再远
   一格是空孔，则可以跳过去；落地后可以**继续**从新位置任意方向再跳
   （只要满足同样条件），一回合内可以连跳任意多次，**但同一个孔在本回合
   内不能走第二遍**（防止原地打转刷分）。玩家可以在任意一次落地后选择
   停止，不强制跳到不能再跳为止。
3. 单步和跳跃**不能混用**：选了跳跃链就必须全程跳，不能"跳一下再走一步"。
4. 没有吃子——跳过的棋子（己方/对方都一样）不会被移除，纯粹当"跳板"。
5. 若轮到的玩家确实无任何合法走法（极罕见，正常规则下几乎不会发生），
   规则引擎需要兜底：允许"过"或任选一颗棋子原地不动结束回合（实现时可
   先按"理论上不会出现"处理，加一条 assert/日志即可，不用特别设计 UI）。

## 3. 候选走法生成与剪枝（本局的核心难点）

**单步候选**：对己方 10 颗棋子各看 6 个方向，过滤越界/非空目标，通常
产出 10~40 个候选——规模可控，不需要特别裁剪。

**跳跃链候选（会指数爆炸，必须专门处理）**：
- 对每颗棋子做一次 **BFS/DFS**，节点 = 当前落点 + 已访问孔集合（同一回
  合不可重复落脚），边 = "沿某方向跳一次"。
- 不要把搜索树上的**每一个中间节点**都当成一个候选——否则一条长链会产出
  几十个候选。只保留：
  1. 每条链的**终点**（即这条跳跃路径能达到的最远位置，默认推荐玩家跳
     到底）；
  2. 外加链条上"净前进距离"局部最大的 1~2 个**中间停靠点**（给模型"可以
     少跳几步、换个方向"的选择权，但不需要穷举所有停靠点）。
- 对每颗棋子，**只保留"净前进距离"最大的 1~2 条跳跃链**，而不是这颗棋子
  所有可能的链——10 颗棋子 × 2 条 = 最多 20 条，再加单步候选后统一按
  "净前进距离"排序截断到 `MAX_MOVE_OPTIONS`（建议 14）。
- "净前进距离"定义：把棋盘中心到"己方目标三角区中心"的方向当作前进轴，
  计算这颗子从起点到终点在该轴上的投影位移，越大越好（这同时也是第5节
  启发式打分的核心指标）。

## 4. `build_state(game, color)` 返回字段

| 字段 | 说明 |
|---|---|
| `rules` | 规则全文，见第 6 节 |
| `you_are` | 当前玩家颜色/角标识 |
| `players` | 参与颜色列表 + 各自目标三角区标识（2/3/4/6 人通用） |
| `your_pegs` | 己方 10 颗棋子当前坐标列表 |
| `pegs_in_start_zone` | 己方还留在**出发三角区**里的棋子数（优先把它们"请出门"） |
| `pegs_in_home_zone` | 己方已经到达**目标三角区**的棋子数（越接近 10 越接近获胜） |
| `opponents_progress` | 各对手 `{color: pegs_in_home_zone}`，方便判断自己是否落后 |
| `candidate_moves` | 剪枝后的候选，单步标 `"(2,1)->(2,2) 单步"`，跳跃链标全路径 `"(2,1)->(2,3)->(2,5) 跳跃x2 净前进3"` |
| `recent_moves` | 最近几步走子记录，保持连续性 |
| `your_goal` | `"把你的10颗棋子全部移动到棋盘对面的目标三角区"` |

## 5. `build_questions()` 设计

```text
move               choice  从 candidate_moves 选一个（m0..mN，无"过"选项）
intent             choice  {advance_leader: 推进已经领先/靠前的棋子,
                             advance_straggler: 优先把还留在起点的棋子送出门,
                             jump_ladder: 搭桥布局，为后续更长跳跃链做准备,
                             block_lane: 占据关键孔位延缓对手的跳跃路线}
pace               score   1=保守稳步 .. 5=激进抢跳跃链
confidence_in_win  noul    0..1，当前"谁先到家"的胜率判断
```

## 6. 规则文本（`state["rules"]`）

```
CHINESE CHECKERS (跳棋), star-shaped board with 121 holes, 2/3/4/6 players
using opposite/evenly-spaced corner triangles. Each player starts with 10
pegs in their home triangle; the goal is to be the FIRST to move all 10
pegs into the triangle directly opposite your own.

MOVE (exactly one peg per turn, choose ONE of):
  - STEP: move to an adjacent empty hole (6 directions).
  - JUMP CHAIN: jump over an adjacent occupied hole (yours or an
    opponent's — jumped pegs are never removed, this game has no
    captures) landing on the empty hole directly beyond it; you may keep
    chaining more jumps from the new position in any direction, and may
    stop after any landing. A hole already visited earlier in the SAME
    turn cannot be revisited. Steps and jumps cannot be mixed in one turn.

WIN: first player to have all 10 pegs inside their target triangle wins.
There are no captures, no blocking wins — it is a pure race.

STRATEGY: a long jump chain can cover far more distance than a step, so
"net forward progress" (candidate_moves already reports this) matters
more than hop count. Pegs still in your own start triangle block your own
future jump chains through that area — clearing them out early is usually
worth it even if the single-move progress looks small.
```

## 7. 与 Flask 层对接

```python
def decide_move(client, game, color, deadline=None, on_late_result=None):
    """返回 (move_path_or_None, request, response, used_fallback)。
    move_path 是完整的落点序列（单步=长度1，跳跃链=长度N），
    前端按这个序列逐格播放棋子跳跃动画。"""
```

- 启发式打分（也是超时兜底）：`move_score = net_forward_progress
  + START_ZONE_BONUS(若清空的是起点里的子) - BLOCK_OWN_LANE_PENALTY(若
  这步棋会堵住自己其他棋子的跳跃线)`，与 doudizhu 的 `move_score` 同构。
- `Match` 状态机：N 人轮转（`turn = (turn+1) % n_players`），每人独立
  `controller`（human/jev/laya），终局判断 = 任一玩家 `pegs_in_home_zone
  == 10`。
- 2 人模式可直接复用 doudizhu/armychess 的双人 Match 循环骨架；3/4/6 人
  模式只需把"下一个行动者"从 `1-other_seat` 换成 `(turn+1)%n`，其余代码
  不变。

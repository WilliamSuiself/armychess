"""AI decision layer for Chinese Checkers — state+questions -> answers.

Follows the same contract as blackjack/doudizhu: `build_state` exposes the
full board situation (rules, own pegs, zones, pruned candidate moves with
net-forward labels), `build_questions` asks which candidate to play plus
intent/pace/confidence, and a heuristic fallback scores moves by net forward
progress when the backend is disabled or times out.
"""

import concurrent.futures
import random
import time

import rules
import weights as _weights_mod
import experience as _experience_mod

# Loaded once at import time; self_evolve.py writes weights_champion.json and
# a process restart (or explicit reload_weights()) is what picks up a newly
# promoted generation — same "swap a config file, not the code" pattern used
# for the champion/challenger promotion gate.
W = _weights_mod.load()
VALUE_COEFFS = _experience_mod.load_coeffs()


def reload_weights():
    global W
    W = _weights_mod.load()
    return W


def reload_value_model():
    global VALUE_COEFFS
    VALUE_COEFFS = _experience_mod.load_coeffs()
    return VALUE_COEFFS


def _learned_value(pegs, player):
    """Predicted moves-remaining-to-finish, fit on real completed games
    (experience.py) -- NOT the same signal as the short-horizon net-progress
    scoring above. Used only as a tie-breaker/injection check below: it can
    "see" that a move which looks like negative progress right now (step
    away from the target to set up a jump chain) actually reduces the real
    distance to finishing, which net-progress alone cannot express."""
    target = rules.target_zone(player)
    start = rules.START_ZONE[player]
    return _experience_mod.predict(VALUE_COEFFS,
                                    _experience_mod.featurize(pegs, target, start))

MAX_MOVE_OPTIONS = 14
OPENING_HALF_MOVES = 14    # ~7 moves each; while most pegs are still home

_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=4, thread_name_prefix="cc-ai")

OPENING_GUIDANCE = (
    "开局阶段提示:跳棋没有固定棋谱,但有公认的开局原则——1)优先把出发区最深处"
    "(角尖)的棋子送出去,它们出路最少,拖到最后只能一格一格往外爬;2)尽量走棋盘"
    "中路/中心区域,中心孔位跳板最多,占住中心也等于堵住对手的快车道;3)用己方"
    "前排棋子当跳板搭连跳链,远胜单步爬行;4)保持队形紧凑,不要让一颗子孤军深入"
    "与主力脱节。符合这些原则的候选会标注📖开局。")

FORMATION_GUIDANCE = (
    "中后局提示:胜负由【最后一颗到达的子】决定——它是真正的瓶颈。原则:"
    "1)优先推进当前最靠后的棋子,带🔗护尾标注的就是尾子的走法;2)不要让尾子"
    "落单:它周围2格内必须有己方子当跳板,否则只能每回合挪一格,宁可让前面的"
    "子留下来接力护送;3)进入目标区时优先填最深的角尖孔,前沿浅孔留到最后——"
    "反过来会逼得最后几颗子都去挤最深的孔。")

SHORT_RULES = (
    "CHINESE CHECKERS (跳棋), 121-hole star board, 2 players. Each side has "
    "10 pegs starting in one corner triangle; goal: be FIRST to move all 10 "
    "pegs into the triangle directly opposite. Per turn move exactly ONE peg: "
    "either STEP to an adjacent empty hole, or a JUMP over an adjacent "
    "occupied hole landing on the empty hole directly beyond, and you may "
    "keep chaining jumps from the new landing in any direction (stopping "
    "anytime). A hole cannot be visited twice in the same turn; steps and "
    "jumps cannot be mixed. No captures — jumped pegs stay put. Coordinates "
    "are axial (q,r): the 6 neighbor directions are q+1 / q-1 / r+1 / r-1 / "
    "q+1,r-1 / q-1,r+1. 'net' = hex-distance reduction toward your TARGET "
    "zone (bigger is better; drifting into a different side arm scores 0 or "
    "less — only moves that genuinely close on the target count).")


def _move_label(path):
    kind = "单步" if (len(path) == 2 and rules.adjacent(path[0], path[1])) \
        else f"跳跃x{len(path) - 1}"
    return rules.path_label(path) + " " + kind


def _hex_dist(a, b=(0, 0)):
    dq, dr = a[0] - b[0], a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def _net_progress(player, path):
    """Hex-distance improvement toward the NEAREST target-zone hole. The old
    1-D start->target axis projection rewarded drifting into the WRONG side
    arm (~60° overlap with the true direction); distance-to-zone can't."""
    target = rules.target_zone(player)
    d = lambda p: min(_hex_dist(p, h) for h in target)
    return d(path[0]) - d(path[-1])


def _apply_path(game, player, path):
    """Lightweight board-only clone with `path` already played — enough to
    call .legal_moves()/.occupied() on (they only touch .pegs)."""
    pegs = {p: set(game.pegs[p]) for p in rules.PLAYERS}
    pegs[player].discard(path[0])
    pegs[player].add(path[-1])
    sim = rules.Game.__new__(rules.Game)
    sim.pegs = pegs
    return sim


def _is_opening(game):
    return game.move_no <= OPENING_HALF_MOVES


def opening_score(game, player, path, w=None):
    """Conventional opening principles for the 2-player race — no fixed
    opening book exists, but these are widely agreed: back pegs out first
    (the corner tip has the fewest exits and strands last), route through
    the center (most jump options, and it blocks the opponent's highway),
    keep the formation tight enough to keep serving as jump screens."""
    W = w if w is not None else globals()["W"]
    if not _is_opening(game):
        return 0.0
    src, dst = path[0], path[-1]
    zone = rules.START_ZONE[player]
    s = 0.0
    if src in zone:
        # deeper home pegs earn more for leaving: depth-5 front row acts as
        # screens, depth-7/8 corner pegs are the classic stranded-peg risk
        s += W["open_deep_leave_base"] + W["open_deep_leave_per_depth"] * max(0, _hex_dist(src) - 5)
    else:
        # moving a field peg while deep home pegs are still stuck loses a
        # tempo — mild, the ladder bonus still dominates real chain value
        deep_left = sum(1 for p in game.pegs[player]
                        if p in zone and _hex_dist(p) >= 7)
        if deep_left >= 2:
            s += W["open_deep_stay_penalty"]
    if _hex_dist(dst) <= 4:          # central hexagon — contested highway
        s += W["open_center_bonus_near"]
    elif _hex_dist(dst) <= 6:
        s += W["open_center_bonus_far"]
    # landing next to a friendly peg keeps the jump scaffold intact
    if any(nb in game.pegs[player] and nb != src
           for nb in rules._neighbors(dst)):
        s += W["open_scaffold_bonus"]
    return s


def _peg_dists(game, player):
    """Each own peg's hex distance to its NEAREST target-zone hole, sorted."""
    target = rules.target_zone(player)
    td = lambda p: min(_hex_dist(p, h) for h in target)
    return sorted(td(p) for p in game.pegs[player])


def _rear_peg(game, player):
    target = rules.target_zone(player)
    return max(game.pegs[player],
               key=lambda p: min(_hex_dist(p, h) for h in target))


def formation_score(game, player, path, w=None):
    """Mid/late-game cohesion. In a race where ALL 10 pegs must arrive, the
    trailing peg is the real bottleneck — greedy 'biggest single jump' leaves
    the last 1-2 pegs isolated at the back with no teammates to leapfrog, so
    they crawl home one hex per turn. Principles encoded:
      1. advancing the rear peg(s) is worth extra;
      2. the rear peg must keep a teammate within jump range (<=2 hexes),
         so someone has to 'escort' — dashing ahead while it strands is bad;
      3. inside the target zone, fill the deep corner holes FIRST — they're
         the hardest to reach, so you want them gone while pegs are plentiful.
    """
    W = w if w is not None else globals()["W"]
    target = rules.target_zone(player)
    before = _peg_dists(game, player)
    sim = _apply_path(game, player, path)
    after = _peg_dists(sim, player)
    s = W["form_rear_weight"] * (before[-1] - after[-1]) + \
        W["form_rear2_weight"] * (before[-2] - after[-2])
    if after[-1] > 2:
        rear = _rear_peg(sim, player)
        escorted = any(q != rear and _hex_dist(q, rear) <= 2
                       for q in sim.pegs[player])
        s += W["form_escort_bonus"] if escorted else W["form_escort_penalty"]
    if path[-1] in target:
        s += W["form_corner_fill_weight"] * max(0, _hex_dist(path[-1]) - 6)
    return s


def two_ply_progress(game, player, path, w=None):
    """2-PLY LOOKAHEAD, adapted for a non-adversarial race game: there's no
    opponent reply to defend against (no captures here), so instead of a
    tactical safety net this measures whether the move sets up a bigger
    follow-up — this move's own net progress, PLUS a fraction of the best
    net-progress ANY of your pegs could make on the hypothetical next turn.
    Rewards "bridge-building" moves that unlock a long jump ladder even when
    the move itself only advances a little."""
    W = w if w is not None else globals()["W"]
    base = _net_progress(player, path)
    sim = _apply_path(game, player, path)
    next_legal = sim.legal_moves(player)
    best_next = max((_net_progress(player, p2) for p2 in next_legal), default=0.0)
    return base + W["ladder_weight"] * best_next, best_next


def build_state(game, player):
    zone = rules.START_ZONE[player]
    target = rules.target_zone(player)
    opp = rules.PLAYERS[1 - rules.PLAYERS.index(player)]

    # Two-stage funnel: a cheap 1-ply pass (net forward progress) widens
    # down to the best chains per peg, then the pricier 2-ply lookahead
    # (does this set up an even bigger follow-up jump next turn?) picks the
    # final Top N — same pattern as xiangqi/chess's hanging-piece lookahead,
    # just adapted to a non-adversarial race (no opponent reply to dodge).
    legal = game.legal_moves(player)
    scored = [(p, _net_progress(player, p)) for p in legal]
    scored.sort(key=lambda t: -t[1])
    best_per_peg = {}
    for path, net in scored:
        best_per_peg.setdefault(path[0], []).append((path, net))
    pre = []
    for peg, lst in best_per_peg.items():
        pre.extend(lst[:2])             # up to 2 best 1-ply chains per peg

    scored2 = lambda p: (two_ply_progress(game, player, p)[0]
                         + opening_score(game, player, p)
                         + formation_score(game, player, p))
    ranked = sorted(pre, key=lambda t: scored2(t[0]), reverse=True)
    pruned = ranked[:MAX_MOVE_OPTIONS]

    rear = _rear_peg(game, player)
    # force-include the trailing peg's best option — it's the bottleneck move
    if not any(t[0][0] == rear for t in pruned):
        rear_best = [t for t in pre if t[0][0] == rear]
        if rear_best:
            pruned = pruned[:MAX_MOVE_OPTIONS - 1] + \
                     [max(rear_best, key=lambda t: scored2(t[0]))]

    # Bridge-move injection. net_progress/two_ply_progress are short-horizon
    # (see/1-2 plies) and systematically exclude "step a peg AWAY from the
    # target to set up a jump chain" moves -- they always score as negative
    # progress one ply out, even when they're exactly the move needed to
    # unstick an endgame where the straight-line path is blocked. Found by
    # testing real no-interference endgames: the hand-scored shortlist can
    # plateau with every option in it scoring worse than doing nothing,
    # while a move outside the shortlist (checked via the value model
    # trained on real completed games, experience.py) would actually reduce
    # the predicted moves-to-finish. Only trust that signal and pay the
    # O(legal moves) scan cost when the current shortlist isn't helping.
    cur_value = _learned_value(game.pegs[player], player)
    best_pruned_value = min(
        (_learned_value(_apply_path(game, player, t[0]).pegs[player], player)
         for t in pruned), default=cur_value)
    bridge_path = None
    if best_pruned_value >= cur_value - 0.5:
        best_alt_value, best_alt_path = cur_value, None
        for mv in legal:
            if any(t[0] == mv for t in pruned):
                continue
            v = _learned_value(_apply_path(game, player, mv).pegs[player], player)
            if v < best_alt_value:
                best_alt_value, best_alt_path = v, mv
        if best_alt_path is not None and best_alt_value < cur_value - 0.5:
            bridge_path = best_alt_path
            pruned = pruned[:MAX_MOVE_OPTIONS - 1] + \
                     [(bridge_path, _net_progress(player, bridge_path))]

    candidates = []
    for i, (path, net) in enumerate(pruned):
        _combined, best_next = two_ply_progress(game, player, path)
        candidates.append({
            "opening": opening_score(game, player, path),
            "is_rear": path[0] == rear,
            "is_bridge": path == bridge_path,
            "id": f"m{i}",
            "move": rules.path_label(path),
            "path": path,
            "kind": "step" if len(path) == 2 else "jump",
            "net": net,
            "ladder_bonus": best_next,
        })

    state = {
        "turn": game.turn,
        "move_no": game.move_no,
        "you_are": player,
        "your_pegs": [list(p) for p in sorted(game.pegs[player])],
        "opponent_pegs": [list(p) for p in sorted(game.pegs[opp])],
        "your_start_zone": [list(p) for p in zone],
        "your_target_zone": [list(p) for p in target],
        "pegs_in_start_zone": game.pegs_in(player, zone),
        "pegs_in_home_zone": game.pegs_in(player, target),
        "target_direction_hint":
            "你的目标三角区尖端在坐标 (q,r)=%s 方向——走法若偏离该星角方向，"
            "净前进为0或负，属于绕路" % str(
                max(target, key=lambda h: _hex_dist(h))),
        "opponents_progress": {opp: game.pegs_in(opp,
                                                  rules.target_zone(opp))},
        "candidate_moves": [
            c["move"] + f" 净前进{c['net']}" +
            (f" 🪜此后可再跳净进{c['ladder_bonus']}" if c["ladder_bonus"] >= 1.5 else "") +
            (" 📖开局" if c["opening"] > 0 else "") +
            (" 🔗护尾" if c["is_rear"] else "") +
            (" 🧩搭桥(净前进为负/零，但为后续跳跃链让路，实测能解开卡死局面)"
             if c["is_bridge"] else "")
            for c in candidates],
        "candidate_ids": [c["id"] for c in candidates],
        "recent_moves": [
            {"player": m["player"], "move": rules.path_label(m["path"]),
             "kind": m["kind"]} for m in game.last_moves[-6:]],
        "your_goal": "把你的10颗棋子全部移动到棋盘对面的目标三角区",
        "rules": SHORT_RULES,
    }
    if _is_opening(game):
        state["opening_guidance"] = OPENING_GUIDANCE
    else:
        state["formation_guidance"] = FORMATION_GUIDANCE
    state["_candidates"] = candidates      # internal, not sent to the model
    return state


def build_questions(state):
    ids = state["candidate_ids"]
    criteria = {}
    for cid, move in zip(ids, state["candidate_moves"]):
        criteria[cid] = move
    return {
        "move": {
            "type": "choice",
            "instructions":
                "Pick ONE candidate move (candidate_moves). Prefer big net "
                "forward progress; also clear pegs still stuck in your start "
                "triangle. A '🪜此后可再跳净进X' tag means a 2-ply lookahead "
                "found that THIS move sets up an even bigger jump for one of "
                "your pegs next turn — often worth taking even if its own "
                "immediate net progress looks modest. A '📖开局' tag marks "
                "moves following conventional opening principles (deep pegs "
                "out first, route via the center, tight formation) — prefer "
                "them while the opening guidance applies. A '🔗护尾' tag marks "
                "the TRAILING (rear-most) peg's move — in the mid/late game "
                "the last peg to arrive decides the game, so keep it "
                "advancing and keep a teammate within jump range of it. A "
                "'🧩搭桥' tag marks a move with negative/zero immediate net "
                "progress that a value model (fit on real finished games) "
                "predicts still REDUCES your true moves-to-finish — take it "
                "when no other candidate actually helps; this is specifically "
                "for positions where everything else is a dead end and only "
                "a 'step back to open a jump chain' move actually unsticks it.",
            "criteria": criteria,
        },
        "intent": {
            "type": "choice",
            "instructions": "Main strategic intent of the chosen move:",
            "criteria": {
                "advance_leader": "推进已经领先/靠前的棋子",
                "advance_straggler": "把还留在出发三角区的棋子送出门",
                "jump_ladder": "搭桥布局，为后续更长跳跃链做准备",
                "block_lane": "占据关键孔位延缓对手的跳跃路线",
            },
        },
        "pace": {
            "type": "choice",
            "instructions": "Overall pace this stage:",
            "criteria": {
                "1": "保守稳步",
                "2": "偏保守",
                "3": "均衡",
                "4": "偏激进",
                "5": "激进抢跳跃链",
            },
        },
        "confidence_in_win": {
            "type": "noul",
            "instructions": "Probability you win from here, 0..1.",
        },
    }


# ---------------- heuristic fallback ----------------

def move_fallback(game, player, w=None):
    W = w if w is not None else globals()["W"]
    legal = game.legal_moves(player)
    if not legal:
        return None
    zone = rules.START_ZONE[player]

    rear = _rear_peg(game, player)

    def score_1ply(path):
        s = _net_progress(player, path) + formation_score(game, player, path, w)
        if path[0] in zone:            # get pegs out of the start triangle
            s += W["fallback_leave_zone_bonus"]
        s -= W["fallback_low_score_jump_penalty"] * (1 if len(path) > 2 else 0) if s <= 0 else 0
        return s

    # cheap 1-ply pass narrows the field, THEN the pricier 2-ply ladder
    # lookahead (each call re-runs legal_moves() for the hypothetical next
    # turn) only runs on a bounded pool — keeps this safe to call on every
    # legal move in the game, including the wide early-game branching. The
    # trailing peg's best move is force-included: it often only nets +1 and
    # would never survive the raw cut, yet it's exactly the bottleneck move.
    pre = sorted(legal, key=score_1ply, reverse=True)[:20]
    if not any(p[0] == rear for p in pre):
        rear_moves = [p for p in legal if p[0] == rear]
        if rear_moves:
            pre.append(max(rear_moves, key=score_1ply))
    final_score = lambda p: (two_ply_progress(game, player, p, w)[0] +
               (W["fallback_leave_zone_bonus"] if p[0] in zone else 0) +
               opening_score(game, player, p, w) +
               formation_score(game, player, p, w))
    best = max(pre, key=final_score)

    # Same bridge-move rescue as build_state's candidate injection: if even
    # the 2-ply-ranked best option doesn't reduce the real moves-to-finish
    # estimate (trained on completed games), it means every short-horizon
    # option is a dead end -- scan the full legal set for a move that does
    # and take that instead. Rare in the mid-game (there's almost always
    # SOME forward option), but this is exactly the deterministic fallback
    # path that gets used when the endgame is genuinely stuck.
    cur_value = _learned_value(game.pegs[player], player)
    if _learned_value(_apply_path(game, player, best).pegs[player], player) >= cur_value - 0.5:
        best_alt_value, best_alt = cur_value, None
        for mv in legal:
            v = _learned_value(_apply_path(game, player, mv).pegs[player], player)
            if v < best_alt_value:
                best_alt_value, best_alt = v, mv
        if best_alt is not None and best_alt_value < cur_value - 0.5:
            return best_alt
    return best


# ---------------- decision pipeline ----------------

def _parse(resp, ids):
    answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
    pick = "m0"
    try:
        pick = answers.get("move", {}).get("choice", "m0")
    except AttributeError:
        pass
    idx = int(str(pick).lstrip("m") or 0) if str(pick).lstrip("m").isdigit() else 0
    idx = max(0, min(idx, len(ids) - 1))
    return ids[idx]


def decide_move(client, game, player, deadline=None, on_late_result=None):
    """Returns (move_path_or_None, request, response, used_fallback)."""
    state = build_state(game, player)
    candidates = state.pop("_candidates")
    if not candidates:
        return None, None, {"auto": "no_legal_moves"}, False
    if len(candidates) == 1:
        return candidates[0]["path"], None, {"auto": "only_option"}, False
    fallback = move_fallback(game, player)
    questions = build_questions(state)
    request = {"state": state, "questions": questions}
    ids = [c["id"] for c in candidates]
    by_id = {c["id"]: c for c in candidates}
    parse = lambda r: by_id[_parse(r, ids)]["path"]

    if deadline is None:
        return parse(client.system_one(state=state, questions=questions)), \
            request, None, False
    t0 = time.time()
    fut = _CALL_EXECUTOR.submit(client.system_one, state=state,
                                questions=questions)
    try:
        resp = fut.result(timeout=deadline)
    except concurrent.futures.TimeoutError:
        if on_late_result:
            def _done(f):
                try:
                    r = f.result()
                except Exception as e:
                    r = {"error": repr(e)}
                try:
                    on_late_result(parse(r), request, r, time.time() - t0)
                except Exception:
                    pass
            fut.add_done_callback(_done)
        return fallback, request, {"fallback": "deadline_exceeded"}, True
    except Exception as e:
        return fallback, request, {"error": repr(e)}, True
    return parse(resp), request, resp, False

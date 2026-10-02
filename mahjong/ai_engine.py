"""AI decision layer for 4-player mahjong.

Two decision channels:
  - action (own turn after draw): discard / self-hu / gang options
  - claim (opponent discarded): hu / gang / peng / pass

The state is built around tile counting: live_tile_counts shows exactly how
many copies of each tile remain unaccounted for — the core skill this demo
exists to showcase.
"""

import concurrent.futures
import random
import time

import mahjong
from mahjong import (tile_label, shanten, wait_tiles, is_winning,
                     TILE_TYPES, COPIES)

_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="mj-ai-call")

SHORT_RULES = (
    "4-player mahjong (Sichuan style): 108 tiles, three suits 万/筒/条 1-9, "
    "no winds/dragons. Win = 4 melds + pair. You may peng (pair in hand + "
    "discard), gang (three in hand + discard, or four in hand), and hu on "
    "anyone's discard or your own draw — but NO chi. After a kong you draw a "
    "supplement tile. Discards and open melds are public; opponents' "
    "concealed hands and the wall are hidden. live_tile_counts tells you how "
    "many copies of each tile remain unknown — use it to judge which waits "
    "are live and which discards are safe.")


def _fmt(tiles):
    return [tile_label(t) for t in sorted(tiles)]


def _meld_label(m):
    t = tile_label(m.tiles[0])
    return {"peng": f"碰{t}", "gang": f"明杠{t}", "jia": f"加杠{t}",
            "angang": f"暗杠{t}"}[m.kind]


def build_state(game, seat):
    s = game.seats[seat]
    live = game.live_tile_counts(seat)
    concealed_shanten = shanten(s.concealed)
    waits = wait_tiles(s.concealed, len(s.melds))
    opponents = []
    for i in range(mahjong.NUM_SEATS):
        if i == seat:
            continue
        o = game.seats[i]
        opponents.append({
            "seat": i,
            "concealed_count": len(o.concealed),
            "open_melds": [_meld_label(m) for m in o.melds],
            "discards": _fmt(o.discards),
            "points": o.points,
        })
    pending = None
    if game.turn_phase == "claim" and game.pending_tile is not None:
        pending = {"tile": tile_label(game.pending_tile),
                   "discarded_by": game.pending_from}
    state = {
        "hand_number": game.hand_no,
        "dealer_seat": game.dealer,
        "your_seat": seat,
        "your_points": s.points,
        "your_concealed_tiles": _fmt(s.concealed),
        "your_open_melds": [_meld_label(m) for m in s.melds],
        "your_discards": _fmt(s.discards),
        "your_shanten": concealed_shanten,
        "your_wait_tiles": _fmt(waits),
        "live_tile_counts": live,
        "live_counts_note":
            "live_tile_counts = copies of each tile still unaccounted for "
            "(in the wall or opponents' hands) — pure tile-counting info.",
        "opponents": opponents,
        "wall_remaining": len(game.wall),
        "pending_discard": pending,
        "recent_history": [h for h in game.history[-12:]],
        "rules": SHORT_RULES,
    }
    return state


def _action_label(a):
    if a.startswith("discard:"):
        return f"打出{tile_label(int(a.split(':')[1]))}"
    if a == "hu:self":
        return "自摸胡牌!"
    if a.startswith("gang:an:"):
        return f"暗杠{tile_label(int(a.split(':')[2]))}"
    if a.startswith("gang:jia:"):
        return f"加杠{tile_label(int(a.split(':')[2]))}"
    return a


def build_questions(game, seat, options, kind):
    criteria = {f"m{i}": _action_label(o) for i, o in enumerate(options)}
    if kind == "claim":
        instructions = (
            "An opponent discarded the tile shown in pending_discard. Pick "
            "ONE response: hu (win now — usually take it), gang, peng "
            "(advances your hand but exposes it and skips others' draws), "
            "or pass. Consider your shanten, whether the tile improves your "
            "hand, and whether claiming reveals too much.")
        criteria = {f"m{i}": {"hu": "胡牌!", "gang": "明杠",
                              "peng": "碰", "pass": "过"}[o]
                    for i, o in enumerate(options)}
    else:
        instructions = (
            "Your turn — pick ONE action. Discarding: keep shanten low and "
            "prefer discarding tiles with low live_tile_counts that are safe "
            "(tiles opponents already discarded can't complete their open "
            "melds). Take hu:self/gang options when profitable; a concealed "
            "gang scores and draws a supplement but reveals the tile.")
    return {
        "action": {
            "type": "choice",
            "instructions": instructions,
            "criteria": criteria,
        },
        "reason": {
            "type": "choice",
            "instructions": "Main reason for the SAME action:",
            "criteria": {
                "reduce_shanten": "Keeps/improves my path to a ready hand",
                "tile_efficiency": "Best expected tile intake",
                "safe_discard": "Avoiding dealing into opponents",
                "score_now": "Hu/gang pays immediately",
                "wait_quality": "Keeps the most live outs",
            },
        },
        "confidence": {
            "type": "noul",
            "instructions": "Probability this action is best, 0..1.",
        },
    }


# ---------- heuristic fallback ----------

def _eval_discard(game, seat, t):
    """Score for discarding tile t: higher = better discard."""
    s = game.seats[seat]
    counts = s.concealed.count(t)
    score = 0.0
    # keep tiles that form/extend groups: simulate removal -> shanten delta
    before = shanten(s.concealed)
    tmp = list(s.concealed)
    tmp.remove(t)
    after = shanten(tmp)
    score += (after - before) * -30          # don't raise shanten
    score += -after * 10                     # prefer low resulting shanten
    live = game.live_tile_counts(seat)
    # discard isolated tiles; tiles with many live copies are keepable parts
    near = 0
    for d in (t - 2, t - 1, t + 1, t + 2):
        if 0 <= d < TILE_TYPES and d // 9 == t // 9:
            near += s.concealed.count(d)
    score -= near * 4                        # connected tiles are valuable
    score -= counts * 8                      # pairs/triples valuable
    # safety: tiles others discarded or melded are safer
    danger_vis = 0
    for i in range(mahjong.NUM_SEATS):
        if i == seat:
            continue
        o = game.seats[i]
        danger_vis += o.discards.count(t) * 6
        for m in o.melds:
            if t in m.tiles:
                danger_vis += 10
    score += danger_vis
    return score


def action_fallback(game, seat):
    legal = game.legal_actions(seat)
    if not legal:
        return f"discard:{game.seats[seat].concealed[0]}"
    if "hu:self" in legal:
        return "hu:self"
    discards = [a for a in legal if a.startswith("discard:")]
    if not discards:
        return legal[0]
    # concealed gang: usually take it late-game or when hand is nearly ready
    gangs = [a for a in legal if a.startswith("gang:")]
    s = game.seats[seat]
    if gangs and shanten(s.concealed) <= 1:
        return gangs[0]
    best = max(discards, key=lambda a: _eval_discard(
        game, seat, int(a.split(":")[1])))
    return best


def claim_fallback(game, seat):
    legal = game.legal_claims(seat)
    if not legal:
        return "pass"
    if "hu" in legal:
        return "hu"
    s = game.seats[seat]
    t = game.pending_tile
    if "gang" in legal:
        return "gang" if shanten(s.concealed) <= 2 else "pass"
    if "peng" in legal:
        tmp = list(s.concealed)
        tmp.remove(t); tmp.remove(t)
        gain = shanten(s.concealed) - shanten(tmp)
        return "peng" if gain > 0 or s.concealed.count(t) >= 3 else "pass"
    return "pass"


# ---------- decision pipeline ----------

def _parse_choice(resp, options):
    answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
    try:
        idx = int(str(answers.get("action", {}).get("choice", "m0"))
                  .lstrip("m"))
    except (ValueError, AttributeError):
        idx = 0
    idx = max(0, min(idx, len(options) - 1))
    return options[idx]


def _decide(kind, client, game, seat, legal_fn, fb_fn,
            deadline, on_late_result):
    legal = legal_fn(game, seat)
    if not legal:
        return None, None, None, False
    if len(legal) == 1:
        return legal[0], None, {"auto": "only_option"}, False
    fb = fb_fn(game, seat)
    state = build_state(game, seat)
    options = list(legal)
    random.shuffle(options)
    questions = build_questions(game, seat, options, kind)
    request = {"state": state, "questions": questions}
    parse = lambda r: _parse_choice(r, options)

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
                on_late_result(parse(r), request, r, time.time() - t0)
            fut.add_done_callback(_done)
        return fb, request, {"fallback": "deadline_exceeded"}, True
    except Exception as e:
        return fb, request, {"error": repr(e)}, True
    return parse(resp), request, resp, False


def decide_action(client, game, seat, deadline=None, on_late_result=None):
    return _decide("action", client, game, seat,
                   mahjong.Game.legal_actions, action_fallback,
                   deadline, on_late_result)


def decide_claim(client, game, seat, deadline=None, on_late_result=None):
    return _decide("claim", client, game, seat,
                   mahjong.Game.legal_claims, claim_fallback,
                   deadline, on_late_result)

"""AI decision layer for Blackjack — same state+questions -> answers contract.

The state deliberately exposes full card-counting information: every card
seen this shoe, the remaining rank breakdown, Hi-Lo running/true count.
Two decision points per round: bet size and each hit/stand/double action.
"""

import concurrent.futures
import random
import time

import rules
from rules import hand_value, card_label

MAX_OPTIONS = 8

_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="bj-ai-call")

SHORT_RULES = (
    "Blackjack vs dealer: closest to 21 without going over wins; A=1 or 11, "
    "face cards=10. Dealer must hit until 17 and stands on all 17s. Blackjack "
    "(natural 21 on 2 cards) pays 1.5x. 'double' doubles your bet but you get "
    "exactly ONE more card. Higher Hi-Lo true count = more big cards left in "
    "the shoe = better for the player (bigger bets, more standing).")


def _fmt(cards):
    return [card_label(c) for c in cards]


def build_state(game, seat):
    s = game.seats[seat]
    dealer_up = game.dealer_hand[0] if game.dealer_hand else None
    total, soft = hand_value(s.hand)
    seen_n = len(game.seen)
    total_cards = rules.NUM_DECKS * 52
    state = {
        "round": game.round_no,
        "phase": game.phase,
        "your_seat": seat,
        "your_chips": s.chips,
        "your_bet": s.bet,
        "your_hand": _fmt(s.hand),
        "your_total": total,
        "your_hand_is_soft": soft,
        "dealer_upcard": card_label(dealer_up) if dealer_up else None,
        "all_seats_chips": [x.chips for x in game.seats],
        "running_count_hi_lo": rules.running_count(game.seen),
        "decks_remaining": round(len(game.shoe) / 52, 2),
        "true_count": round(rules.running_count(game.seen) /
                            max(len(game.shoe) / 52, 0.25), 1),
        "cards_seen_this_shoe": seen_n,
        "remaining_ranks": rules.remaining_ranks(game.shoe),
        "count_note": "remaining_ranks shows exactly how many of each rank "
                      "are left in the shoe — true counting info. Positive "
                      "true count favors the player.",
        "rules": SHORT_RULES,
    }
    return state


def build_questions():
    return {
        "action": {
            "type": "choice",
            "instructions":
                "Pick ONE option for your hand. 'hit' draws a card, 'stand' "
                "ends your turn, 'double' doubles the bet for exactly one "
                "more card. Use the count: high true count -> more tens/aces "
                "left -> stand more, double more; low count -> play safer.",
            "criteria": {},
        },
        "reason": {
            "type": "choice",
            "instructions": "Main reason for the SAME action:",
            "criteria": {
                "basic_strategy": "Standard basic strategy for this total vs dealer upcard",
                "count_edge": "Deviated from basic strategy because of the count",
                "protect_lead": "Playing safe to protect a chip lead",
                "catch_up": "Behind on chips — taking extra risk",
            },
        },
        "confidence": {
            "type": "noul",
            "instructions": "Probability this action is correct, 0..1.",
        },
    }


def build_bid_questions():
    return {
        "bet": {
            "type": "choice",
            "instructions":
                "Pick your bet this round. Bet more when the true count is "
                "positive (rich shoe favors the player), less when negative. "
                "Also consider your chip position vs the other seats.",
            "criteria": {},
        },
        "confidence": {
            "type": "noul",
            "instructions": "How confident this bet sizing is right, 0..1.",
        },
    }


# ---------- heuristic fallback (simplified basic strategy + count) ----------

def action_fallback(game, seat):
    legal = game.legal_actions(seat)
    if not legal:
        return "stand"
    s = game.seats[seat]
    total, soft = hand_value(s.hand)
    up = rules.card_value(game.dealer_hand[0]) if game.dealer_hand else 10
    can_double = "double" in legal
    tc = (rules.running_count(game.seen) /
          max(len(game.shoe) / 52, 0.25))
    # count nudges: positive count -> stand one point earlier, double wider
    stand_adj = 1 if tc >= 2 else (-1 if tc <= -2 else 0)
    if soft:
        stand_at = 18 + stand_adj
        if can_double and 15 <= total <= 18 and up <= 6:
            return "double"
        return "stand" if total >= stand_at else "hit"
    stand_at = 17 + stand_adj
    if up >= 7:
        stand_at = 17            # vs strong upcard hit until 17
    elif up <= 6:
        stand_at = 12 + stand_adj  # dealer likely busts — stand on 12+
    if can_double and total in (10, 11):
        return "double"
    if can_double and total == 9 and 3 <= up <= 6:
        return "double"
    return "stand" if total >= stand_at else "hit"


def bet_fallback(game, seat):
    legal = game.legal_bets(seat)
    if not legal:
        return 0
    tc = (rules.running_count(game.seen) /
          max(len(game.shoe) / 52, 0.25))
    chips = game.seats[seat].chips
    # true-count driven sizing, clamped to legal choices
    if tc >= 4:
        want = 100
    elif tc >= 2:
        want = 50
    elif tc <= -2:
        want = 10
    else:
        want = 20
    best = min(legal, key=lambda b: abs(b - want))
    return min(best, chips)


# ---------- decision pipeline ----------

def _parse_choice(resp, options, answers_key):
    answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
    try:
        idx = int(str(answers.get(answers_key, {}).get("choice", "m0")).lstrip("m"))
    except (ValueError, AttributeError):
        idx = 0
    idx = max(0, min(idx, len(options) - 1))
    return options[idx]


def decide_action(client, game, seat, deadline=None, on_late_result=None):
    legal = game.legal_actions(seat)
    if not legal:
        return "stand", None, None, False
    if len(legal) == 1:
        return legal[0], None, {"auto": "only_option"}, False
    fallback = action_fallback(game, seat)
    labels = {"hit": "要牌 hit", "stand": "停牌 stand", "double": "加倍 double"}
    state = build_state(game, seat)
    questions = build_questions()
    options = list(legal)
    random.shuffle(options)
    questions["action"]["criteria"] = {f"m{i}": labels[o]
                                       for i, o in enumerate(options)}
    request = {"state": state, "questions": questions}
    parse = lambda r: _parse_choice(r, options, "action")

    if deadline is None:
        return parse(client.system_one(state=state, questions=questions)), \
            request, None, False
    t0 = time.time()
    fut = _CALL_EXECUTOR.submit(client.system_one, state=state, questions=questions)
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
        return fallback, request, {"fallback": "deadline_exceeded"}, True
    except Exception as e:
        return fallback, request, {"error": repr(e)}, True
    return parse(resp), request, resp, False


def decide_bet(client, game, seat, deadline=None, on_late_result=None):
    legal = game.legal_bets(seat)
    if not legal:
        return 0, None, None, False
    if len(legal) == 1:
        return legal[0], None, {"auto": "only_option"}, False
    fallback = bet_fallback(game, seat)
    state = build_state(game, seat)
    questions = build_bid_questions()
    options = list(legal)
    random.shuffle(options)
    questions["bet"]["criteria"] = {f"m{i}": f"押{b}筹码"
                                    for i, b in enumerate(options)}
    request = {"state": state, "questions": questions}
    parse = lambda r: _parse_choice(r, options, "bet")

    if deadline is None:
        return parse(client.system_one(state=state, questions=questions)), \
            request, None, False
    t0 = time.time()
    fut = _CALL_EXECUTOR.submit(client.system_one, state=state, questions=questions)
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
        return fallback, request, {"fallback": "deadline_exceeded"}, True
    except Exception as e:
        return fallback, request, {"error": repr(e)}, True
    return parse(resp), request, resp, False

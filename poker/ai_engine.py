"""AI decision layer for Texas Hold'em.

State exposes only public info + the acting seat's hole cards: board, street,
pot/stacks/invested, full action history per street, positions (button/SB/BB),
pot odds for the call, and remaining-deck info for range counting.
"""

import concurrent.futures
import random
import time

import holdem
from holdem import card_label, rank_idx, best_score, hand_name, STREET_CN

_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="pk-ai-call")

SHORT_RULES = (
    "No-limit Texas Hold'em, 3-handed. 4 streets: preflop, flop (3 cards), "
    "turn (+1), river (+1), then showdown. Best 5-card hand from your 2 hole "
    "cards + board wins. Only you can see your hole cards. Position matters: "
    "acting last postflop is an advantage. Opponents' hole cards are hidden "
    "until showdown — infer their range from their actions.")


def _fmt(cards):
    return [card_label(c) for c in cards]


def _positions(game):
    h = game.hand
    ev = h.events[0] if h.events else {}
    return {"button": game.button,
            "small_blind": ev.get("sb"), "big_blind": ev.get("bb")}


def _action_history(game):
    h = game.hand
    hist = []
    for e in h.events:
        k = e["kind"]
        if k == "blinds":
            hist.append(f"盲注: 按钮=seat{e['button']} 小盲=seat{e['sb']} "
                        f"大盲=seat{e['bb']}")
        elif k == "street":
            hist.append(f"--- {STREET_CN[e['street']]}: "
                        f"{' '.join(_fmt(e['board']))} ---")
        elif k == "raise":
            hist.append(f"seat{e['seat']} 加注到{e['to']}"
                        + ("(全下)" if e.get("allin") else ""))
        elif k == "call":
            hist.append(f"seat{e['seat']} 跟注{e['amount']}"
                        + ("(全下)" if e.get("allin") else ""))
        elif k in ("fold", "check"):
            hist.append(f"seat{e['seat']} "
                        + ("弃牌" if k == "fold" else "过牌"))
    return hist


def build_state(game, seat):
    h = game.hand
    s = game.seats[seat]
    to_call = h.current_bet - s.bet
    pot_now = sum(x.invested for x in game.seats)
    known = set(s.hole) | set(h.board)
    seen_cards = len(known)
    state = {
        "hand_number": game.hand_no,
        "street": h.street,
        "your_seat": seat,
        "your_hole_cards": _fmt(s.hole),
        "your_stack": s.stack,
        "your_bet_this_street": s.bet,
        "your_invested_this_hand": s.invested,
        "board": _fmt(h.board),
        "pot": pot_now,
        "current_bet_to_match": h.current_bet,
        "amount_to_call": min(to_call, s.stack),
        "pot_odds_if_call": round(min(to_call, s.stack) /
                                max(pot_now + min(to_call, s.stack), 1), 3),
        "positions": _positions(game),
        "opponents": [{"seat": i,
                       "stack": game.seats[i].stack,
                       "bet_this_street": game.seats[i].bet,
                       "folded": game.seats[i].folded,
                       "allin": game.seats[i].allin}
                      for i in range(3) if i != seat and game.seats[i].hole],
        "action_history_this_hand": _action_history(game),
        "known_cards_you_can_see": seen_cards,
        "card_counting_note":
            "board + your hole cards are known; the other 52-%d cards "
            "include opponents' hole cards + the undealt deck. Infer ranges "
            "from action_history." % seen_cards,
        "rules": SHORT_RULES,
    }
    return state


def build_questions(game, seat, options):
    h = game.hand
    labels = {}
    for a in options:
        if a.startswith("raise:"):
            labels[a] = f"加注到{a.split(':')[1]}"
        else:
            labels[a] = {"fold": "弃牌", "check": "过牌",
                         "call": "跟注"}[a]
    return {
        "action": {
            "type": "choice",
            "instructions":
                "Pick ONE action. Think about: hand strength, board texture, "
                "position, pot odds, stack sizes, and what your opponents' "
                "actions reveal about their range. Bluffing is allowed; a "
                "fold is sometimes correct.",
            "criteria": {f"m{i}": labels[o] for i, o in enumerate(options)},
        },
        "reason": {
            "type": "choice",
            "instructions": "Main reason for the SAME action:",
            "criteria": {
                "value": "Strong hand — building the pot",
                "pot_odds": "Price is right to continue",
                "bluff": "Representing strength / fold equity",
                "weak_hand": "Hand not worth continuing",
                "position": "Leveraging position / free cards",
                "range_read": "Opponent's line looks weak/strong",
            },
        },
        "opponent_range_guess": {
            "type": "choice",
            "instructions": "What do you think the most dangerous opponent "
                            "holds?",
            "criteria": {
                "monster": "Very strong (set/two pair+/nuts)",
                "strong": "Strong made hand (top pair good kicker+)",
                "medium": "Medium hand (mid pair / weak top pair)",
                "draw": "Drawing (flush/straight draw)",
                "weak": "Weak or air",
                "no_idea": "Cannot read yet",
            },
        },
        "confidence": {
            "type": "noul",
            "instructions": "Probability this is the best action, 0..1.",
        },
    }


# ---------- heuristic fallback ----------

def _preflop_strength(hole):
    r1, r2 = sorted((rank_idx(c) for c in hole), reverse=True)
    suited = hole[0][1] == hole[1][1]
    if r1 == r2:
        return 60 + r1 * 3                       # pairs
    score = r1 * 4 + r2
    if suited:
        score += 8
    if r1 - r2 == 1:
        score += 6                               # connectors
    elif r1 - r2 == 2:
        score += 3
    return score


def _postflop_strength(game, seat):
    s = game.seats[seat]
    cards = s.hole + game.hand.board
    if len(cards) < 5:
        return 0, "未成型"
    cat = best_score(cards)[0]
    return cat, hand_name(cards)


def fallback(game, seat):
    legal = game.legal_actions(seat)
    if not legal:
        return "fold"
    if "check" in legal and "call" not in legal:
        # free option — sometimes raise for value with strong hands
        h = game.hand
        if h.street == "preflop":
            st = _preflop_strength(game.seats[seat].hole)
            raises = [a for a in legal if a.startswith("raise:")]
            if st >= 70 and raises:
                return raises[0]
            return "check"
        cat, _ = _postflop_strength(game, seat)
        raises = [a for a in legal if a.startswith("raise:")]
        if cat >= 4 and raises:                  # straight+ -> value bet
            return raises[len(raises) // 2]
        if cat == 3 and raises:
            return raises[0]
        return "check"
    h = game.hand
    s = game.seats[seat]
    to_call = h.current_bet - s.bet
    pot = sum(x.invested for x in game.seats)
    pot_odds = min(to_call, s.stack) / max(pot + min(to_call, s.stack), 1)
    raises = [a for a in legal if a.startswith("raise:")]
    if h.street == "preflop":
        st = _preflop_strength(s.hole)
        if st >= 70:                             # strong pair / premium
            return raises[-1] if raises else "call"
        if st >= 40:
            return "call" if pot_odds < 0.35 else "fold"
        return "call" if pot_odds < 0.12 else "fold"
    cat, _ = _postflop_strength(game, seat)
    if cat >= 5:                                 # flush+
        return raises[-1] if raises else "call"
    if cat >= 3:                                 # trips+
        return raises[0] if raises else "call"
    if cat >= 2:
        return "call" if pot_odds < 0.35 else "fold"
    if cat == 1:
        return "call" if pot_odds < 0.20 else "fold"
    return "call" if pot_odds < 0.08 else "fold"


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


def decide(client, game, seat, deadline=None, on_late_result=None):
    legal = game.legal_actions(seat)
    if not legal:
        return "fold", None, None, False
    if len(legal) == 1:
        return legal[0], None, {"auto": "only_option"}, False
    fb = fallback(game, seat)
    state = build_state(game, seat)
    options = list(legal)
    random.shuffle(options)
    questions = build_questions(game, seat, options)
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

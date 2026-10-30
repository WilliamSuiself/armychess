"""AI decision layer for UNO — same state+questions -> answers contract.

Two decision points:
  - "turn": which legal card to play (or draw if none/forced)
  - "color": which color to declare after playing a wild/wild+4

State gives each seat's exact hand-size (visible info in physical UNO
too) plus a full "cards unaccounted for" breakdown (108 - discard - your
hand), since that's the UNO equivalent of card counting: it tells you how
many of each color/action are still live among opponents + the draw pile.
"""

import collections
import concurrent.futures
import random
import time

import uno
from uno import card_color, card_kind, card_label, card_points, is_wild, COLORS

MAX_OPTIONS = 8

_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="uno-ai-call")

SHORT_RULES = (
    "UNO, 4 players. Match top card's color or number/symbol, or play a wild "
    "anytime (wild+4 only if you have no card matching the current color). "
    "Skip=next player loses turn. Reverse=direction flips (acts as skip with "
    "2 left). +2/+4=next player draws that many and loses turn. If you have "
    "ANY legal card you must play one (no voluntary draw). First to empty "
    "their hand scores every other player's remaining card points (number="
    "face value, skip/reverse/+2=20, wild/+4=50); first to 300 total wins "
    "the match.")


def _unaccounted_cards(game, seat):
    """Deck composition minus discard pile minus your own hand = cards that
    are either in the draw pile or in an opponent's hand (indistinguishable
    from here, same as real UNO 'what's left' counting)."""
    full = collections.Counter(uno.new_deck())
    seen = collections.Counter(game.discard) + collections.Counter(game.hands[seat])
    full.subtract(seen)
    out = {k: v for k, v in full.items() if v > 0}
    by_color = collections.Counter()
    for card, n in out.items():
        c = card_color(card) or "wild"
        by_color[c] += n
    return out, dict(by_color)


def build_state(game, seat):
    hand = game.hands[seat]
    unaccounted, unaccounted_by_color = _unaccounted_cards(game, seat)
    others = [{"seat": s, "cards_left": len(game.hands[s]),
               "danger": "UNO! about to win" if len(game.hands[s]) == 1 else
                        ("low" if len(game.hands[s]) <= 3 else "")}
              for s in range(game.n) if s != seat]
    return {
        "round": game.round_no,
        "your_seat": seat,
        "your_hand": [card_label(c) for c in hand],
        "your_hand_codes": hand,
        "top_card": card_label(game.top_card()),
        "active_color": uno.COLOR_NAMES.get(game.active_color, game.active_color),
        "direction": "clockwise" if game.direction == 1 else "counter-clockwise",
        "opponents": others,
        "your_score": game.scores[seat],
        "all_scores": game.scores,
        "unaccounted_cards_total": sum(unaccounted.values()),
        "unaccounted_by_color": unaccounted_by_color,
        "count_note": "unaccounted_by_color = cards not in your hand and not "
                      "yet discarded (sitting in the draw pile or an "
                      "opponent's hand) — low count for a color means that "
                      "color is scarce, safer to demand it if you hold it.",
        "rules": SHORT_RULES,
    }


def build_play_questions(options, hand_after_counts=None):
    labels = {}
    for i, c in enumerate(options):
        tag = "draw" if c is None else card_label(c)
        extra = ""
        if c is not None and uno.is_action(c):
            extra = " (对下家使用攻击/控场牌)"
        elif c == "W4":
            extra = " (对下家 +4 并指定颜色)"
        elif c == "W":
            extra = " (指定颜色)"
        labels[f"m{i}"] = tag + extra
    return {
        "play": {
            "type": "choice",
            "instructions":
                "Pick ONE card to play (or 'draw' if listed/forced). Prefer "
                "dumping action cards (skip/reverse/+2/+4) on an opponent "
                "with cards_left<=2 — that's often lethal. Otherwise prefer "
                "playing your most color-cornered cards first (colors you "
                "have few options left in) and keep wilds for emergencies.",
            "criteria": labels,
        },
        "confidence": {
            "type": "noul",
            "instructions": "Confidence this is the best play, 0..1.",
        },
    }


def build_color_questions():
    return {
        "color": {
            "type": "choice",
            "instructions":
                "Which color should the wild become? Usually pick the color "
                "you hold the most of (keeps your future hand flexible). If "
                "an opponent is close to winning (cards_left<=2), consider a "
                "color they're unlikely to hold instead.",
            "criteria": {f"m{i}": f"{uno.COLOR_NAMES[c]}({c})" for i, c in enumerate(COLORS)},
        },
    }


# ---------------- heuristic fallback ----------------

def _best_color_for_hand(hand):
    counts = collections.Counter(card_color(c) for c in hand if not is_wild(c))
    if not counts:
        return random.choice(COLORS)
    return counts.most_common(1)[0][0]


def play_fallback(game, seat):
    legal = game.legal_plays(seat)
    if not legal:
        return None, None
    others_low = any(len(game.hands[s]) <= 2 for s in range(game.n) if s != seat)
    # 1) lethal/strong action cards against a low-hand opponent
    if others_low:
        attack = [c for c in legal if uno.is_action(c) or c == "W4"]
        if attack:
            c = max(attack, key=lambda c: card_points(c))
            return c, (_best_color_for_hand(game.hands[seat]) if is_wild(c) else None)
    # 2) prefer non-wild cards so wilds stay available for emergencies
    non_wild = [c for c in legal if not is_wild(c)]
    pool = non_wild or legal
    # prefer the color you're most stocked in (dump duplicates, stay flexible on scarce colors)
    color_counts = collections.Counter(card_color(c) for c in game.hands[seat] if not is_wild(c))
    def score(c):
        if is_wild(c):
            return -1
        s = color_counts.get(card_color(c), 0)
        if uno.is_action(c):
            s += 0.5
        return s
    best = max(pool, key=score)
    color = _best_color_for_hand(game.hands[seat]) if is_wild(best) else None
    return best, color


# ---------------- decision pipeline ----------------

def _parse_choice(resp, options, key):
    answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
    try:
        idx = int(str(answers.get(key, {}).get("choice", "m0")).lstrip("m"))
    except (ValueError, AttributeError):
        idx = 0
    return options[max(0, min(idx, len(options) - 1))]


def _call_with_deadline(client, state, questions, deadline, on_late_result, parse):
    request = {"state": state, "questions": questions}
    if deadline is None:
        return parse(client.system_one(state=state, questions=questions)), request, None, False
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
        return None, request, {"fallback": "deadline_exceeded"}, True
    except Exception as e:
        return None, request, {"error": repr(e)}, True
    return parse(resp), request, resp, False


def decide_turn(client, game, seat, deadline=None, on_late_result=None):
    """Returns (card_or_None, chosen_color_or_None, request, response, used_fallback).
    card is None when the seat must/should draw."""
    legal = game.legal_plays(seat)
    fb_card, fb_color = play_fallback(game, seat)
    if not legal:
        return None, None, None, {"auto": "forced_draw"}, False
    if len(legal) == 1 and len(legal) + (0 if game.must_draw_cards() else 0) == 1:
        c = legal[0]
        color = _best_color_for_hand(game.hands[seat]) if is_wild(c) else None
        return c, color, None, {"auto": "only_option"}, False

    options = list(legal)
    random.shuffle(options)
    state = build_state(game, seat)
    questions = build_play_questions(options)
    parse = lambda r: _parse_choice(r, options, "play")
    picked, request, resp, used_fallback = _call_with_deadline(
        client, state, questions, deadline, on_late_result, parse)
    card = picked if not used_fallback else fb_card
    color = None
    if card is not None and is_wild(card):
        color = decide_color(client, game, seat, deadline=deadline)[0] \
            if not used_fallback else fb_color
    return card, color, request, resp, used_fallback


def decide_color(client, game, seat, deadline=None, on_late_result=None):
    fallback = _best_color_for_hand(game.hands[seat])
    state = build_state(game, seat)
    questions = build_color_questions()
    options = list(COLORS)
    parse = lambda r: _parse_choice(r, options, "color")
    if client is None:
        return fallback, None, {"auto": "no_client"}, True
    picked, request, resp, used_fallback = _call_with_deadline(
        client, state, questions, deadline, on_late_result, parse)
    return (picked if not used_fallback else fallback), request, resp, used_fallback

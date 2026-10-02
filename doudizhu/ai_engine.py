"""AI decision layer for Dou Dizhu — same state+questions -> typed-answers
contract as army-chess / tetris-arena (Jev /systemone; Laya drop-in).

Two decision points:
  - bidding: choice among legal bids (不叫/1分/2分/3分)
  - playing: choice among enumerated legal plays (+ 不出 when allowed)

Both get a `deadline` path: if the backend call doesn't finish in time the
local heuristic pick is applied immediately and the late result is only logged.
"""

import concurrent.futures
import random
import time

import cards

MAX_MOVE_OPTIONS = 14

_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="ddz-ai-call")

SHORT_RULES = (
    "Dou Dizhu (Fight the Landlord), 3 players: one Landlord vs two Peasants "
    "(peasants cooperate — either one emptying their hand wins for BOTH). "
    "On your turn you must beat the leading play with the SAME type and length "
    "but higher rank, or pass. Bombs (4 of a kind) beat everything except a "
    "bigger bomb; the Rocket (both jokers) beats everything. Whoever empties "
    "their hand first decides the game for their whole side.")

RANK_ORD = "3<4<5<6<7<8<9<10<J<Q<K<A<2<小王<大王"


def _fmt_hand(hand):
    return [cards.card_label(c) for c in cards.sort_cards(hand)]


def build_state(game, seat):
    """Fields a decision needs go FIRST — small backends truncate over-budget
    state from the end."""
    landlord = game.landlord
    my_role = None
    if landlord is not None:
        my_role = "landlord" if seat == landlord else "peasant"
    others = {s: len(game.hands[s]) for s in game.SEATS if s != seat}
    last = game.lead_play
    state = {
        "phase": game.phase,
        "your_seat": seat,
        "your_role": my_role or "bidding (unknown yet)",
        "your_hand": _fmt_hand(game.hands[seat]),
        "your_hand_size": len(game.hands[seat]),
        "other_hand_sizes": others,
        "landlord_seat": landlord,
        "current_turn_seat": game.turn,
        "rank_order": RANK_ORD,
    }
    if landlord is not None:
        partner = partner_seat(game, seat) if seat != landlord else None
        if seat != landlord:
            state["your_partner_seat"] = partner
            state["partner_cards_left"] = len(game.hands[partner])
            opp_sizes = {landlord: len(game.hands[landlord])}
        else:
            opp_sizes = {s: len(game.hands[s])
                         for s in game.SEATS if s != seat}
        state["opponents_cards_left"] = opp_sizes
        one = [s for s, n in opp_sizes.items() if n == 1]
        if one:
            state["tactical_tip"] = (
                f"Opponent seat {one[0]} has exactly ONE card — do NOT lead "
                f"a single: they top it and win instantly. Any multi-card "
                f"lead (pair/triple/straight/bomb…) is UNBEATABLE for them "
                f"— they can't form the same type.")
        else:
            two = [s for s, n in opp_sizes.items() if n == 2]
            if two:
                state["tactical_tip"] = (
                    f"Opponent seat {two[0]} has exactly TWO cards — leads of "
                    f"3+ cards (triples/straights/combos) CANNOT be matched "
                    f"by them, only bombed; singles/pairs still let them "
                    f"answer.")
        if seat != landlord and len(game.hands[partner]) <= 2:
            state["teamwork_tip"] = (
                f"Your peasant partner (seat {partner}) has only "
                f"{len(game.hands[partner])} card(s) left — they win the game "
                f"for BOTH of you the moment they go out. Help them: lead your "
                f"SMALLEST single (1 card left) or smallest pair (2 cards left), "
                f"and never play over their cards.")
    if game.phase == "bidding":
        cnt = cards.counts(game.hands[seat])
        runs = sorted(r for r in cnt if r <= 14)
        longest = 0
        for i in range(len(runs)):
            j = i
            while j + 1 < len(runs) and runs[j + 1] == runs[j] + 1:
                j += 1
            longest = max(longest, j - i + 1)
        state["bidding"] = {
            "bid_order": game.bid_order,
            "bids_so_far": {str(s): v for s, v in game.bids.items()},
            "current_highest_bid": game.current_max_bid,
            "note": "Each seat bids once, must exceed the current highest bid "
                    "or pass (0). Highest bidder becomes the Landlord and takes "
                    "the 3 bottom cards.",
        }
        state["hand_strength"] = {
            "quads": [cards.RANK_LABEL[r] for r, v in cnt.items() if v == 4],
            "jokers": sorted(r for r in cnt if r >= 16),
            "twos": cnt.get(15, 0),
            "triples": [cards.RANK_LABEL[r] for r, v in cnt.items() if v >= 3],
            "longest_run_length": longest,
            "note": "Strong hands (quads/both jokers/many 2s/long runs) justify "
                    "higher bids.",
        }
    else:
        state["bottom_cards"] = _fmt_hand(game.bottom)
        played_flat = []
        for h in game.history:
            if h["kind"] == "play":
                played_flat.extend(h["cards"])
        state["cards_played_so_far"] = [cards.card_label(c)
                                        for c in cards.sort_cards(played_flat)]
        # Card counting: which A/2/jokers are still unseen (in opponents' hands
        # or unused bottom) — e.g. if 大王 is already out, 小王 is the top card.
        seen = cards.counts(played_flat) + cards.counts(game.hands[seat])
        unseen_top = []
        for r in (17, 16, 15, 14):
            left = (1 if r >= 16 else 4) - seen.get(r, 0)
            unseen_top += [cards.RANK_LABEL[r]] * left
        state["unseen_top_cards"] = unseen_top
        # Full unplayed list = whole deck − played − own hand ⇒ everything in
        # the other two hands; landlord's 3 bottom cards are marked (底) —
        # only the landlord knows them for sure, opponents just know they're
        # somewhere in the landlord's hand.
        played_set = set(played_flat)
        mine_set = set(game.hands[seat])
        bottom_set = set(game.bottom)
        remaining = [c for c in cards.sort_cards(
            [s + ch for s in cards.SUITS for ch in cards.RCHARS] + ["js", "jb"])
            if c not in played_set and c not in mine_set]
        state["cards_elsewhere"] = [
            cards.card_label(c) + ("(底)" if c in bottom_set else "")
            for c in remaining]
        state["cards_elsewhere_note"] = (
            "Unplayed cards not in your hand = held by the other two players; "
            "(底) marks the 3 bottom cards which are certainly in the "
            "landlord's hand.")
        if last:
            last_role = ("landlord" if last["seat"] == landlord else
                         ("partner" if seat != landlord else "peasant"))
            state["play_to_beat"] = {
                "seat": last["seat"], "played_by": last_role,
                "their_cards_left": len(game.hands[last["seat"]]),
                "label": cards.play_label(last["cards"]),
                "type": last["type"], "main_rank": last["main"],
                "cards": _fmt_hand(last["cards"])}
        else:
            state["play_to_beat"] = "none — you LEAD, play anything legal"
        # who acts next, in order — feeding your partner only works if they
        # act BEFORE the landlord; likewise you may want to "cover" when the
        # landlord acts right after a weak partner
        order = []
        for i in (1, 2):
            s = (seat + i) % 3
            if s == landlord:
                role = "landlord (opponent)"
            elif seat == landlord:
                role = "peasant (opponent)"
            else:
                role = "partner"
            order.append(f"seat{s} = {role}")
        state["acting_order_after_you"] = order
        state["recent_history"] = brief_history_tail(game)
        state["your_goal"] = (
            "You are a PEASANT: help EITHER peasant (you or your partner) empty "
            "their hand first — you win together, so never fight your partner's "
            "plays." if my_role == "peasant"
            else "You are the LANDLORD: empty your hand first; you fight BOTH "
            "peasants alone.")
    state["rules"] = SHORT_RULES
    return state


def brief_history_tail(game, n=10):
    from doudizhu import brief_history
    return brief_history(game, n)


def build_questions():
    return {
        "move": {
            "type": "choice",
            "instructions":
                "Pick ONE option. When following, options must beat the leading "
                "play (same type/length, higher rank) or be 'pass'. When "
                "LEADING, prefer low/medium plays — do NOT waste your biggest "
                "cards (2/jokers) or bombs early. Consider: shedding many "
                "cards, keeping bombs for emergencies, and (as a peasant) NOT "
                "fighting your partner's plays.",
            "criteria": {},
        },
        "intent": {
            "type": "choice",
            "instructions": "Strategic purpose of the SAME move you picked:",
            "criteria": {
                "shed_cards": "Dump cards / longest combo to empty hand faster",
                "control": "Take/seize the lead to dictate the next play type",
                "block": "Stop the opponent (esp. the landlord) from going out",
                "feed_partner": "Deliberately play a small card so your peasant "
                    "partner (close to going out) can shed their last cards",
                "support_partner": "Let your peasant partner's play stand",
                "save_strength": "Cheap move, keep big cards and bombs for later",
            },
        },
        "confidence_in_win": {
            "type": "noul",
            "instructions": "Your side's probability of winning now, 0..1.",
        },
        "aggression": {
            "type": "score",
            "instructions": "How aggressive is this move (1=very passive .. "
                             "5=all-in, bombs/rocket spent freely)?",
            "criteria": ["very passive", "passive", "balanced",
                          "aggressive", "all-in"],
        },
    }


# ---------- heuristics (fallback + candidate ordering) ----------

def partner_seat(game, seat):
    """The other non-landlord seat (only meaningful when seat is a peasant)."""
    return next(s for s in game.SEATS
                if s != seat and s != game.landlord)


def opponent_sizes(game, seat):
    """{seat: cards_left} for the seats this player is actually fighting."""
    if game.landlord is None:
        return {}
    if seat == game.landlord:
        return {s: len(game.hands[s]) for s in game.SEATS if s != seat}
    return {game.landlord: len(game.hands[game.landlord])}


def move_score(game, seat, play_cards):
    """Higher = better. Used to shortlist candidates AND as the deadline
    fallback. Favors shedding many cards cheaply; heavy penalty for spending
    bombs/rocket; peasant bonus for passing on a partner's lead."""
    p = cards.classify(play_cards)
    hand_n = len(game.hands[seat])
    score = len(play_cards) * 2.0                       # shed more cards
    score -= p["main"] * 0.35                           # prefer low ranks
    if len(play_cards) == hand_n:
        score += 200                                    # instant win
    if p["type"] == "bomb":
        score -= 25
    if p["type"] == "rocket":
        score -= 60
    # peasant cooperation
    last = game.lead_play
    if game.landlord is not None and seat != game.landlord:
        partner = partner_seat(game, seat)
        if last and last["seat"] == partner:
            score -= 12                                 # partner led — pass-ish
            if len(play_cards) == hand_n:
                score += 200                            # unless it wins outright
        # partner close to going out: feed them with the smallest fitting lead
        if game.lead_play is None:
            pn = len(game.hands[partner])
            if pn == 1 and p["type"] == "single" and p["main"] <= 12:
                score += 30
            elif pn == 2 and p["type"] == "pair" and p["main"] <= 12:
                score += 30
            elif pn <= 2 and p["type"] in ("bomb", "rocket"):
                score -= 30
    # opponent close to going out:
    if game.lead_play is None:
        opp_min = min(opponent_sizes(game, seat).values(), default=99)
        # a play containing MORE cards than an opponent holds is structurally
        # unmatchable for them (only a bomb could answer)
        if len(play_cards) > opp_min:
            score += 20
        # but a single vs their LAST card can be topped for an instant win
        if p["type"] == "single":
            if opp_min == 1:
                score -= 40
            elif opp_min == 2:
                score -= 15
    score += random.random() * 0.1
    return score


def pass_score(game, seat):
    """Heuristic value of passing (only meaningful when allowed)."""
    last = game.lead_play
    if last is None:
        return -999
    score = 0.0
    partner = None
    if game.landlord is not None and seat != game.landlord:
        partner = partner_seat(game, seat)
        if last["seat"] == partner:
            score += 15          # partner's play — let it ride
    # An OPPONENT holds the lead: passing hands them the initiative for free,
    # and the fewer cards they hold the more it costs us — at 1-2 cards left
    # they go out on their next lead, so contest at almost any price.
    if game.landlord is not None and last["seat"] != partner:
        leader_n = len(game.hands[last["seat"]])
        if leader_n <= 2:
            score -= 40
        elif leader_n <= 5:
            score -= 12
        else:
            score -= 5
    return score


def pick_fallback(game, seat):
    """Pure-local move choice (no backend). Returns card list or None=pass."""
    legal = game.legal_plays(seat)
    if not legal:
        return None
    best = max(legal, key=lambda p: move_score(game, seat, p))
    if game.can_pass(seat):
        if pass_score(game, seat) > move_score(game, seat, best):
            return None
    return best


def bid_fallback(game, seat):
    """Simple bid heuristic: strong hands (bombs/2s/jokers) bid higher."""
    cnt = cards.counts(game.hands[seat])
    strength = (cnt.get(17, 0) * 4 + cnt.get(16, 0) * 3
                + cnt.get(15, 0) * 2 + sum(1 for v in cnt.values() if v == 4) * 5)
    want = 0 if strength < 5 else (1 if strength < 9 else (2 if strength < 13 else 3))
    legal = game.legal_bids(seat)
    legal_nonzero = [v for v in legal if v > 0]
    if want > 0 and legal_nonzero:
        return min(want, max(legal_nonzero)) if want > game.current_max_bid else 0
    return 0


# ---------- candidate labels ----------

def compact_label(game, seat, play_cards):
    """Terse option label for the model (small backends have a tiny per-option
    token budget)."""
    if play_cards is None:
        return "pass"
    p = cards.classify(play_cards)
    lbl = cards.play_label(play_cards)
    n = len(play_cards)
    left = len(game.hands[seat]) - n
    out = f"{lbl}({n}张,剩{left})"
    if p and p["type"] == "bomb":
        out = "BOMB " + out
    if p and p["type"] == "rocket":
        out = "ROCKET " + out
    return out


def shortlist_moves(game, seat, max_options=MAX_MOVE_OPTIONS):
    """Ranked candidate plays for the choice question + whether pass is in.

    Also prunes obviously-wasteful options BEFORE they reach the model — a
    bomb/rocket or a top single (2/王) offered as a legal choice occasionally
    gets picked "for fun" even when it's a terrible lead/follow. Keeping them
    only when they're actually justified (about to win, or an opponent is
    about to go out) removes most of those pointless big plays."""
    legal = game.legal_plays(seat)
    can_pass = game.can_pass(seat)
    if not legal:
        return [], can_pass
    ranked = sorted(legal, key=lambda p: move_score(game, seat, p), reverse=True)

    hand_n = len(game.hands[seat])
    leading = game.lead_play is None
    # is an OPPONENT close to going out? (peasants don't bomb each other)
    threat = False
    partner_leading = False
    if not leading and game.landlord is not None:
        last_seat = game.lead_play["seat"]
        is_partner = (seat != game.landlord and last_seat != game.landlord)
        if is_partner:
            partner_leading = True
        elif len(game.hands[last_seat]) <= 3:
            threat = True

    opp_min = min(opponent_sizes(game, seat).values(), default=99)

    filtered = []
    for p in ranked:
        cp = cards.classify(p)
        t, m = cp["type"], cp["main"]
        wins_now = len(p) == hand_n
        # Partner (fellow peasant) holds the lead — their play stands unless we
        # can win outright. Offering anything else just lets the model burn a
        # big card fighting its own teammate.
        if partner_leading and not wins_now:
            continue
        # An opponent down to ONE card can top any single we lead and win —
        # don't offer singles at all while multi-card options remain.
        if leading and t == "single" and opp_min == 1 and not wins_now:
            continue
        if t in ("bomb", "rocket") and not wins_now:
            if leading and hand_n > 5:
                continue
            if not leading and not threat and len(filtered) >= 3:
                continue
        if leading and t == "single" and m >= 15 and hand_n > 5 \
                and len(filtered) >= 3:
            continue
        filtered.append(p)
    if not filtered and not partner_leading:
        filtered = ranked[:1]
    return filtered[:max_options], can_pass


def decide_move(client, game, seat, deadline=None, on_late_result=None):
    """Returns (play_or_None, request, response, used_fallback)."""
    # Guaranteed win in hand -> just take it; asking the model only risks it
    # picking 'pass' instead of going out.
    legal = game.legal_plays(seat)
    hand_n = len(game.hands[seat])
    winners = [p for p in legal if len(p) == hand_n]
    if winners:
        return winners[0], None, {"auto": "winning_play"}, False

    shortlist, can_pass = shortlist_moves(game, seat)
    if not shortlist:
        return None, None, None, False

    fallback = pick_fallback(game, seat)
    options = list(shortlist)
    if can_pass:
        options = options + [None]
    random.shuffle(options)  # avoid positional bias, mirrors army-chess
    state = build_state(game, seat)
    questions = build_questions()
    criteria = {f"m{i}": (compact_label(game, seat, o) if o is not None else "pass 不出")
                for i, o in enumerate(options)}
    questions["move"]["criteria"] = criteria
    request = {"state": state, "questions": questions}

    def parse(resp):
        answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
        pm = answers.get("move", {})
        try:
            idx = int(str(pm.get("choice", "m0")).lstrip("m"))
        except (ValueError, AttributeError):
            idx = 0
        idx = max(0, min(idx, len(options) - 1))
        return options[idx]

    if deadline is None:
        response = client.system_one(state=state, questions=questions)
        return parse(response), request, response, False

    t0 = time.time()
    future = _CALL_EXECUTOR.submit(client.system_one, state=state, questions=questions)
    try:
        response = future.result(timeout=deadline)
    except concurrent.futures.TimeoutError:
        if on_late_result is not None:
            def _finish(fut):
                try:
                    resp = fut.result()
                except Exception as e:
                    resp = {"error": repr(e)}
                on_late_result(parse(resp), request, resp, time.time() - t0)
            future.add_done_callback(_finish)
        return fallback, request, {"fallback": "deadline_exceeded",
                                   "deadline": deadline}, True
    except Exception as e:
        return fallback, request, {"error": repr(e)}, True
    return parse(response), request, response, False


def decide_bid(client, game, seat, deadline=None, on_late_result=None):
    """Returns (bid_value, request, response, used_fallback)."""
    legal = game.legal_bids(seat)
    if not legal:
        return 0, None, None, False
    fallback = bid_fallback(game, seat)

    state = build_state(game, seat)
    labels = {0: "不叫", 1: "叫1分", 2: "叫2分", 3: "叫3分"}
    questions = {
        "bid": {
            "type": "choice",
            "instructions": "Bid for the Landlord role based on your hand "
                             "strength. You take 3 extra cards but fight both "
                             "peasants alone. Must exceed the current highest "
                             "bid (or choose 不叫).",
            "criteria": {f"m{i}": f"{labels[v]}" for i, v in enumerate(legal)},
        },
        "confidence_in_win": {
            "type": "noul",
            "instructions": "If you became Landlord, your probability of "
                             "winning, 0..1.",
        },
    }
    request = {"state": state, "questions": questions}

    def parse(resp):
        answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
        b = answers.get("bid", {})
        try:
            idx = int(str(b.get("choice", "m0")).lstrip("m"))
        except (ValueError, AttributeError):
            idx = 0
        idx = max(0, min(idx, len(legal) - 1))
        return legal[idx]

    if deadline is None:
        response = client.system_one(state=state, questions=questions)
        return parse(response), request, response, False

    t0 = time.time()
    future = _CALL_EXECUTOR.submit(client.system_one, state=state, questions=questions)
    try:
        response = future.result(timeout=deadline)
    except concurrent.futures.TimeoutError:
        if on_late_result is not None:
            def _finish(fut):
                try:
                    resp = fut.result()
                except Exception as e:
                    resp = {"error": repr(e)}
                on_late_result(parse(resp), request, resp, time.time() - t0)
            future.add_done_callback(_finish)
        return fallback, request, {"fallback": "deadline_exceeded"}, True
    except Exception as e:
        return fallback, request, {"error": repr(e)}, True
    return parse(response), request, response, False

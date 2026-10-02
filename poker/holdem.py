"""Texas Hold'em engine — 3-player, no-limit, rotating blinds.

- 4 streets: preflop -> flop -> turn -> river -> showdown.
- Actions: fold / check / call / raise (discrete sizes: min, half-pot, pot,
  all-in) — enough for real betting texture while staying enumerable for the
  AI contract.
- Correct layered side pots for all-ins.
- Public info only in AI state: hole cards of the acting seat, board, full
  action history, stacks/pot — memory + range inference is the demo point.

Card format: rank+suit, e.g. "As" "Td" "9c". Ranks 2..9,T,J,Q,K,A.
"""

import itertools
import random

RCHARS = "23456789TJQKA"
SUITS = ("s", "h", "d", "c")

CHIPS_START = 1000
SMALL_BLIND = 10
BIG_BLIND = 20
MAX_HANDS = 20                 # match ends after this many hands
MIN_RAISE_MULT = 1             # min raise = last raise size (or BB preflop)


def rank_idx(card):
    return RCHARS.index(card[0])


def card_label(card):
    sym = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}[card[1]]
    return sym + ("10" if card[0] == "T" else card[0])


def new_deck(rng):
    d = [r + s for r in RCHARS for s in SUITS]
    rng.shuffle(d)
    return d


# ---------- hand evaluator ----------

def _eval5(cards):
    """Score a 5-card hand: higher tuple wins."""
    ranks = sorted((rank_idx(c) for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    flush = len(set(suits)) == 1
    uniq = sorted(set(ranks), reverse=True)
    straight_high = None
    if len(uniq) == 5:
        if uniq[0] - uniq[4] == 4:
            straight_high = uniq[0]
        elif uniq == [12, 3, 2, 1, 0]:          # wheel A-5
            straight_high = 3
    from collections import Counter
    cnt = Counter(ranks)
    groups = sorted(cnt.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    counts = [c for _, c in groups]
    ordered = []
    for r, _ in groups:
        ordered += [r] * cnt[r]
    if flush and straight_high is not None:
        return (8, straight_high)
    if counts == [4, 1]:
        return (7, groups[0][0], groups[1][0])
    if counts == [3, 2]:
        return (6, groups[0][0], groups[1][0])
    if flush:
        return (5, *ranks)
    if straight_high is not None:
        return (4, straight_high)
    if counts == [3, 1, 1]:
        return (3, *ordered)
    if counts == [2, 2, 1]:
        return (2, *ordered)
    if counts == [2, 1, 1, 1]:
        return (1, *ordered)
    return (0, *ranks)


def best_score(cards):
    """Best 5-of-7 score for up to 7 cards."""
    if len(cards) <= 5:
        return _eval5(cards)
    return max(_eval5(c) for c in itertools.combinations(cards, 5))


HAND_NAME = ["高牌", "一对", "两对", "三条", "顺子", "同花", "葫芦", "四条", "同花顺"]


def hand_name(cards):
    return HAND_NAME[best_score(cards)[0]]


# ---------- game engine ----------

STREETS = ["preflop", "flop", "turn", "river"]
STREET_BOARD = {"preflop": 0, "flop": 3, "turn": 4, "river": 5}


class Seat:
    def __init__(self):
        self.stack = CHIPS_START
        self.hole = []
        self.bet = 0               # chips committed this street
        self.invested = 0          # chips committed this hand
        self.folded = False
        self.allin = False
        self.acted = False         # acted since last raise this street


class Hand:
    """One hand of poker."""

    def __init__(self, game):
        self.g = game
        self.deck = new_deck(game.rng)
        self.board = []
        self.street_idx = 0
        self.current_bet = 0
        self.min_raise_to = 0      # absolute "raise to" lower bound
        self.pot = 0
        self.turn = None
        self.finished = False      # hand done (fold-out or showdown)
        self.showdown = False
        self.last_raiser = None
        self.events = []           # action log (public)

    @property
    def street(self):
        return STREETS[self.street_idx]

    def live_seats(self):
        return [i for i, s in enumerate(self.g.seats) if s.hole and not s.folded]

    def acting_seats(self):
        return [i for i in self.live_seats() if not self.g.seats[i].allin]


class Game:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.seats = [Seat(), Seat(), Seat()]
        self.button = 2            # first hand: button=0 after rotation
        self.hand_no = 0
        self.hand = None
        self.phase = "playing"     # playing | handover | over
        self.hand_end_at = 0.0
        self.last_result = None
        self._start_hand()

    # ---------- hand lifecycle ----------

    def _alive(self):
        return [i for i, s in enumerate(self.seats) if s.stack > 0]

    def _start_hand(self):
        self.hand_no += 1
        if self.hand_no > MAX_HANDS or len(self._alive()) < 2:
            self.phase = "over"
            return
        self.phase = "playing"
        alive = self._alive()
        self.button = next(i for i in range(self.button + 1, self.button + 4)
                           if i % 3 in alive) % 3
        for s in self.seats:
            s.hole, s.bet, s.invested = [], 0, 0
            s.folded = s.allin = s.acted = False
        h = Hand(self)
        self.hand = h
        # blinds: next two alive seats after button
        order = [i for i in range(self.button + 1, self.button + 4)
                 if i % 3 in alive]
        sb, bb = order[0] % 3, order[1] % 3
        for seat in range(3):
            if seat in alive:
                self.seats[seat].hole = [h.deck.pop(), h.deck.pop()]
            else:
                self.seats[seat].folded = True
        self._post(sb, SMALL_BLIND)
        self._post(bb, BIG_BLIND)
        h.current_bet = min(BIG_BLIND, max(s.invested for s in self.seats
                                           if s.hole))
        h.min_raise_to = h.current_bet + BIG_BLIND
        h.events.append({"kind": "blinds", "sb": sb, "bb": bb,
                         "button": self.button})
        # preflop: first to act = left of BB
        h.turn = self._next_after(bb)
        self._check_street_done()

    def _post(self, seat, amount):
        s = self.seats[seat]
        amt = min(amount, s.stack)
        s.stack -= amt
        s.bet += amt
        s.invested += amt
        s.allin = s.stack == 0
        return amt

    def _next_after(self, seat, require_action=True):
        h = self.hand
        for i in range(1, 4):
            n = (seat + i) % 3
            s = self.seats[n]
            if s.hole and not s.folded and (not require_action or not s.allin):
                return n
        return None

    # ---------- legal actions ----------

    def legal_actions(self, seat):
        h, s = self.hand, self.seats[seat]
        if self.phase != "playing" or h is None or h.turn != seat \
                or s.folded or s.allin:
            return []
        to_call = h.current_bet - s.bet
        out = []
        if to_call > 0:
            out.append("fold")
            call_cost = min(to_call, s.stack)
            out.append("call")
        else:
            out.append("check")
        # raise options: distinct target "raise-to" amounts
        if s.stack > to_call:
            lo = max(h.min_raise_to, h.current_bet + BIG_BLIND)
            targets = set()
            pot_after_call = h.pot + sum(x.bet for x in self.seats) + to_call
            for t in (lo,
                      h.current_bet + max(BIG_BLIND, pot_after_call // 2),
                      h.current_bet + max(BIG_BLIND, pot_after_call)):
                if t > h.current_bet:
                    targets.add(t)
            max_to = s.bet + s.stack
            for t in sorted(targets):
                t = min(t, max_to)
                if t > h.current_bet and t <= max_to:
                    out.append(f"raise:{t}")
            if f"raise:{max_to}" not in out and max_to > h.current_bet:
                out.append(f"raise:{max_to}")
        return out

    # ---------- applying ----------

    def apply_action(self, seat, action):
        legal = self.legal_actions(seat)
        if not legal:
            raise ValueError("not your turn")
        h, s = self.hand, self.seats[seat]
        to_call = h.current_bet - s.bet
        if action == "fold" and "fold" in legal:
            s.folded = True
            h.events.append({"kind": "fold", "seat": seat})
        elif action == "check" and "check" in legal:
            h.events.append({"kind": "check", "seat": seat})
        elif action == "call" and "call" in legal:
            amt = self._post(seat, min(to_call, s.stack))
            h.events.append({"kind": "call", "seat": seat, "amount": amt,
                             "allin": s.allin})
        elif action.startswith("raise:"):
            target = int(action.split(":")[1])
            opts = {int(a.split(":")[1]) for a in legal
                    if a.startswith("raise:")}
            if opts:
                target = min(opts, key=lambda t: abs(t - target))
            else:
                raise ValueError("raise not legal")
            add = min(target - s.bet, s.stack)
            prev_bet = h.current_bet
            self._post(seat, add)
            new_bet = s.bet
            if new_bet > prev_bet:
                h.current_bet = new_bet
                h.min_raise_to = new_bet + max(new_bet - prev_bet, BIG_BLIND)
                h.last_raiser = seat
                for i in range(3):
                    if i != seat and self.seats[i].hole \
                            and not self.seats[i].folded:
                        self.seats[i].acted = False
            h.events.append({"kind": "raise", "seat": seat,
                             "to": new_bet, "allin": s.allin})
        else:
            raise ValueError(f"illegal action {action}")
        s.acted = True
        if len(h.live_seats()) == 1:
            self._finish_fold()
            return
        h.turn = self._next_after(seat)
        self._check_street_done()

    def _check_street_done(self):
        h = self.hand
        if h is None or h.finished:
            return
        live = h.live_seats()
        if len(live) <= 1:
            self._finish_fold()
            return
        # all live players either all-in or acted & matched
        pending = [i for i in h.acting_seats()
                   if not self.seats[i].acted or
                   self.seats[i].bet < h.current_bet]
        if pending:
            if h.turn is None or self.seats[h.turn].folded \
                    or self.seats[h.turn].allin:
                h.turn = pending[0] if not self.seats[pending[0]].allin \
                    else None
            return
        if not h.acting_seats():
            # everyone all-in -> run out board then showdown
            while h.street_idx < 3:
                h.street_idx += 1
                h.board += [h.deck.pop() for _ in
                            range(STREET_BOARD[STREETS[h.street_idx]] -
                                  len(h.board))]
            self._showdown()
            return
        if h.street_idx >= 3:
            self._showdown()
            return
        # next street
        h.street_idx += 1
        h.board += [h.deck.pop() for _ in
                    range(STREET_BOARD[h.street] - len(h.board))]
        h.events.append({"kind": "street", "street": h.street,
                         "board": list(h.board)})
        h.current_bet = 0
        h.min_raise_to = BIG_BLIND
        h.last_raiser = None
        for s in self.seats:
            s.bet = 0
            s.acted = False
        h.turn = self._next_after(self.button)

    # ---------- finishes ----------

    def _pot_total(self):
        return self.hand.pot + sum(s.invested for s in self.seats)

    def _finish_fold(self):
        h = self.hand
        w = h.live_seats()[0]
        pot = self._pot_total()
        self.seats[w].stack += pot
        h.events.append({"kind": "win_fold", "seat": w, "pot": pot})
        self.last_result = {"type": "fold", "winners": [w], "pot": pot}
        h.finished = True
        self.phase = "handover"

    def _showdown(self):
        h = self.hand
        h.showdown = True
        live = h.live_seats()
        scores = {i: best_score(self.seats[i].hole + h.board) for i in live}
        # layered side pots
        payouts = {i: 0 for i in range(3)}
        invested = {i: self.seats[i].invested for i in live}
        remaining = dict(invested)
        while any(v > 0 for v in remaining.values()):
            layer = min(v for v in remaining.values() if v > 0)
            contrib = {i: layer for i, v in remaining.items() if v > 0}
            pot_layer = sum(contrib.values())
            eligible = [i for i in contrib if i in scores]
            best = max(scores[i] for i in eligible)
            winners = [i for i in eligible if scores[i] == best]
            share = pot_layer // len(winners)
            for w in winners:
                payouts[w] += share
            for i in contrib:
                remaining[i] -= layer
        for i in live:
            self.seats[i].stack += payouts[i]
        winners = [i for i in live if payouts[i] == max(payouts.values())
                   and payouts[i] > 0]
        h.events.append({"kind": "showdown",
                         "hole": {str(i): list(self.seats[i].hole)
                                  for i in live},
                         "board": list(h.board),
                         "scores": {str(i): hand_name(
                             self.seats[i].hole + h.board) for i in live},
                         "winners": winners,
                         "pot": sum(invested.values())})
        self.last_result = {"type": "showdown", "winners": winners,
                            "pot": sum(invested.values()),
                            "hole": {str(i): list(self.seats[i].hole)
                                     for i in live},
                            "names": {str(i): hand_name(
                                self.seats[i].hole + h.board) for i in live}}
        h.finished = True
        self.phase = "handover"

    def next_hand(self):
        if self.phase == "handover":
            self._start_hand()

    @property
    def over(self):
        return self.phase == "over"

    def winner(self):
        if self.phase != "over":
            return None
        return max(range(3), key=lambda i: self.seats[i].stack)


STREET_CN = {"preflop": "翻牌前", "flop": "翻牌", "turn": "转牌", "river": "河牌"}
ACTION_CN = {"fold": "弃牌", "check": "过牌", "call": "跟注", "raise": "加注"}

"""Blackjack (21点) rules — multi-seat vs dealer, 4-deck shoe.

- Dealer stands on ALL 17s (incl. soft 17).
- Player actions: hit / stand / double (double only as first action).
- Blackjack (natural 21 on the first two cards) pays 1.5x; dealer natural
  beats everything except a player natural (push).
- No split / insurance / surrender (kept simple).
- Hi-Lo running count + remaining-rank breakdown exposed for the AI layer —
  card counting is the whole point of this demo.
"""

import random

RCHARS = "A23456789TJQK"          # A..K index 0..12
SUITS = ("s", "h", "d", "c")
NUM_DECKS = 4
RESHUFFLE_BELOW = 52             # <1 deck left -> reshuffle
CHIPS_START = 1000
BET_CHOICES = [10, 20, 50, 100]


def rank_of(card):
    return RCHARS.index(card[1]) + 1          # 1..13


def card_value(card):
    r = rank_of(card)
    return 11 if r == 1 else min(r, 10)


def hand_value(cards):
    """Return (total, soft)."""
    total = sum(min(rank_of(c), 10) if rank_of(c) > 1 else 11 for c in cards)
    aces = sum(1 for c in cards if rank_of(c) == 1)
    soft = False
    while aces and total > 21:
        total -= 10
        aces -= 1
    if aces:
        soft = True
    return total, soft


def is_blackjack(cards):
    return len(cards) == 2 and hand_value(cards)[0] == 21


def card_label(card):
    sym = {"s": "♠", "h": "♥", "d": "♦", "c": "♣"}[card[0]]
    return sym + ("10" if card[1] == "T" else card[1])


def is_red(card):
    return card[0] in ("h", "d")


def new_shoe(rng):
    return [s + ch for _ in range(NUM_DECKS) for s in SUITS for ch in RCHARS]


def remaining_ranks(shoe):
    """{rank_char: count} left in shoe — raw card-counting info."""
    out = {}
    for c in shoe:
        out[c[1]] = out.get(c[1], 0) + 1
    return out


def running_count(seen):
    """Hi-Lo count over all cards dealt so far this shoe."""
    cnt = 0
    for c in seen:
        r = rank_of(c)
        if r <= 6:
            cnt += 1
        elif r >= 10:
            cnt -= 1
    return cnt


class Seat:
    def __init__(self):
        self.chips = CHIPS_START
        self.hand = []
        self.bet = 0
        self.stood = False
        self.busted = False
        self.doubled = False
        self.natural = False
        self.done = False            # finished acting this round


class Game:
    """One match = repeated rounds until a bust or MAX_ROUNDS."""

    MAX_ROUNDS = 30

    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.shoe = new_shoe(self.rng)
        self.rng.shuffle(self.shoe)
        self.seen = []                       # all cards dealt this shoe
        self.seats = [Seat(), Seat(), Seat()]
        self.dealer_hand = []
        self.dealer_hole_hidden = True
        self.phase = "betting"               # betting | acting | dealer | settle | over
        self.round_no = 0
        self.turn = None                     # seat currently acting
        self.history = []
        self._start_round()

    def _draw(self):
        if len(self.shoe) < RESHUFFLE_BELOW:
            self.shoe = new_shoe(self.rng)
            self.rng.shuffle(self.shoe)
            self.seen = []
            self.history.append({"kind": "reshuffle"})
        c = self.shoe.pop()
        self.seen.append(c)
        return c

    def _start_round(self):
        self.round_no += 1
        if self.round_no > self.MAX_ROUNDS:
            self.phase = "over"
            return
        for s in self.seats:
            s.hand, s.bet = [], 0
            s.stood = s.busted = s.doubled = s.natural = s.done = False
        self.dealer_hand = []
        self.dealer_hole_hidden = True
        self.phase = "betting"
        self.turn = next((i for i, s in enumerate(self.seats) if s.chips > 0), None)
        if self.turn is None:
            self.phase = "over"
        self.history.append({"kind": "round", "n": self.round_no})

    # ---------- betting ----------

    def legal_bets(self, seat):
        if self.phase != "betting" or seat != self.turn:
            return []
        return [b for b in BET_CHOICES if b <= self.seats[seat].chips]

    def apply_bet(self, seat, amount):
        legal = self.legal_bets(seat)
        if amount not in legal:
            amount = min(legal) if legal else 0
        s = self.seats[seat]
        s.bet = amount
        self.history.append({"kind": "bet", "seat": seat, "amount": amount})
        nxt = next((i for i in range(seat + 1, 3)
                    if self.seats[i].chips > 0), None)
        if nxt is None:
            self._deal_round()
        else:
            self.turn = nxt

    def _deal_round(self):
        for _ in range(2):
            for s in self.seats:
                if s.bet > 0:
                    s.hand.append(self._draw())
            self.dealer_hand.append(self._draw())
        for i, s in enumerate(self.seats):
            if s.bet > 0 and is_blackjack(s.hand):
                s.natural = s.done = True
        self.phase = "acting"
        self.turn = next((i for i, s in enumerate(self.seats)
                          if s.bet > 0 and not s.done), None)
        if self.turn is None:
            self._dealer_play()

    # ---------- player actions ----------

    def legal_actions(self, seat):
        if self.phase != "acting" or seat != self.turn:
            return []
        s = self.seats[seat]
        acts = ["hit", "stand"]
        if len(s.hand) == 2 and s.chips >= s.bet * 2:
            acts.append("double")
        return acts

    def apply_action(self, seat, action):
        if action not in self.legal_actions(seat):
            raise ValueError(f"illegal action {action}")
        s = self.seats[seat]
        if action == "hit":
            s.hand.append(self._draw())
            if hand_value(s.hand)[0] > 21:
                s.busted = s.done = True
            self.history.append({"kind": "hit", "seat": seat,
                                 "card": s.hand[-1],
                                 "total": hand_value(s.hand)[0]})
        elif action == "double":
            s.doubled = True
            s.hand.append(self._draw())
            if hand_value(s.hand)[0] > 21:
                s.busted = True
            s.done = True
            self.history.append({"kind": "double", "seat": seat,
                                 "card": s.hand[-1],
                                 "total": hand_value(s.hand)[0]})
        else:
            s.stood = s.done = True
            self.history.append({"kind": "stand", "seat": seat})
        self.turn = next((i for i in range(seat + 1, 3)
                          if self.seats[i].bet > 0 and not self.seats[i].done),
                         None)
        if self.turn is None:
            self._dealer_play()

    # ---------- dealer & settle ----------

    def _dealer_play(self):
        self.dealer_hole_hidden = False
        while hand_value(self.dealer_hand)[0] < 17:
            self.dealer_hand.append(self._draw())
        self.history.append({"kind": "dealer",
                             "cards": list(self.dealer_hand),
                             "total": hand_value(self.dealer_hand)[0]})
        dv, _ = hand_value(self.dealer_hand)
        dealer_nat = is_blackjack(self.dealer_hand)
        results = []
        for i, s in enumerate(self.seats):
            if s.bet <= 0:
                continue
            pv, _ = hand_value(s.hand)
            won = push = False
            if s.natural and dealer_nat:
                push = True
            elif s.natural:
                s.chips += int(s.bet * 1.5); won = True
            elif dealer_nat or s.busted or (pv <= dv <= 21):
                s.chips -= s.bet * (2 if s.doubled else 1)
            elif dv > 21 or pv > dv:
                s.chips += s.bet * (2 if s.doubled else 1); won = True
            else:
                push = True
            results.append({"seat": i, "pv": pv, "won": won, "push": push,
                            "chips": s.chips})
        self.history.append({"kind": "settle", "results": results})
        self.phase = "settle"
        self.turn = None

    def next_round(self):
        if self.phase == "settle":
            if all(s.chips <= 0 for s in self.seats) or \
                    sum(1 for s in self.seats if s.chips > 0) <= 0:
                self.phase = "over"
            else:
                self._start_round()

    @property
    def over(self):
        return self.phase == "over" or self.round_no > self.MAX_ROUNDS

    def winner(self):
        """Highest chips at match end (None until over)."""
        if self.phase != "over":
            return None
        best = max(range(3), key=lambda i: self.seats[i].chips)
        return best


SEAT_CN = {0: "座位0", 1: "座位1", 2: "座位2"}

"""Core UNO engine: deck, legality, turn order, scoring.

Four seats, standard 108-card deck. Pure game rules, no Flask/AI deps so it
can be unit-tested standalone (mirrors tetris.py / doudizhu.py / mahjong.py).

Card representation: a 2-char-ish code string.
  - number cards:  "<color><digit>"      e.g. "R5", "G0"
  - skip:          "<color>S"            e.g. "Y S" -> stored as "YS"
  - reverse:       "<color>V"                        "BV"
  - draw two:      "<color>D"                         "RD"
  - wild:          "W"
  - wild draw four: "W4"
Colors: R Y G B (wild cards have no fixed color; W/W4 get a color assigned
once played, tracked separately as `active_color`).

Simplifications vs. physical UNO (documented, not bugs):
  - A player who CAN legally play a card MUST play one (no voluntary draw).
    This halves the decision space without changing the strategic core.
  - No "catch failing to call UNO" penalty mini-game; reaching 1 card is
    just tracked/announced automatically.
  - Stacking draw-2/draw-4 onto each other is not allowed (common but not
    universal house rule) — the next player simply draws and is skipped.
"""

import random

COLORS = ["R", "Y", "G", "B"]
NUM_SEATS = 4
TARGET_SCORE = 300   # match ends when some seat's cumulative score >= this
HAND_SIZE = 7

COLOR_NAMES = {"R": "红", "Y": "黄", "G": "绿", "B": "蓝"}


def new_deck(rng=None):
    rng = rng or random
    deck = []
    for c in COLORS:
        deck.append(f"{c}0")
        for n in range(1, 10):
            deck.append(f"{c}{n}")
            deck.append(f"{c}{n}")
        for sym in ("S", "V", "D"):
            deck.append(f"{c}{sym}")
            deck.append(f"{c}{sym}")
    deck.extend(["W"] * 4)
    deck.extend(["W4"] * 4)
    rng.shuffle(deck)
    return deck


def card_color(card):
    return None if card in ("W", "W4") else card[0]


def card_kind(card):
    """Matching key ignoring color: digit string, 'S'/'V'/'D', or None for wilds."""
    if card in ("W", "W4"):
        return None
    return card[1:]


def is_wild(card):
    return card in ("W", "W4")


def is_action(card):
    return not is_wild(card) and card_kind(card) in ("S", "V", "D")


def card_points(card):
    if card in ("W", "W4"):
        return 50
    k = card_kind(card)
    if k in ("S", "V", "D"):
        return 20
    return int(k)


SYMBOL_LABEL = {"S": "禁出", "V": "反转", "D": "+2"}


def card_label(card):
    if card == "W":
        return "万能"
    if card == "W4":
        return "万能+4"
    c, k = card_color(card), card_kind(card)
    name = COLOR_NAMES.get(c, c)
    if k in SYMBOL_LABEL:
        return f"{name}{SYMBOL_LABEL[k]}"
    return f"{name}{k}"


class Game:
    def __init__(self, num_seats=NUM_SEATS, rng=None):
        self.n = num_seats
        self.rng = rng or random.Random()
        self.scores = [0] * self.n
        self.history = []
        self.round_no = 0
        self.match_over = False
        self.match_winner = None
        self._start_round()

    # ---------------- round lifecycle ----------------

    def _start_round(self):
        self.round_no += 1
        self.deck = new_deck(self.rng)
        self.hands = [[] for _ in range(self.n)]
        for _ in range(HAND_SIZE):
            for s in range(self.n):
                self.hands[s].append(self.deck.pop())
        # flip a starting card that isn't a wild draw-4 (house rule: redraw if W4)
        self.discard = []
        while True:
            top = self.deck.pop()
            if top != "W4":
                self.discard.append(top)
                break
            self.deck.insert(0, top)
            self.rng.shuffle(self.deck)
        self.active_color = card_color(top) if not is_wild(top) else self.rng.choice(COLORS)
        self.direction = 1
        self.turn = 0
        self.pending_draw_count = 0   # cards the *current* player must draw before playing (after D/W4)
        self.must_draw = False
        self.round_over = False
        self.round_winner = None
        self.last_play_seat = None
        self._apply_effect_fresh_start(top)

    def _apply_effect_fresh_start(self, top):
        """Apply the flipped starting card's effect (standard UNO house rule)."""
        if top == "S":
            self.turn = self._advance(self.turn, 1)
        elif card_kind(top) == "V":
            self.direction *= -1
        elif card_kind(top) == "D":
            self.pending_draw_count = 2
            self.must_draw = True

    def _advance(self, seat, steps):
        return (seat + steps * self.direction) % self.n

    def top_card(self):
        return self.discard[-1]

    # ---------------- legality ----------------

    def legal_plays(self, seat):
        if self.round_over or self.match_over:
            return []
        hand = self.hands[seat]
        top = self.top_card()
        has_color_match = any(card_color(c) == self.active_color for c in hand
                              if not is_wild(c))
        out = []
        for c in hand:
            if c == "W":
                out.append(c)
            elif c == "W4":
                if not has_color_match:
                    out.append(c)
            elif card_color(c) == self.active_color or card_kind(c) == card_kind(top):
                out.append(c)
        return out

    def must_draw_cards(self):
        return self.pending_draw_count if self.must_draw else 0

    # ---------------- draw pile ----------------

    def _ensure_deck(self, need=1):
        while len(self.deck) < need:
            if len(self.discard) <= 1:
                return  # nothing to reshuffle from; caller should handle empty draw
            top = self.discard.pop()
            self.rng.shuffle(self.discard)
            self.deck.extend(self.discard)
            self.discard = [top]

    def draw(self, seat, n=1):
        drawn = []
        for _ in range(n):
            self._ensure_deck(1)
            if not self.deck:
                break
            drawn.append(self.deck.pop())
        self.hands[seat].extend(drawn)
        return drawn

    # ---------------- actions ----------------

    def apply_draw(self, seat):
        """Current player has no legal play (or must eat a pending D2/D4): draw."""
        if seat != self.turn or self.round_over:
            raise ValueError("not your turn")
        n = self.pending_draw_count if self.must_draw else 1
        drawn = self.draw(seat, n)
        forced = self.must_draw
        self.pending_draw_count = 0
        self.must_draw = False
        if forced:
            # drawing to satisfy a +2/+4 always ends the turn immediately
            self.turn = self._advance(self.turn, 1)
        else:
            # drew because no legal play existed; may play the drawn card if it helps,
            # otherwise turn passes
            playable = drawn and drawn[-1] in self.legal_plays(seat)
            if not playable:
                self.turn = self._advance(self.turn, 1)
        return drawn

    def apply_play(self, seat, card, chosen_color=None):
        if seat != self.turn or self.round_over:
            raise ValueError("not your turn")
        if card not in self.legal_plays(seat):
            raise ValueError(f"illegal play: {card}")
        self.hands[seat].remove(card)
        self.discard.append(card)
        self.last_play_seat = seat
        if is_wild(card):
            if chosen_color not in COLORS:
                chosen_color = self.rng.choice(COLORS)
            self.active_color = chosen_color
        else:
            self.active_color = card_color(card)

        if not self.hands[seat]:
            self._end_round(seat)
            return

        k = card_kind(card)
        if k == "S":
            self.turn = self._advance(self.turn, 2)
        elif k == "V":
            if self.n == 2:
                self.turn = self._advance(self.turn, 2)  # acts as skip for 2p
            else:
                self.direction *= -1
                self.turn = self._advance(self.turn, 1)
        elif k == "D":
            nxt = self._advance(self.turn, 1)
            self.pending_draw_count = 2
            self.must_draw = True
            self.turn = nxt
        elif card == "W4":
            nxt = self._advance(self.turn, 1)
            self.pending_draw_count = 4
            self.must_draw = True
            self.turn = nxt
        else:
            self.turn = self._advance(self.turn, 1)

    def _end_round(self, winner):
        self.round_over = True
        self.round_winner = winner
        pts = sum(card_points(c) for s in range(self.n) if s != winner
                  for c in self.hands[s])
        self.scores[winner] += pts
        self.history.append({"round": self.round_no, "winner": winner, "points": pts})
        if self.scores[winner] >= TARGET_SCORE:
            self.match_over = True
            self.match_winner = winner

    def next_round(self):
        if self.match_over:
            return
        self._start_round()

    def hand_sizes(self):
        return [len(h) for h in self.hands]

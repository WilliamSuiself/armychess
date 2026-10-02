"""4-player Mahjong (四人麻将 / 川麻式) — 108 tiles, no honors, no chi.

Tiles: man(万) 0-8, pin(筒) 9-17, sou(条) 18-26; four copies each = 108.
Claims on a discard: hu > gang > peng (no chi — Sichuan-style 4-player).
Kongs: ming (off discard), jia (add 4th to own peng), an (concealed).
After any kong the player draws a supplement tile.
Win: standard 4 melds + pair. Scoring: zimo each pays 400; dianpao discarder
pays 800; gangs pay small side amounts. Dealer keeps the seat on a win,
otherwise it rotates. Match = up to MAX_HANDS hands; most points wins.

Card/tile counting is the point: for each tile type the AI gets
live_tiles = 4 - (own hand) - (discards + open melds).
"""

import random
from functools import lru_cache

NUM_SEATS = 4
COPIES = 4
TILE_TYPES = 27
WALL_SIZE = TILE_TYPES * COPIES
MAX_HANDS = 8
START_POINTS = 0


def suit(t):
    return "mps"[t // 9]


def tile_label(t):
    return "万筒条"[t // 9] + str(t % 9 + 1)


def new_wall(rng):
    w = [t for t in range(TILE_TYPES) for _ in range(COPIES)]
    rng.shuffle(w)
    return w


# ---------- win / shanten ----------

def _is_win(counts):
    """counts: tuple of 27 counts (any total). Standard 4x3+2 shape."""
    counts = list(counts)
    for i in range(TILE_TYPES):
        if counts[i] >= 2:
            counts[i] -= 2
            if _all_melds(counts):
                return True
            counts[i] += 2
    return False


def _all_melds(counts):
    for i in range(TILE_TYPES):
        c = counts[i]
        if c == 0:
            continue
        if c >= 3:
            counts[i] -= 3
            if _all_melds(counts):
                counts[i] += 3
                return True
            counts[i] += 3
        if i % 9 <= 6 and counts[i] and counts[i + 1] and counts[i + 2]:
            counts[i] -= 1
            counts[i + 1] -= 1
            counts[i + 2] -= 1
            if _all_melds(counts):
                counts[i] += 1
                counts[i + 1] += 1
                counts[i + 2] += 1
                return True
            counts[i] += 1
            counts[i + 1] += 1
            counts[i + 2] += 1
        return False
    return True


def _shanten_rec(counts, i, melds, pairs, taatsu, memo):
    """Standard recursive shanten for 4m+1p target. counts is mutable list."""
    key = (tuple(counts), melds, pairs, taatsu)
    if key in memo:
        return memo[key]
    while i < TILE_TYPES and counts[i] == 0:
        i += 1
    if i >= TILE_TYPES:
        t = min(taatsu, 4 - melds)
        s = 8 - 2 * melds - t - pairs
        memo[key] = s
        return s
    best = 8
    c = counts
    # triplet
    if c[i] >= 3:
        c[i] -= 3
        best = min(best, _shanten_rec(c, i, melds + 1, pairs, taatsu, memo))
        c[i] += 3
    # sequence
    if i % 9 <= 6 and c[i + 1] and c[i + 2]:
        c[i] -= 1; c[i + 1] -= 1; c[i + 2] -= 1
        best = min(best, _shanten_rec(c, i, melds + 1, pairs, taatsu, memo))
        c[i] += 1; c[i + 1] += 1; c[i + 2] += 1
    # pair (for the head)
    if c[i] >= 2 and not pairs:
        c[i] -= 2
        best = min(best, _shanten_rec(c, i, melds, 1, taatsu, memo))
        c[i] += 2
    # taatsu: adjacent
    if i % 9 <= 7 and c[i + 1]:
        c[i] -= 1; c[i + 1] -= 1
        best = min(best, _shanten_rec(c, i, melds, pairs, taatsu + 1, memo))
        c[i] += 1; c[i + 1] += 1
    # taatsu: kanchan
    if i % 9 <= 6 and c[i + 2]:
        c[i] -= 1; c[i + 2] -= 1
        best = min(best, _shanten_rec(c, i, melds, pairs, taatsu + 1, memo))
        c[i] += 1; c[i + 2] += 1
    # discard this tile type entirely
    c[i] -= 1
    best = min(best, _shanten_rec(c, i, melds, pairs, taatsu, memo))
    c[i] += 1
    memo[key] = best
    return best


def shanten(tiles):
    """tiles: list of tile indices (13 or fewer considered — pass concealed
    tiles only, open melds are already complete)."""
    counts = [0] * TILE_TYPES
    for t in tiles:
        counts[t] += 1
    return _shanten_rec(counts, 0, 0, 0, 0, {})


def is_winning(tiles):
    counts = [0] * TILE_TYPES
    for t in tiles:
        counts[t] += 1
    return _is_win(counts)


def wait_tiles(concealed, open_meld_count):
    """Which tiles complete the hand (concealed + open melds)."""
    if len(concealed) + 3 * open_meld_count != 13:
        return []
    out = []
    for t in range(TILE_TYPES):
        if is_winning(concealed + [t]):
            out.append(t)
    return out


# ---------- game engine ----------

class Meld:
    def __init__(self, kind, tiles, source=None):
        self.kind = kind          # peng | gang | jia | angang
        self.tiles = list(tiles)
        self.source = source      # seat that discarded into it (None=concealed)

    def public_tiles(self):
        return list(self.tiles)


class Seat:
    def __init__(self):
        self.concealed = []       # own tiles (secret)
        self.melds = []           # open/declared melds
        self.discards = []        # tiles this seat has discarded
        self.points = START_POINTS
        self.gang_pending = False # drew supplement, must discard


class Game:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.seats = [Seat() for _ in range(NUM_SEATS)]
        self.dealer = 0
        self.hand_no = 0
        self.winner = None
        self.last_result = None
        self.phase = "playing"    # playing | handover | over
        self.hand_over_at = 0.0
        self.history = []
        self._start_hand()

    # ---------- hand lifecycle ----------

    def _start_hand(self):
        self.hand_no += 1
        if self.hand_no > MAX_HANDS:
            self.phase = "over"
            return
        self.phase = "playing"
        self.wall = new_wall(self.rng)
        self.turn = self.dealer
        self.turn_phase = "action"          # action | claim
        self.pending_tile = None            # last discard being claimed
        self.pending_from = None
        self.claim_queue = []               # [(priority, seat)]
        self.last_drawn = None              # tile just drawn by turn seat
        self.winner_last = None             # hu winner of this hand
        for s in self.seats:
            s.concealed, s.melds, s.discards = [], [], []
            s.gang_pending = False
        for _ in range(13):
            for s in self.seats:
                s.concealed.append(self.wall.pop())
        for s in self.seats:
            s.concealed.sort()
        self.last_drawn = self.wall.pop()
        self.seats[self.turn].concealed.append(self.last_drawn)
        self.history.append({"kind": "deal", "dealer": self.dealer,
                             "hand_no": self.hand_no})

    # ---------- visibility helpers ----------

    def visible_counts(self, seat):
        """Tiles visible to `seat` outside own concealed hand:
        all discards + all meld tiles + other seats' open tiles."""
        counts = [0] * TILE_TYPES
        for i, s in enumerate(self.seats):
            for t in s.discards:
                counts[t] += 1
            for m in s.melds:
                for t in m.public_tiles():
                    counts[t] += 1
        return counts

    def live_tile_counts(self, seat):
        """Remaining unknown copies per tile type = 4 - own_concealed - visible."""
        vis = self.visible_counts(seat)
        out = {}
        for t in range(TILE_TYPES):
            own = self.seats[seat].concealed.count(t)
            left = COPIES - own - vis[t]
            out[tile_label(t)] = left
        return out

    def meld_count(self, seat):
        return len(self.seats[seat].melds)

    # ---------- legal actions (action phase: after own draw) ----------

    def legal_actions(self, seat):
        if self.phase != "playing" or self.turn != seat \
                or self.turn_phase != "action":
            return []
        s = self.seats[seat]
        acts = [f"discard:{t}" for t in sorted(set(s.concealed))]
        if is_winning(s.concealed):
            acts.append("hu:self")
        for t in sorted(set(s.concealed)):
            if s.concealed.count(t) == 4:
                acts.append(f"gang:an:{t}")
        for m in s.melds:
            if m.kind == "peng":
                t = m.tiles[0]
                if t in s.concealed:
                    acts.append(f"gang:jia:{t}")
        return acts

    def legal_claims(self, seat):
        if self.phase != "playing" or self.turn_phase != "claim":
            return []
        if not any(s == seat for _, s in self.claim_queue):
            return []
        t = self.pending_tile
        s = self.seats[seat]
        out = ["pass"]
        if is_winning(s.concealed + [t]):
            out.append("hu")
        if s.concealed.count(t) >= 3:
            out.append("gang")
        if s.concealed.count(t) >= 2:
            out.append("peng")
        return out

    # ---------- applying ----------

    def apply_action(self, seat, action):
        if action not in self.legal_actions(seat):
            raise ValueError(f"illegal action {action}")
        s = self.seats[seat]
        if action == "hu:self":
            self._win(seat, seat, self_drawn=True)
            return
        if action.startswith("gang:"):
            kind, t = action.split(":")[1], int(action.split(":")[2])
            if kind == "an":
                for _ in range(4):
                    s.concealed.remove(t)
                s.melds.append(Meld("angang", [t] * 4))
                for o in range(NUM_SEATS):
                    if o != seat:
                        self.seats[o].points -= 200
                s.points += 200 * (NUM_SEATS - 1)
                self.history.append({"kind": "angang", "seat": seat, "tile": t})
            else:  # jia
                s.concealed.remove(t)
                for m in s.melds:
                    if m.kind == "peng" and m.tiles[0] == t:
                        m.kind = "jia"
                        m.tiles.append(t)
                        break
                for o in range(NUM_SEATS):
                    if o != seat:
                        self.seats[o].points -= 200
                s.points += 200 * (NUM_SEATS - 1)
                self.history.append({"kind": "jia", "seat": seat, "tile": t})
            self._draw_supplement(seat)
            return
        # discard
        t = int(action.split(":")[1])
        s.concealed.remove(t)
        s.discards.append(t)
        s.discards.sort()
        self.last_drawn = None
        self.history.append({"kind": "discard", "seat": seat, "tile": t})
        self._open_claim_window(seat, t)

    def apply_claim(self, seat, action):
        legal = self.legal_claims(seat)
        if not legal:
            raise ValueError("no claim pending for seat")
        if action not in legal:
            raise ValueError(f"illegal claim {action}")
        if action != "pass":
            self.claim_queue = [x for x in self.claim_queue if x[1] != seat]
            if action == "hu":
                self._win(seat, self.pending_from, self_drawn=False)
                return
            s = self.seats[seat]
            t = self.pending_tile
            self.seats[self.pending_from].discards.remove(t)
            if action == "peng":
                for _ in range(2):
                    s.concealed.remove(t)
                s.melds.append(Meld("peng", [t] * 3, self.pending_from))
                self.history.append({"kind": "peng", "seat": seat, "tile": t,
                                     "from": self.pending_from})
                self.turn = seat
                self.turn_phase = "action"
                self.pending_tile = None
                self.last_drawn = None
                return
            if action == "gang":
                for _ in range(3):
                    s.concealed.remove(t)
                s.melds.append(Meld("gang", [t] * 4, self.pending_from))
                self.seats[self.pending_from].points -= 400
                s.points += 400
                self.history.append({"kind": "gang", "seat": seat, "tile": t,
                                     "from": self.pending_from})
                self.turn = seat
                self.pending_tile = None
                self._draw_supplement(seat)
                return
        # pass -> advance queue
        self.claim_queue = [x for x in self.claim_queue if x[1] != seat]
        if not self.claim_queue:
            self._next_draw()

    def _draw_supplement(self, seat):
        if not self.wall:
            self._draw_out()
            return
        t = self.wall.pop()
        s = self.seats[seat]
        s.concealed.append(t)
        s.concealed.sort()
        self.last_drawn = t
        self.turn = seat
        self.turn_phase = "action"
        self.history.append({"kind": "supplement", "seat": seat})

    def _open_claim_window(self, discarder, t):
        queue = []
        for prio, kinds in ((0, ("hu",)), (1, ("gang",)), (2, ("peng",))):
            for d in range(1, NUM_SEATS):
                seat = (discarder + d) % NUM_SEATS
                s = self.seats[seat]
                if "hu" in kinds and is_winning(s.concealed + [t]):
                    queue.append((prio, seat))
                elif "gang" in kinds and s.concealed.count(t) >= 3:
                    queue.append((prio, seat))
                elif "peng" in kinds and s.concealed.count(t) >= 2:
                    queue.append((prio, seat))
        self.claim_queue = sorted(queue)
        if self.claim_queue:
            self.turn_phase = "claim"
            self.pending_tile = t
            self.pending_from = discarder
        else:
            self._next_draw()

    def _next_draw(self):
        self.pending_tile = None
        self.turn = (self.turn + 1) % NUM_SEATS
        if not self.wall:
            self._draw_out()
            return
        t = self.wall.pop()
        s = self.seats[self.turn]
        s.concealed.append(t)
        s.concealed.sort()
        self.last_drawn = t
        self.turn_phase = "action"
        self.history.append({"kind": "draw", "seat": self.turn})

    def _win(self, winner, source, self_drawn):
        s = self.seats[winner]
        if self_drawn:
            for o in range(NUM_SEATS):
                if o != winner:
                    self.seats[o].points -= 400
            s.points += 400 * (NUM_SEATS - 1)
            how = "自摸"
        else:
            self.seats[source].points -= 800
            s.points += 800
            how = f"seat{source}点炮"
        self.last_result = {"type": "hu", "winner": winner, "how": how,
                            "self_drawn": self_drawn,
                            "points": [x.points for x in self.seats],
                            "hands": {str(i): list(self.seats[i].concealed)
                                      for i in range(NUM_SEATS)}}
        self.history.append({"kind": "hu", "seat": winner, "how": how})
        self.phase = "handover"
        self.dealer = winner if self_drawn else winner  # winner deals next
        self.winner_last = winner

    def _draw_out(self):
        self.last_result = {"type": "draw",
                            "points": [x.points for x in self.seats],
                            "hands": {str(i): list(self.seats[i].concealed)
                                      for i in range(NUM_SEATS)},
                            "shanten": [shanten(self.seats[i].concealed)
                                        for i in range(NUM_SEATS)]}
        self.history.append({"kind": "draw"})
        self.phase = "handover"
        self.winner_last = None

    def next_hand(self):
        if self.phase == "handover":
            if self.winner_last is not None:
                self.dealer = self.winner_last
            else:
                self.dealer = (self.dealer + 1) % NUM_SEATS
            self._start_hand()

    @property
    def over(self):
        return self.phase == "over"

    def match_winner(self):
        if self.phase != "over":
            return None
        return max(range(NUM_SEATS), key=lambda i: self.seats[i].points)


ACT_CN = {"hu:self": "自摸胡牌", "hu": "胡牌", "gang": "明杠",
          "peng": "碰", "pass": "过"}

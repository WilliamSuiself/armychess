"""Chinese Checkers (跳棋) rules engine.

121-hole star board = hexagon of radius 4 (61 holes) + six 10-hole corner
triangles (arms). Axial (q, r) coordinates; pointy-top screen projection
x = sqrt3*(q + r/2), y = 1.5*r (y grows downward).

Player seats: "red" (seat 0) starts in the bottom arm (apex at screen
bottom-center), "blue" (seat 1) in the opposite top arm. Each races to move
all 10 pegs into the opposite arm. No captures — jumped pegs stay.

A move is a *path* [start, landing, ...]:
 - len==2 with adjacent endpoints -> a single STEP;
 - every hop clears exactly one occupied hole and lands on the empty hole
   directly beyond it -> a JUMP CHAIN.
Steps and jumps never mix in one turn; no hole is visited twice per turn.
"""
import copy

DIRS = [(1, 0), (1, -1), (0, -1), (-1, 0), (-1, 1), (0, 1)]

PLAYERS = ["red", "blue"]
CN = {"red": "红方", "blue": "蓝方"}


def _rot60(cell, k):
    """Rotate an axial cell by k*60 degrees."""
    q, r = cell
    for _ in range(k % 6):
        q, r = -r, q + r
    return (q, r)


# The bottom arm (apex at screen bottom-center), derived once:
#   apex (-4, 8); cells = apex + a*(0,-1) + b*(1,-1), a+b <= 3.
_BASE_ARM = [(-4 + b, 8 - a - b) for s in range(4) for a in range(s + 1)
             for b in [s - a]]

ARMS = [sorted(_rot60(c, k) for c in _BASE_ARM) for k in range(6)]

HEXAGON = [(q, r) for q in range(-4, 5) for r in range(max(-4, -q - 4),
                                                       min(4, -q + 4) + 1)]

HOLES = sorted(HEXAGON + [c for arm in ARMS for c in arm])
HOLE_SET = set(HOLES)
INDEX = {h: i for i, h in enumerate(HOLES)}

assert len(HOLES) == 121, f"board has {len(HOLES)} holes, want 121"
assert len(set(tuple(sorted(a)) for a in ARMS)) == 6, "arms overlap"

# red (seat 0) owns the arm whose apex has the largest r (screen bottom);
# blue owns the opposite arm.
RED_ARM = max(ARMS, key=lambda arm: sum(r for _, r in arm) / len(arm))
BLUE_ARM = sorted((-q, -r) for q, r in RED_ARM)

START_ZONE = {"red": RED_ARM, "blue": BLUE_ARM}


def target_zone(player):
    return START_ZONE[PLAYERS[1 - PLAYERS.index(player)]]


def adjacent(a, b):
    return (a[0] - b[0], a[1] - b[1]) in DIRS


def _neighbors(pos):
    return [(pos[0] + d[0], pos[1] + d[1]) for d in DIRS]


def label(pos):
    """Human-readable coordinate for prompts, e.g. (q=2,r=-1)."""
    return f"({pos[0]},{pos[1]})"


def path_label(path):
    return "->".join(label(p) for p in path)


class Game:
    def __init__(self):
        self.pegs = {"red": set(START_ZONE["red"]),
                     "blue": set(START_ZONE["blue"])}
        self.turn = 0                  # index into PLAYERS
        self.move_no = 0
        self.winner = None
        self.last_moves = []           # recent moves for AI context
        self.history = []

    # ---------------- queries ----------------
    def occupied(self):
        return self.pegs["red"] | self.pegs["blue"]

    def legal_moves(self, player):
        """All legal move paths: steps + every jump-chain destination."""
        occ = self.occupied()
        moves = []
        for peg in sorted(self.pegs[player]):
            for n in _neighbors(peg):
                if n in HOLE_SET and n not in occ:
                    moves.append([peg, n])
            for dest, path in self._jump_targets(peg, occ).items():
                moves.append(path)
        return moves

    def _jump_targets(self, start, occ):
        """BFS over jump chains from start. Returns {dest: full path}."""
        paths = {start: [start]}
        frontier = [start]
        while frontier:
            cur = frontier.pop(0)
            for d in DIRS:
                mid = (cur[0] + d[0], cur[1] + d[1])
                land = (cur[0] + 2 * d[0], cur[1] + 2 * d[1])
                if (mid in occ and land in HOLE_SET and land not in occ
                        and land not in paths):
                    paths[land] = paths[cur] + [land]
                    frontier.append(land)
        return {dest: p for dest, p in paths.items() if dest != start}

    def progress(self, player):
        """Sum of progress toward the target arm (bigger = closer to win)."""
        target = target_zone(player)
        # axis unit: from own arm apex toward target arm apex
        own = START_ZONE[player]
        ax = (sum(q for q, _ in target) - sum(q for q, _ in own),
              sum(r for _, r in target) - sum(r for _, r in own))
        n = max(abs(ax[0]) + abs(ax[1]), 1)
        total = 0
        for q, r in self.pegs[player]:
            total += (q * ax[0] + r * ax[1]) // 1
        return total / n

    def pegs_in(self, player, zone):
        return len(self.pegs[player] & set(zone))

    # ---------------- mutations ----------------
    def apply_move(self, player, path):
        if self.winner:
            raise ValueError("game over")
        if player != PLAYERS[self.turn]:
            raise ValueError(f"not {player}'s turn")
        path = [tuple(p) for p in path]
        if not path or path[0] not in self.pegs[player]:
            raise ValueError("start hole has no own peg")
        occ = self.occupied()
        occ.discard(path[0])
        for h in path:
            if h not in HOLE_SET:
                raise ValueError(f"hole {h} not on board")
        if len(path) == 2 and adjacent(path[0], path[1]):
            kind = "step"
        else:
            kind = "jump"
            cur = path[0]
            for k, nxt in enumerate(path[1:]):
                d = (nxt[0] - cur[0], nxt[1] - cur[1])
                if d not in [(2 * x, 2 * y) for x, y in DIRS]:
                    raise ValueError(f"illegal hop {cur}->{nxt}")
                mid = (cur[0] + d[0] // 2, cur[1] + d[1] // 2)
                if mid not in occ:
                    raise ValueError(f"no peg to jump over at {mid}")
                if nxt in occ or nxt in set(path[:k + 1]):
                    raise ValueError("landing occupied or hole revisited")
                cur = nxt
        if path[-1] in occ:
            raise ValueError("destination occupied")
        self.pegs[player].discard(path[0])
        self.pegs[player].add(path[-1])
        self.move_no += 1
        rec = {"player": player, "path": path, "kind": kind,
               "move_no": self.move_no}
        self.last_moves.append(rec)
        self.last_moves = self.last_moves[-12:]
        self.history.append(rec)
        if self.pegs[player] <= set(target_zone(player)):
            self.winner = player
        self.turn = 1 - self.turn

    def snapshot(self):
        return {p: sorted(self.pegs[p]) for p in PLAYERS}

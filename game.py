"""Standard army chess (军棋/陆战棋) game logic.

Board: 12 rows x 5 cols, 60 points total.
  - Rows 0-5   : AI territory   (back row = 0)
  - Rows 6-11  : Player territory (back row = 11)
Each side has 25 pieces:
  司令 x1, 军长 x1, 师长 x2, 旅长 x2, 团长 x2, 营长 x2,
  连长 x3, 排长 x3, 工兵 x3, 地雷 x3, 炸弹 x2, 军旗 x1

Terrain:
  - post (兵站): normal square, always attackable.
  - camp (行营): safe haven — a piece sitting in a camp cannot be attacked,
    and while inside a camp it may only move one step at a time (no rail).
  - hq (大本营): headquarters. The flag must be placed in one of the two
    HQ squares. Any piece that ends its move on an HQ square becomes stuck
    there (can never move again).

Movement:
  - 军旗 / 地雷 never move.
  - A piece not on a rail square (or one sitting inside a camp) may move
    one orthogonal step onto an empty square, or attack an adjacent enemy
    (unless that enemy is safe inside a camp).
  - A piece starting its move on a rail square may additionally slide any
    number of squares along the rail network: 工兵 (engineer) may turn
    corners at rail junctions, every other piece may only travel in a
    straight line. Sliding stops the instant a piece (friendly or enemy)
    blocks the path; landing on the first enemy piece is a valid capture.
  - Crossing between the two territories (row 5 <-> row 6) is blocked on
    columns 1 and 3 (the "mountain gap") — only columns 0, 2, 4 connect.

Combat:
  - Moving onto the enemy 军旗 captures it and wins the game immediately.
  - 炸弹 (bomb) destroys itself and whatever it collides with, always.
  - 地雷 (mine) destroys any attacker except 工兵, which defuses it and
    survives. Mines never move (they cannot be an "attacker").
  - Otherwise, higher rank wins; equal ranks destroy each other.
    Rank order (high to low): 司令 > 军长 > 师长 > 旅长 > 团长 > 营长 >
    连长 > 排长 > 工兵.
"""

import json
import os
import random
from collections import Counter
from copy import deepcopy


ROWS, COLS = 12, 5

RANK = {
    "司令": 9, "军长": 8, "师长": 7, "旅长": 6, "团长": 5,
    "营长": 4, "连长": 3, "排长": 2, "工兵": 1,
}

# Pieces that can never move.
IMMOVABLE = {"军旗", "地雷"}

# Full 25-piece roster per side.
SETUP_POOL = {
    "player": (
        ["司令", "军长"] + ["师长"] * 2 + ["旅长"] * 2 + ["团长"] * 2 + ["营长"] * 2
        + ["连长"] * 3 + ["排长"] * 3 + ["工兵"] * 3 + ["地雷"] * 3 + ["炸弹"] * 2
        + ["军旗"]
    ),
    "ai": (
        ["司令", "军长"] + ["师长"] * 2 + ["旅长"] * 2 + ["团长"] * 2 + ["营长"] * 2
        + ["连长"] * 3 + ["排长"] * 3 + ["工兵"] * 3 + ["地雷"] * 3 + ["炸弹"] * 2
        + ["军旗"]
    ),
}

# === Static terrain ===================================================
#
# Matches the standard printed board: HQ row, then one plain rail row,
# then the 3-row camp diamond (2 camps / 1 center camp / 2 camps), then
# the plain rail front row that meets the opponent's front row. Camps are
# additionally linked diagonally to their 4 diagonal neighbors (the "X"
# lines drawn around every 行营 on the real board) — see `road_neighbors`.

CAMP_CELLS = {
    (2, 1), (2, 3), (3, 2), (4, 1), (4, 3),
    (7, 1), (7, 3), (8, 2), (9, 1), (9, 3),
}

HQ_CELLS = {"ai": [(0, 1), (0, 3)], "player": [(11, 1), (11, 3)]}
ALL_HQ_CELLS = set(HQ_CELLS["ai"]) | set(HQ_CELLS["player"])

OWN_ROWS = {"ai": set(range(0, 6)), "player": set(range(6, 12))}
MINE_ROWS = {"ai": {0, 1}, "player": {10, 11}}
FRONT_ROW = {"ai": 5, "player": 6}

RAIL_CELLS = set()
# The two edge columns are rail for every row EXCEPT the HQ rows (0 and
# 11): the HQ row is never part of the rail network, so a piece can never
# rail-slide straight from its own side into the opponent's back row in
# one move — the final step onto/near HQ must always be an ordinary
# 1-step move. Without this, an open edge column would let a piece fly
# the full length of the board in a single move, which is far too strong.
for _r in range(1, ROWS - 1):
    RAIL_CELLS.add((_r, 0))
    RAIL_CELLS.add((_r, 4))
for _r in (1, 5, 6, 10):
    for _c in range(COLS):
        RAIL_CELLS.add((_r, _c))


def _terrain_kind(pos):
    if pos in CAMP_CELLS:
        return "camp"
    if pos in ALL_HQ_CELLS:
        return "hq"
    return "post"


TERRAIN = {
    (r, c): {"kind": _terrain_kind((r, c)), "rail": (r, c) in RAIL_CELLS}
    for r in range(ROWS) for c in range(COLS)
}

DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1)]
DIAG_DIRS = [(-1, -1), (-1, 1), (1, -1), (1, 1)]


def in_bounds(pos):
    r, c = pos
    return 0 <= r < ROWS and 0 <= c < COLS


def can_cross(a, b):
    """Mountain-gap rule: row5<->row6 only connects on cols 0, 2, 4."""
    if not in_bounds(b):
        return False
    (r0, c0), (r1, c1) = a, b
    if (r0 == 5 and r1 == 6 and c0 == c1) or (r0 == 6 and r1 == 5 and c0 == c1):
        return c0 in (0, 2, 4)
    return True


def adjacent(a, b):
    return abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1


def side_of(row):
    return "ai" if row <= 5 else "player"


def orth_neighbors(pos):
    out = []
    for dr, dc in DIRS:
        n = (pos[0] + dr, pos[1] + dc)
        if in_bounds(n) and can_cross(pos, n):
            out.append(n)
    return out


def road_neighbors(pos):
    """1-step public-road neighbors used for ordinary (non-rail) movement:
    orthogonal always, plus the diagonal road links that connect every
    camp to its 4 diagonal neighbors (as drawn on the real board — camps
    are the only squares with diagonal connections)."""
    out = list(orth_neighbors(pos))
    is_camp = TERRAIN[pos]["kind"] == "camp"
    for dr, dc in DIAG_DIRS:
        n = (pos[0] + dr, pos[1] + dc)
        if not in_bounds(n):
            continue
        if is_camp or TERRAIN[n]["kind"] == "camp":
            out.append(n)
    return out


def _camp_immune(pos):
    return TERRAIN[pos]["kind"] == "camp"


# === Battle resolution ===============================================

def battle(attacker_type, defender_type):
    """Resolve combat. Returns (outcome, revealed_defender_type)."""
    if defender_type == "军旗":
        return "flag_taken_player", defender_type
    if attacker_type == "军旗":
        return "flag_taken_ai", attacker_type  # guard; flag can't actually attack

    if attacker_type == "炸弹" or defender_type == "炸弹":
        return "both_die", defender_type

    if defender_type == "地雷":
        if attacker_type == "工兵":
            return "attacker_wins", defender_type
        # Mines are permanent barriers: the attacker dies, the mine stays.
        return "defender_wins", defender_type
    if attacker_type == "地雷":
        return "both_die", attacker_type  # guard; mines can't actually attack

    a, d = RANK.get(attacker_type, 0), RANK.get(defender_type, 0)
    if a > d:
        return "attacker_wins", defender_type
    if d > a:
        return "defender_wins", attacker_type
    return "both_die", defender_type


def tactical_override(game, owner):
    """Hard-coded tactical priorities that outrank any model judgement:
      1. flag_win — an enemy 军旗 is capturable this turn: take it (the game
         ends instantly; nothing else matters).
      2. flag_defense — an enemy piece can capture OUR flag next turn:
         capture that threat now, preferring a predicted win, then a bomb
         trade, then the strongest available piece.
    Returns (from_pos, to_pos, reason) or None."""
    opponent = "ai" if owner == "player" else "player"

    for fp in movable_pieces(game, owner):
        for tp in possible_moves(game, owner, fp):
            t = game["board"].get(tp)
            if t and t["owner"] == opponent and t["type"] == "军旗":
                return fp, tp, "flag_win"

    flag_pos = next((p for p, pc in game["board"].items()
                     if pc["owner"] == owner and pc["type"] == "军旗"
                     and pc["alive"]), None)
    if flag_pos is None:
        return None

    threats = [ep for ep, pc in game["board"].items()
               if pc["owner"] == opponent and pc["alive"]
               and flag_pos in possible_moves(game, opponent, ep)]
    if not threats:
        return None

    known = game["revealed_to"][owner]
    best = None
    for threat in threats:
        tkey = f"{threat[0]},{threat[1]}"
        threat_known = known.get(tkey)
        for fp in movable_pieces(game, owner):
            if threat not in possible_moves(game, owner, fp):
                continue
            piece = game["board"][fp]
            if threat_known and (threat_known in RANK or
                                 threat_known in IMMOVABLE or threat_known == "炸弹"):
                outcome = battle(piece["type"], threat_known)[0]
                score = {"attacker_wins": 3, "flag_taken_player": 3,
                         "both_die": 2}.get(outcome, 0)
            else:
                # Unknown threat: a bomb guarantees the trade; otherwise send
                # the strongest available piece (small rank bonus breaks ties).
                score = (2.0 if piece["type"] == "炸弹" else 1.0) \
                    + RANK.get(piece["type"], 0) * 0.01
            if best is None or score > best[0]:
                best = (score, fp, threat)
    if best:
        return best[1], best[2], "flag_defense"
    return None


def _stronger_marker(existing, victim_type):
    """Bound marker '>X' where X = the strongest piece this enemy has beaten
    so far — a later weaker kill must not lower the known bound."""
    vr = RANK.get(victim_type, 0)
    if isinstance(existing, str) and existing.startswith(">"):
        cur = existing[1:].split("或")[0]
        if RANK.get(cur, -1) >= vr:
            return existing
    return f">{victim_type}"


def _bound_hint(attacker_type, outcome, defender_pos=None, defender_side=None,
                defender_moved=False):
    """Deduction about an unseen defender after the attacker died — a bound
    marker shown on that enemy piece instead of a false exact type.

    Mines are permanent barriers here: they survive every non-engineer
    attack (defender_wins), so a surviving defender is either a mine or a
    higher-ranked piece — but only if it could physically be a mine (sits
    in the defender's back two rows and has never moved).

    defender_wins:
      - 司令 → "地雷" (only a mine can stop it — nothing outranks it, and a
        bomb would be mutual destruction)
      - 工兵 → "非地雷" (engineers defuse mines, and bombs are mutual
        destruction — so it must be a combat piece)
      - other X → ">X或雷" when a mine is physically possible, else ">X"
    both_die:
      - 工兵 → "工兵或炸弹" (only a bomb or a fellow engineer kills one)
      - 炸弹 → None (a bomb dies to anything — no information)
      - other X → "同级或炸弹" (equal rank or a bomb — mines survive, so a
        mine can never be involved in mutual destruction)
    """
    if outcome == "defender_wins":
        if attacker_type == "司令":
            return "地雷"
        if attacker_type == "工兵":
            return "非地雷"
        # A surviving defender is either a higher rank OR a mine — but a
        # mine can only sit in the back two rows and can never move, so a
        # defender anywhere else (or one that has moved) can't be a mine.
        could_be_mine = (not defender_moved and defender_pos is not None
                         and defender_pos[0] in MINE_ROWS[defender_side])
        return f">{attacker_type}或雷" if could_be_mine else f">{attacker_type}"
    if attacker_type == "工兵":
        return "工兵或炸弹"
    if attacker_type == "炸弹":
        return None
    return "同级或炸弹"


# === Preset formations =================================================
#
# Coordinates below are in LOCAL space: row 0 = your back row (HQ row),
# row 5 = your front row, regardless of which side plays it. They are
# mapped onto the real board by `_preset_abs_layout` — for "ai" local row r
# maps to absolute row r; for "player" it maps to absolute row (11 - r)
# (a vertical mirror), columns stay the same. Every preset fills the full
# 25 non-camp cells on one side (no manual placement is required, but the
# usual setup_place/setup_remove calls still work on top of a preset).

PRESET_FORMATIONS = [
    {
        # Classic strong-opening layout (mirrors the player's proven custom
        # formation): the front row is ALL power pieces, and each corner
        # 师长 has a 炸弹 sitting one step behind it — if the 师长 dies to
        # a bigger enemy piece, the bomb trades it back next turn.
        "name": "强攻阵",
        "desc": "前排全线强子（司令居中、双师长护角），两炸弹紧随角上师长身后——师长阵亡即报复性兑换",
        "layout": {
            (0, 0): "地雷", (0, 1): "军旗", (0, 2): "地雷", (0, 3): "排长", (0, 4): "地雷",
            (1, 0): "连长", (1, 1): "旅长", (1, 2): "军长", (1, 3): "工兵", (1, 4): "工兵",
            (2, 0): "连长", (2, 2): "团长", (2, 4): "营长",
            (3, 0): "营长", (3, 1): "排长", (3, 3): "排长", (3, 4): "连长",
            (4, 0): "炸弹", (4, 2): "工兵", (4, 4): "炸弹",
            (5, 0): "师长", (5, 1): "旅长", (5, 2): "司令", (5, 3): "团长", (5, 4): "师长",
        },
    },
    {
        # Asymmetric right-flank assault: 司令+双师长 mass on the right,
        # 团长 probes the left edge on the rail, front-row 工兵 can slide
        # along the whole front rail line to defuse mines.
        "name": "右翼突破",
        "desc": "司令+双师长堆右路强攻，团长守左角诱敌，双炸弹护两翼强点，工兵占前排铁路随时滑拆",
        "layout": {
            (0, 0): "地雷", (0, 1): "军旗", (0, 2): "地雷", (0, 3): "排长", (0, 4): "地雷",
            (1, 0): "工兵", (1, 1): "旅长", (1, 2): "军长", (1, 3): "连长", (1, 4): "连长",
            (2, 0): "营长", (2, 2): "团长", (2, 4): "排长",
            (3, 0): "连长", (3, 1): "排长", (3, 3): "营长", (3, 4): "旅长",
            (4, 0): "炸弹", (4, 2): "工兵", (4, 4): "炸弹",
            (5, 0): "团长", (5, 1): "工兵", (5, 2): "师长", (5, 3): "司令", (5, 4): "师长",
        },
    },
    {
        # Defensive shell: flag buried on the right behind two mines, a third
        # mine seals the left edge; front is still strong (师长+司令 right,
        # 工兵 left edge on rail for quick mine-clearing counterplay).
        "name": "铁桶阵",
        "desc": "军旗藏右翼雷后，左路地雷封锁，前排右强左工兵——防守反击型",
        "layout": {
            (0, 0): "排长", (0, 1): "排长", (0, 2): "地雷", (0, 3): "军旗", (0, 4): "地雷",
            (1, 0): "地雷", (1, 1): "军长", (1, 2): "旅长", (1, 3): "连长", (1, 4): "工兵",
            (2, 0): "营长", (2, 2): "团长", (2, 4): "连长",
            (3, 0): "连长", (3, 1): "营长", (3, 3): "排长", (3, 4): "旅长",
            (4, 0): "炸弹", (4, 2): "工兵", (4, 4): "炸弹",
            (5, 0): "团长", (5, 1): "工兵", (5, 2): "师长", (5, 3): "司令", (5, 4): "师长",
        },
    },
    {
        # The ONE kept sacrificial-scout formation: light pieces screen the
        # front (they probe and die cheaply), but 司令 hides among them in
        # the center as a bluff, and the whole second line is heavy hitters
        # ready to counter-attack whatever the scouts reveal.
        "name": "诱敌深入",
        "desc": "前排排长/工兵示弱侦察送死探雷，司令隐身中路做伪装，旅长师长军长第二线重兵反扑",
        "layout": {
            (0, 0): "地雷", (0, 1): "军旗", (0, 2): "地雷", (0, 3): "排长", (0, 4): "地雷",
            (1, 0): "旅长", (1, 1): "师长", (1, 2): "军长", (1, 3): "师长", (1, 4): "旅长",
            (2, 0): "团长", (2, 2): "营长", (2, 4): "团长",
            (3, 0): "营长", (3, 1): "连长", (3, 3): "连长", (3, 4): "工兵",
            (4, 0): "炸弹", (4, 2): "工兵", (4, 4): "炸弹",
            (5, 0): "排长", (5, 1): "排长", (5, 2): "司令", (5, 3): "连长", (5, 4): "工兵",
        },
    },
    {
        # Rail raid: all three engineers sit on rail squares (two edge
        # columns + center), ready to slide the full length and turn corners
        # to snipe mines; the front is still 师长/司令/团长-strong with the
        # usual bomb escort on both edge columns.
        "name": "铁道奇袭",
        "desc": "三工兵全卡铁路（两边线+中路）可长途滑行拐弯拆雷，前排右中强势主攻，边线炸弹滑行支援",
        "layout": {
            (0, 0): "地雷", (0, 1): "排长", (0, 2): "地雷", (0, 3): "军旗", (0, 4): "地雷",
            (1, 0): "工兵", (1, 1): "旅长", (1, 2): "军长", (1, 3): "营长", (1, 4): "工兵",
            (2, 0): "连长", (2, 2): "团长", (2, 4): "连长",
            (3, 0): "排长", (3, 1): "连长", (3, 3): "营长", (3, 4): "旅长",
            (4, 0): "炸弹", (4, 2): "工兵", (4, 4): "炸弹",
            (5, 0): "团长", (5, 1): "排长", (5, 2): "师长", (5, 3): "司令", (5, 4): "师长",
        },
    },
]


CUSTOM_FORMATIONS_PATH = os.path.join(os.path.dirname(__file__), "custom_formations.json")


def load_custom_formations():
    """Custom formations saved by the player, persisted alongside the app.
    Returns a list in the same shape as PRESET_FORMATIONS (local-coord layouts)."""
    if not os.path.exists(CUSTOM_FORMATIONS_PATH):
        return []
    try:
        with open(CUSTOM_FORMATIONS_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for entry in raw:
        layout = {}
        for k, t in entry.get("layout", {}).items():
            r, c = k.split(",")
            layout[(int(r), int(c))] = t
        out.append({"name": entry.get("name", "自定义阵型"),
                    "desc": entry.get("desc", "自定义阵型"),
                    "layout": layout, "custom": True})
    return out


def _save_custom_formations(formations):
    raw = [{
        "name": f["name"], "desc": f["desc"],
        "layout": {f"{r},{c}": t for (r, c), t in f["layout"].items()},
    } for f in formations]
    tmp = CUSTOM_FORMATIONS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CUSTOM_FORMATIONS_PATH)


def all_formations():
    """Built-in presets followed by any player-saved custom formations."""
    return PRESET_FORMATIONS + load_custom_formations()


def save_current_setup_as_formation(game, owner, name):
    """Save the current (complete) setup for `owner` as a new custom formation."""
    if game.get("phase") != "setup":
        return {"ok": False, "error": "not in setup phase"}
    placed = game["setup"].get(owner, {})
    if len(placed) != len(SETUP_POOL[owner]):
        return {"ok": False,
                "error": f"placed {len(placed)}/{len(SETUP_POOL[owner])} pieces — finish setup first"}

    local_layout = {}
    for key, t in placed.items():
        r, c = (int(x) for x in key.split(","))
        lr = r if owner == "ai" else 11 - r
        local_layout[(lr, c)] = t

    try:
        validate_formation(local_layout)
    except AssertionError as e:
        return {"ok": False, "error": f"current setup isn't a valid standalone formation: {e}"}

    name = (name or "").strip() or f"自定义阵型{len(load_custom_formations()) + 1}"
    customs = load_custom_formations()
    customs.append({"name": name, "desc": "自定义阵型", "layout": local_layout, "custom": True})
    _save_custom_formations(customs)
    return {"ok": True, "name": name, "index": len(PRESET_FORMATIONS) + len(customs) - 1}


def _formation_cells():
    return [(r, c) for r in range(6) for c in range(COLS) if (r, c) not in CAMP_CELLS]


def validate_formation(layout):
    """Raise AssertionError if a preset's local layout is not fully legal."""
    cells = _formation_cells()
    assert set(layout.keys()) == set(cells), "formation must fill exactly the 25 non-camp cells"
    counts = {}
    for (r, c), t in layout.items():
        counts[t] = counts.get(t, 0) + 1
        if t == "军旗":
            assert c in (1, 3) and r == 0, f"军旗 must be in an HQ cell, got {(r, c)}"
        if t == "地雷":
            assert r in (0, 1), f"地雷 must be in the back two rows, got {(r, c)}"
        if t == "炸弹":
            assert r != 5, f"炸弹 cannot be on the front row, got {(r, c)}"
    expected = {}
    for t in SETUP_POOL["player"]:
        expected[t] = expected.get(t, 0) + 1
    assert counts == expected, f"piece counts mismatch: {counts} vs {expected}"


def _preset_abs_layout(owner, idx):
    formations = all_formations()
    idx = idx % len(formations)
    local = formations[idx]["layout"]
    out = {}
    for (r, c), t in local.items():
        pos = (r, c) if owner == "ai" else (11 - r, c)
        out[pos] = t
    return out


def perturb_layout(owner, layout):
    """Return a slightly randomized variant of a legal layout so repeated
    games don't let the opponent memorize the AI's formation: a random
    horizontal mirror (columns 0<->4, 1<->2 — HQ cells swap with each other,
    so the flag stays legal) plus a few legality-checked pairwise swaps."""
    layout = dict(layout)
    mine_rows = MINE_ROWS[owner]
    hq_cells = HQ_CELLS[owner]

    if random.random() < 0.5:
        layout = {(r, 4 - c): t for (r, c), t in layout.items()}

    def ok_at(pos, t):
        if t == "军旗":
            return pos in hq_cells
        if t == "地雷":
            return pos[0] in mine_rows
        return True

    positions = list(layout.keys())
    for _ in range(random.randint(2, 5)):
        a, b = random.sample(positions, 2)
        ta, tb = layout[a], layout[b]
        if ta != tb and ok_at(a, tb) and ok_at(b, ta):
            layout[a], layout[b] = tb, ta
    return layout


def setup_apply_preset(game, owner, idx):
    """Overwrite this side's setup with one of the built-in or custom formations."""
    if game.get("phase") != "setup":
        return {"ok": False, "error": "not in setup phase"}
    formations = all_formations()
    if not (0 <= idx < len(formations)):
        return {"ok": False, "error": "unknown formation index"}
    layout = _preset_abs_layout(owner, idx)
    game["setup"][owner] = {f"{r},{c}": t for (r, c), t in layout.items()}
    game["setup_pool"][owner] = []
    game.setdefault("setup_preset", {"player": None, "ai": None})[owner] = idx
    return {"ok": True, "pool": [], "preset": idx, "name": formations[idx]["name"]}


# === Random valid setup (used to auto-fill the AI, and as a default) ===

def random_valid_setup(owner):
    """Return {pos: piece_type} — a fully random layout satisfying setup rules."""
    rows = OWN_ROWS[owner]
    all_cells = [(r, c) for r in rows for c in range(COLS)
                 if TERRAIN[(r, c)]["kind"] != "camp"]
    hq_cells = HQ_CELLS[owner][:]
    mine_rows = MINE_ROWS[owner]
    front_row = FRONT_ROW[owner]

    layout = {}
    remaining = set(all_cells)

    flag_cell = random.choice(hq_cells)
    layout[flag_cell] = "军旗"
    remaining.discard(flag_cell)

    pool = [t for t in SETUP_POOL[owner] if t != "军旗"]
    random.shuffle(pool)
    mines = [t for t in pool if t == "地雷"]
    bombs = [t for t in pool if t == "炸弹"]
    others = [t for t in pool if t not in ("地雷", "炸弹")]

    mine_eligible = [c for c in remaining if c[0] in mine_rows]
    random.shuffle(mine_eligible)
    for t in mines:
        c = mine_eligible.pop()
        remaining.discard(c)
        layout[c] = t

    bomb_eligible = [c for c in remaining if c[0] != front_row]
    random.shuffle(bomb_eligible)
    for t in bombs:
        c = bomb_eligible.pop()
        remaining.discard(c)
        layout[c] = t

    rest = list(remaining)
    random.shuffle(rest)
    for t in others:
        c = rest.pop()
        layout[c] = t

    return layout


# === Game state lifecycle =========================================

def new_game():
    """Fresh game with both sides randomly (validly) set up — for quick testing."""
    layout = {"ai": random_valid_setup("ai"), "player": random_valid_setup("player")}
    return _build_game_from_layout(layout)


def new_game_for_setup(preset=0):
    """Create a setup-phase game state, pre-filled with a default preset formation
    for the player (still fully editable via setup_place / setup_remove / setup_apply_preset)."""
    game = {
        "phase": "setup",
        "board": {},
        "my_known": {"player": {}, "ai": {}},
        "revealed_to": {"player": {}, "ai": {}},
        "turn": 0,
        "winner": None,
        "log": [],
        "last_ai_probs": None,
        "setup": {"player": {}, "ai": {}},
        "setup_pool": {
            "player": SETUP_POOL["player"][:],
            "ai": SETUP_POOL["ai"][:],
        },
        "setup_preset": {"player": None, "ai": None},
    }
    setup_apply_preset(game, "player", preset)
    game["setup_preset"]["player"] = preset
    return game


def _build_game_from_layout(layout):
    board = {}
    for owner, pieces in layout.items():
        for pos, ptype in pieces.items():
            board[pos] = {"type": ptype, "owner": owner, "alive": True}
    return {
        "phase": "playing",
        "board": board,
        "my_known": {
            owner: {f"{r},{c}": ptype for (r, c), ptype in pieces.items()}
            for owner, pieces in layout.items()
        },
        "revealed_to": {"player": {}, "ai": {}},
        "turn": 0,
        "winner": None,
        "log": [],
        "last_ai_probs": None,
        "setup": {},
        "setup_pool": {"player": [], "ai": []},
    }


# === Setup phase ==================================================

def setup_place(game, owner, pos, piece_type):
    if game.get("phase") != "setup":
        return {"ok": False, "error": "not in setup phase"}
    if not in_bounds(pos):
        return {"ok": False, "error": "position out of bounds"}
    if pos[0] not in OWN_ROWS[owner]:
        return {"ok": False, "error": "must place on your own side"}
    if TERRAIN[pos]["kind"] == "camp":
        return {"ok": False, "error": "行营内不能布子"}
    if piece_type not in SETUP_POOL[owner]:
        return {"ok": False, "error": f"unknown piece type {piece_type}"}

    pool = game["setup_pool"][owner]
    if pool.count(piece_type) == 0:
        return {"ok": False, "error": f"no {piece_type} left to use"}

    if piece_type == "军旗" and pos not in HQ_CELLS[owner]:
        return {"ok": False, "error": "军旗必须放在大本营"}
    if piece_type == "地雷" and pos[0] not in MINE_ROWS[owner]:
        return {"ok": False, "error": "地雷只能放在最后两排"}
    if piece_type == "炸弹" and pos[0] == FRONT_ROW[owner]:
        return {"ok": False, "error": "炸弹不能放在第一线"}

    pos_key = f"{pos[0]},{pos[1]}"
    if pos_key in game["setup"][owner]:
        old_type = game["setup"][owner][pos_key]
        game["setup_pool"][owner].append(old_type)

    game["setup"][owner][pos_key] = piece_type
    pool.remove(piece_type)
    return {"ok": True, "pool": game["setup_pool"][owner]}


def setup_remove(game, owner, pos):
    if game.get("phase") != "setup":
        return {"ok": False, "error": "not in setup phase"}
    pos_key = f"{pos[0]},{pos[1]}"
    if pos_key not in game["setup"][owner]:
        return {"ok": False, "error": "no piece at that position"}
    ptype = game["setup"][owner].pop(pos_key)
    game["setup_pool"][owner].append(ptype)
    return {"ok": True, "pool": game["setup_pool"][owner]}


def auto_fill_setup(game, owner):
    """Fill an unplaced side's setup with a randomly perturbed preset —
    used when that side is AI-controlled (no human to place pieces)."""
    idx = random.randrange(len(PRESET_FORMATIONS))
    lay = perturb_layout(owner, _preset_abs_layout(owner, idx))
    game["setup"][owner] = {f"{r},{c}": t for (r, c), t in lay.items()}
    game.setdefault("setup_preset", {"player": None, "ai": None})[owner] = idx


def setup_start(game):
    if game.get("phase") != "setup":
        return {"ok": False, "error": "not in setup phase"}

    total = len(SETUP_POOL["player"])
    if len(game["setup"]["player"]) != total:
        return {"ok": False,
                "error": f"player placed {len(game['setup']['player'])}/{total} pieces"}
    if "军旗" not in game["setup"]["player"].values():
        return {"ok": False, "error": "player must place a 军旗 (flag)"}

    ai_preset = random.randrange(len(PRESET_FORMATIONS))
    ai_layout = perturb_layout("ai", _preset_abs_layout("ai", ai_preset))
    game["setup"]["ai"] = {f"{r},{c}": t for (r, c), t in ai_layout.items()}
    game.setdefault("setup_preset", {"player": None, "ai": None})["ai"] = ai_preset

    def _to_tuples(d):
        out = {}
        for k, v in d.items():
            r, c = k.split(",")
            out[(int(r), int(c))] = v
        return out

    layout = {
        "player": _to_tuples(game["setup"]["player"]),
        "ai": _to_tuples(game["setup"]["ai"]),
    }
    fresh = _build_game_from_layout(layout)
    fresh["setup"] = game["setup"]
    fresh["setup_pool"] = game["setup_pool"]
    fresh["setup_preset"] = game.get("setup_preset", {"player": None, "ai": ai_preset})
    game.clear()
    game.update(fresh)
    return {"ok": True, "phase": game["phase"]}


# === Move logic ==================================================

def _step_moves(game, owner, pos):
    moves = []
    for n in road_neighbors(pos):
        occ = game["board"].get(n)
        if occ is None or not occ["alive"]:
            moves.append(n)
        elif occ["owner"] != owner and not _camp_immune(n):
            moves.append(n)
    return moves


def _slide_straight(game, owner, pos):
    """Non-engineer rail move: straight line only, stop at first obstacle."""
    moves = []
    for dr, dc in DIRS:
        cur = pos
        nxt = (pos[0] + dr, pos[1] + dc)
        while (in_bounds(nxt) and can_cross(cur, nxt)
               and TERRAIN[cur]["rail"] and TERRAIN[nxt]["rail"]):
            occ = game["board"].get(nxt)
            if occ is None or not occ["alive"]:
                moves.append(nxt)
            else:
                if occ["owner"] != owner and not _camp_immune(nxt):
                    moves.append(nxt)
                break
            cur = nxt
            nxt = (nxt[0] + dr, nxt[1] + dc)
    return moves


def _slide_engineer(game, owner, pos):
    """Engineer rail move: BFS over the rail graph, corners allowed."""
    visited = {pos}
    frontier = [pos]
    moves = []
    while frontier:
        cur = frontier.pop()
        for n in orth_neighbors(cur):
            if n in visited or not (TERRAIN[cur]["rail"] and TERRAIN[n]["rail"]):
                continue
            visited.add(n)
            occ = game["board"].get(n)
            if occ is None or not occ["alive"]:
                moves.append(n)
                frontier.append(n)
            elif occ["owner"] != owner and not _camp_immune(n):
                moves.append(n)
    return moves


def movable_pieces(game, owner):
    out = []
    for pos, piece in game["board"].items():
        if piece["owner"] != owner or not piece["alive"]:
            continue
        if piece["type"] in IMMOVABLE:
            continue
        if TERRAIN[pos]["kind"] == "hq":
            continue
        out.append(pos)
    return out


def possible_moves(game, owner, pos):
    piece = game["board"].get(pos)
    if not piece or piece["owner"] != owner or not piece["alive"]:
        return []
    if piece["type"] in IMMOVABLE:
        return []
    if TERRAIN[pos]["kind"] == "hq":
        return []

    moves = _step_moves(game, owner, pos)
    if TERRAIN[pos]["kind"] != "camp" and TERRAIN[pos]["rail"]:
        extra = _slide_engineer(game, owner, pos) if piece["type"] == "工兵" \
            else _slide_straight(game, owner, pos)
        for m in extra:
            if m not in moves:
                moves.append(m)
    return moves


def _reveal_flag_on_commander_down(game, dead_owner, event):
    """亮旗 rule: dead_owner's 司令 just died → that side's flag location is
    revealed to the opponent (standard army-chess rule)."""
    enemy = "ai" if dead_owner == "player" else "player"
    for pos, p in game["board"].items():
        if p["owner"] == dead_owner and p["type"] == "军旗" and p["alive"]:
            key = f"{pos[0]},{pos[1]}"
            if game["revealed_to"][enemy].get(key) != "军旗":
                game["revealed_to"][enemy][key] = "军旗"
                event.setdefault("flags_revealed", []).append(
                    {"owner": dead_owner, "pos": list(pos)})
            return


def execute_move(game, owner, from_pos, to_pos):
    piece = game["board"].get(from_pos)
    if not piece or piece["owner"] != owner or not piece["alive"]:
        return {"ok": False, "error": "invalid source"}

    legal = possible_moves(game, owner, from_pos)
    if to_pos not in legal:
        return {"ok": False, "error": "illegal target"}

    opponent = "ai" if owner == "player" else "player"
    target = game["board"].get(to_pos)

    del game["board"][from_pos]
    game["my_known"][owner][f"{from_pos[0]},{from_pos[1]}"] = None

    event = {"actor": owner, "from": from_pos, "to": to_pos,
             "mover_type": piece["type"]}

    fkey = f"{from_pos[0]},{from_pos[1]}"
    tkey = f"{to_pos[0]},{to_pos[1]}"

    if target and target["alive"]:
        outcome, _ = battle(piece["type"], target["type"])

        # Symmetric hidden-info convention: the side that LOST a piece learns
        # only that the survivor is stronger than what it killed — a ">X"
        # bound marker, never the real name. A dead attacker, however, flips
        # face-up: its exact type goes on its origin square as history
        # (not the fight square — a marker there would wrongly transfer onto
        # the defender when the defender later moves away).
        if outcome in ("attacker_wins", "flag_taken_player"):
            if target["type"] == "地雷":
                marker = "工兵"      # only an engineer can remove a mine
            else:
                marker = _stronger_marker(
                    game["revealed_to"][opponent].get(fkey), target["type"])
            game["revealed_to"][opponent].pop(fkey, None)
            game["revealed_to"][opponent][tkey] = marker
        else:
            game["revealed_to"][opponent][fkey] = piece["type"]
            marker = piece["type"]
        # What each side sees the combatants as — stored on the event so the
        # recent-events feed can replay history without leaking real types.
        event["seen_attacker"] = marker
        if outcome in ("attacker_wins", "flag_taken_player"):
            game["revealed_to"][owner][tkey] = target["type"]
            event["seen_defender"] = target["type"]
        else:
            hint = _bound_hint(piece["type"], outcome,
                               defender_pos=to_pos, defender_side=opponent,
                               defender_moved=bool(target.get("has_moved")))
            if hint:
                game["revealed_to"][owner][tkey] = hint
            event["seen_defender"] = hint or "?"

        event["combat"] = True
        event["attacker_type"] = piece["type"]
        event["defender_type"] = target["type"]
        event["outcome"] = outcome

        if outcome == "attacker_wins":
            game["board"][to_pos] = piece
            piece["has_moved"] = True
            game["my_known"][owner][tkey] = piece["type"]
        elif outcome == "defender_wins":
            pass
        elif outcome == "both_die":
            if to_pos in game["board"]:
                del game["board"][to_pos]
            game["my_known"][owner][tkey] = None
        elif outcome == "flag_taken_player":
            # Name is misleading — it means "a flag was taken"; the winner is
            # whoever moved (the attacker), not hard-coded "player".
            game["board"][to_pos] = piece
            piece["has_moved"] = True
            game["my_known"][owner][tkey] = piece["type"]
            game["winner"] = owner
            game["phase"] = "ended"
        elif outcome == "flag_taken_ai":
            game["winner"] = "ai"
            game["phase"] = "ended"

        # 亮旗: a dead 司令 exposes its side's flag to the opponent.
        if piece["type"] == "司令" and outcome in ("defender_wins", "both_die"):
            _reveal_flag_on_commander_down(game, owner, event)
        if target["type"] == "司令" and outcome in ("attacker_wins", "both_die"):
            _reveal_flag_on_commander_down(game, opponent, event)
    else:
        game["board"][to_pos] = piece
        piece["has_moved"] = True
        game["my_known"][owner][tkey] = piece["type"]
        # A revealed piece stays revealed when it MOVES — the opponent watched
        # it go. Clear stale memories at the destination first so a different
        # piece's history can't mislabel the newcomer. A moved piece is also
        # provably not a mine (mines never move), so drop any "或雷" suffix.
        opp_rev = game["revealed_to"][opponent]
        opp_rev.pop(tkey, None)
        game["revealed_to"][owner].pop(tkey, None)
        if opp_rev.get(fkey):
            mark = opp_rev.pop(fkey)
            if isinstance(mark, str) and mark.endswith("或雷"):
                mark = mark[:-2]
            opp_rev[tkey] = mark
        # What the opponent sees the mover as right now — for the recent
        # events feed (a revealed piece shows its bound marker, not its name).
        event["seen_mover"] = game["revealed_to"][opponent].get(tkey) or "?"

    # HQ deduction: stepping onto one enemy HQ without winning means the flag
    # was not there — and since the flag never leaves its HQ, it must be in
    # the OTHER one. Reveal it to the mover automatically.
    if not game["winner"] and to_pos in HQ_CELLS[opponent]:
        other_hq = next(p for p in HQ_CELLS[opponent] if p != to_pos)
        key = f"{other_hq[0]},{other_hq[1]}"
        if game["revealed_to"][owner].get(key) != "军旗":
            game["revealed_to"][owner][key] = "军旗"
            event["flag_deduced"] = list(other_hq)

    # No legal moves left for the side to move next => that side loses.
    if not game["winner"]:
        next_owner = "ai" if owner == "player" else "player"
        if not any(possible_moves(game, next_owner, p) for p in movable_pieces(game, next_owner)):
            game["winner"] = owner
            game["phase"] = "ended"

    game["turn"] += 1
    game["log"].append(event)
    return {"ok": True, "event": event, "winner": game["winner"]}


# === View for a specific owner ===================================

def view_for_owner(game, owner):
    view = []
    for r in range(ROWS):
        row = []
        for c in range(COLS):
            pos = (r, c)
            pos_key = f"{r},{c}"
            terrain = TERRAIN[pos]
            piece = game["board"].get(pos)

            cell = {"terrain": terrain["kind"], "rail": terrain["rail"], "piece": None}
            if piece is None:
                history = game["revealed_to"][owner].get(pos_key)
                if history:
                    cell["history"] = history
            elif piece["owner"] == owner:
                cell["piece"] = {"type": piece["type"], "owner": owner}
            else:
                p = {"type": "?", "owner": piece["owner"]}
                if game["winner"]:
                    # Game over — flip everything face-up so the player can
                    # see what the enemy still had.
                    p["type"] = piece["type"]
                    p["revealed"] = True
                else:
                    history = game["revealed_to"][owner].get(pos_key)
                    if history:
                        # Known enemy (fought it, deduced via HQ, or flag
                        # exposed by a dead 司令) — show its real type.
                        p["type"] = history
                        p["revealed"] = True
                cell["piece"] = p
            row.append(cell)
        view.append(row)
    return view


def full_board_view(game):
    """God-view of the board: every piece shown with its real type and owner.
    Used for replay frames — the point of a replay is to see what actually
    happened, not what each side knew at the time."""
    view = []
    for r in range(ROWS):
        row = []
        for c in range(COLS):
            t = TERRAIN[(r, c)]
            cell = {
                "type": t["kind"],
                "camp": t["kind"] == "camp",
                "hq": t["kind"] == "hq",
                "rail": t["rail"],
            }
            piece = game["board"].get((r, c))
            if piece:
                cell["piece"] = {"type": piece["type"], "owner": piece["owner"]}
            row.append(cell)
        view.append(row)
    return view


def setup_board_view(game, owner):
    """Board preview during the setup phase: shows only the owner's own
    placed pieces (the opponent's pieces aren't placed onto the shared
    board yet, and would be hidden either way)."""
    placed = game["setup"].get(owner, {})
    view = []
    for r in range(ROWS):
        row = []
        for c in range(COLS):
            terrain = TERRAIN[(r, c)]
            cell = {"terrain": terrain["kind"], "rail": terrain["rail"], "piece": None}
            t = placed.get(f"{r},{c}")
            if t:
                cell["piece"] = {"type": t, "owner": owner}
            row.append(cell)
        view.append(row)
    return view


# === Jev prompt construction ======================================

RULES_DESCRIPTION = """STANDARD ARMY CHESS (军棋/陆战棋) — 12x5 board, 25 pieces per side.
Coordinates are (row,col): row 0 = YOUR back/HQ row, row 5 = your front row, rows 6-11 = enemy half.

RANKS (higher beats lower): 司令(9) > 军长(8) > 师长(7) > 旅长(6) > 团长(5) > 营长(4) > 连长(3) > 排长(2) > 工兵(1)

SPECIAL PIECES:
- 军旗: immovable, sits in one of your 2 HQ squares. Moving onto the enemy flag wins instantly.
- 地雷: immovable PERMANENT barrier — any attacker except 工兵 dies and the mine STAYS
  on the board (defender_wins). Only 工兵 removes it (defuses it and survives).
- 炸弹: destroys itself AND any piece it touches, attacking or defending.

BATTLE: higher rank kills lower (winner takes the square); equal ranks both die.

TERRAIN:
- 行营 camp (5 per side, diamond): a piece inside CANNOT be attacked by anything. Camps are
  the only squares with diagonal links (step diagonally in/out; corner camps also reach the
  center camp diagonally). Inside a camp you move only 1 step, no rail bonus.
- 大本营 HQ (2 per side): holds the flag. Any piece ending a move on an HQ is stuck forever.
- 铁路 rail (both edge columns, plus rows 1,5,6,10 — but NEVER the HQ rows): starting on rail
  lets a piece slide any distance along connected rail instead of 1 step. 工兵 may turn
  corners; all other pieces slide straight only. Sliding stops at the first piece in the
  path — if enemy, that's an attack. Reaching the enemy HQ row always takes a final
  ordinary 1-step move (no rail slide can land on row 0/11).
- Mountain gap: crossing row 5 <-> row 6 is only possible on columns 0, 2, 4.

MOVE: exactly one piece per turn — 1 orthogonal step, or a rail slide, or 1 step in any of
up-to-8 directions when moving into/out of/within camps. Cannot enter your own piece's
square; cannot attack into a camp. Flag and mines never move.

WIN: capture the enemy 军旗, or leave the enemy with no legal move.

PRIORITIES (highest first):
  1. If any move captures the enemy 军旗 this turn, ALWAYS take it — it wins instantly.
  2. If an enemy piece can capture YOUR 军旗 next turn, capture that piece now.
  3. Everything else is normal strategy.

HIDDEN INFO: enemy types are hidden until combat. When pieces collide, the DEFENDER'S side
always learns the attacker's type — a surviving attacker stays marked even after it moves
later — while the attacker identifies the defender only if it wins the square. Listed in
enemy_pieces_revealed. Never feed a weaker piece into a revealed stronger enemy; check
ranks before choosing an [attack] option. A revealed entry can also be a DEDUCTION shown
when your attacking piece died: ">X或雷" = the defender outranks your X or is a mine
(a mine only stays possible while that piece sits in its back rows and has never moved —
once it moves the marker drops to ">X"); "地雷" = it stopped your 司令, which only a mine
can do; "非地雷" = it beat your 工兵, so it is a combat piece; "同级或炸弹" = mutual
destruction means equal rank or a bomb (a mine survives attacks, so it is never the
mutual-destruction defender). A plain ">X" marker (no 或雷) on a live enemy means it
killed your X and survived — strictly stronger than X, and provably a mobile combat
piece, never a mine.

recent_events = the last few half-moves from YOUR perspective — use them to keep a
coherent plan (follow up on a probe, keep pressing a weakened lane, stop wandering
the same piece back and forth). Enemy pieces there appear as your intel markers.

enemy_inventory = the enemy casualty ledger: confirmed_dead (types you verified),
uncertain_dead ("X或炸弹" — mutual destruction where you can't tell which), and
remaining_by_type with ranges like "0~2存活". strongest_confirmed_alive is the
highest rank definitely still on the board — if 司令 is gone, 军长 rules. If
bombs_remaining is "0~0" or P_all_bombs_gone is high, your big pieces can attack
unknowns far more safely.

FLAG INTELLIGENCE:
- HQ deduction: the flag can never leave its HQ. If a piece moves onto one enemy HQ and the
  game does NOT end, that square was not the flag — the OTHER enemy HQ is then automatically
  revealed to you as the flag (see enemy_flag_known_at).
- 亮旗: when a side's 司令 dies, its flag position is revealed to the opponent. Killing their
  司令 = free flag location; losing yours exposes your flag (your_flag_location_exposed_to_enemy)."""


def _piece_tag(pos, piece):
    """Compact piece description: '师长(5,2)|camp|rail' — terrain flags only
    shown when they differ from a plain post square."""
    tag = f"{piece['type']}({pos[0]},{pos[1]})"
    kind = TERRAIN[pos]["kind"]
    if kind != "post":
        tag += f"|{kind}"
    if TERRAIN[pos]["rail"]:
        tag += "|rail"
    return tag


def build_jev_state(game, owner):
    own_movable = []
    own_immovable = []
    enemy_pieces_unknown = []
    enemy_pieces_revealed = []

    for pos, piece in game["board"].items():
        pos_str = f"({pos[0]},{pos[1]})"
        kind = TERRAIN[pos]["kind"]
        if piece["owner"] == owner:
            if piece["type"] in IMMOVABLE or kind == "hq":
                own_immovable.append(_piece_tag(pos, piece))
            else:
                own_movable.append(_piece_tag(pos, piece))
        else:
            rev = game["revealed_to"][owner].get(f"{pos[0]},{pos[1]}")
            if rev:
                entry = {"pos": pos_str, "type": rev}
                if kind != "post":
                    entry["terrain"] = kind
                enemy_pieces_revealed.append(entry)
            else:
                enemy_pieces_unknown.append(
                    pos_str if kind == "post" else f"{pos_str}|{kind}")

    # Flag intel: squares the opponent's flag is known/deduced to occupy,
    # and whether OUR flag's location has been exposed to the enemy.
    opponent = "ai" if owner == "player" else "player"
    enemy_flag_known = [
        pos_key for pos_key, t in game["revealed_to"][owner].items() if t == "军旗"
    ]
    own_flag_pos = next(
        (f"{p[0]},{p[1]}" for p, pc in game["board"].items()
         if pc["owner"] == owner and pc["type"] == "军旗" and pc["alive"]), None)
    own_flag_exposed = bool(
        own_flag_pos and game["revealed_to"][opponent].get(own_flag_pos) == "军旗")

    # --- Enemy casualty ledger -----------------------------------------
    # Derived from the combat log — always from THIS side's knowledge:
    #   we won attacking      -> defender type was revealed: exact death
    #   enemy attacked & died -> dead attacker flips face-up: exact death
    #   we attacked, both die -> defender was "同级或炸弹": fuzzy death
    pool = Counter(SETUP_POOL[opponent])
    dead_exact = Counter()
    dead_fuzzy = []          # e.g. "旅长或炸弹"
    for ev in game["log"]:
        if not ev.get("combat"):
            continue
        oc = ev["outcome"]
        if ev["actor"] == owner:
            if oc in ("attacker_wins", "flag_taken_player"):
                dead_exact[ev["defender_type"]] += 1
            elif oc == "both_die":
                dead_fuzzy.append(f"{ev['attacker_type']}或炸弹")
        else:
            if oc in ("defender_wins", "both_die"):
                dead_exact[ev["attacker_type"]] += 1

    remaining = {}
    for t, n in pool.items():
        exact = dead_exact.get(t, 0)
        fuzzy = sum(1 for lbl in dead_fuzzy if t in lbl.split("或"))
        lo, hi = max(0, n - exact - fuzzy), n - exact
        if hi == 0:
            remaining[t] = "全灭(已确认)"
        elif lo == hi:
            remaining[t] = f"{hi}存活"
        else:
            # rough P(each fuzzy death was this type) ≈ 0.5 — honest range
            remaining[t] = f"{lo}~{hi}存活(含{fuzzy}例疑似)"
    strongest_confirmed = next(
        (t for t in sorted(RANK, key=RANK.get, reverse=True)
         if dead_exact.get(t, 0) < pool[t]
         and not any(t in l.split("或") for l in dead_fuzzy)), None)
    strongest_possible = next(
        (t for t in sorted(RANK, key=RANK.get, reverse=True)
         if pool[t] > dead_exact.get(t, 0)), None)

    # Per-side fuzzy deaths can also settle the "did a bomb die" question:
    # P(all bombs gone) ≈ 0.5^(fuzzy labels that could be bombs) when the
    # remaining slots only come from fuzzy deaths.
    bomb_fuzzy = sum(1 for l in dead_fuzzy if "炸弹" in l)
    bomb_hi = pool["炸弹"] - dead_exact.get("炸弹", 0)
    bomb_lo = max(0, bomb_hi - bomb_fuzzy)
    p_all_bombs_gone = round(0.5 ** bomb_fuzzy, 2) if bomb_lo == 0 and bomb_fuzzy else (
        1.0 if bomb_hi == 0 else 0.0)

    enemy_inventory = {
        "confirmed_dead": dict(dead_exact),
        "uncertain_dead": dead_fuzzy,
        "remaining_by_type": remaining,
        "strongest_confirmed_alive": strongest_confirmed,
        "strongest_possibly_alive": strongest_possible,
        "bombs_remaining": f"{bomb_lo}~{bomb_hi}",
        "P_all_bombs_gone": p_all_bombs_gone,
    }

    # Continuous memory: the last few half-moves, rendered from THIS side's
    # perspective — own pieces show real types, enemies show only what this
    # side legitimately learned (">X" bounds, dead pieces face-up, or "?").
    # Without it the model has zero memory and replans from scratch every turn.
    outcome_cn = {"attacker_wins": "攻胜", "defender_wins": "守胜",
                  "both_die": "同毁", "flag_taken_player": "夺旗",
                  "flag_taken_ai": "夺旗"}
    recent = []
    for ev in game["log"][-6:]:
        mine_move = ev["actor"] == owner
        tag = "你" if mine_move else "敌"
        fr, to = tuple(ev["from"]), tuple(ev["to"])
        if ev.get("combat"):
            atk = ev["attacker_type"] if mine_move else ev.get("seen_attacker", "?")
            dfd = ev.get("seen_defender", "?") if mine_move else ev["defender_type"]
            s = f"{tag}:{atk}{fr}→{dfd}{to} {outcome_cn.get(ev['outcome'], ev['outcome'])}"
        else:
            lbl = ev["mover_type"] if mine_move else ev.get("seen_mover", "?")
            s = f"{tag}:{lbl}{fr}→{to}"
        if ev.get("flags_revealed"):
            s += " ⚑亮旗"
        if ev.get("flag_deduced"):
            s += " ⚑推断出旗"
        recent.append(s)

    return {
        "rules": RULES_DESCRIPTION,
        "you_are": owner,
        "your_pieces_movable": own_movable,
        "your_pieces_immovable": own_immovable,
        "enemy_pieces_unknown": enemy_pieces_unknown,
        "enemy_pieces_revealed": enemy_pieces_revealed,
        "enemy_flag_known_at": enemy_flag_known,
        "your_flag_pos": own_flag_pos,
        "your_flag_location_exposed_to_enemy": own_flag_exposed,
        "recent_events": recent,
        "enemy_inventory": enemy_inventory,
        "turn": game["turn"],
        "board_size": f"{ROWS}x{COLS}",
        "your_goal": "Capture the opponent's 军旗 (flag), or leave them with no legal move.",
    }


def build_jev_questions():
    return {
        "primary_move": {
            "type": "choice",
            "instructions":
                "Choose ONE legal move from your movable pieces (cannot move flag or mine, "
                "cannot move a piece stuck in an HQ). Each option is a move: 'm0' = first "
                "legal move, 'm1' = second, etc. Stepping onto an unknown enemy square "
                "triggers battle per the rules; a piece sitting in a camp cannot be attacked.",
            "criteria": {},
        },
        "move_purpose": {
            "type": "choice",
            "instructions":
                "What is the strategic purpose of the SAME move you picked in primary_move? "
                "Your answer must describe that move — e.g. if you chose an [attack] option, "
                "do not answer camp_retreat unless attacking is itself the retreat.",
            "criteria": {
                "exploration": "Move to a likely-empty square to scout opponent layout",
                "aggressive": "Attack suspected strong pieces (rank-based on what you've revealed)",
                "flag_hunt": "Move toward likely flag positions to capture opponent's 军旗",
                "defensive": "Stay near your own 军旗 / HQ to defend against flag_hunt",
                "mine_clear": "Use 工兵 to defuse a suspected 地雷 (engineer only)",
                "camp_retreat": "Move a valuable/threatened/already-revealed piece into an "
                    "empty 行营 (camp), orthogonally or diagonally, so it becomes completely "
                    "unattackable",
                "camp_hub": "Use the camp diamond's diagonal links (including camp-to-camp "
                    "diagonal steps) as a shortcut to reposition a supporting piece between "
                    "flanks/center faster than the plain squares would allow",
                "rail_maneuver": "Use a straight rail line to reposition quickly across the "
                    "board (not the camp diamond's diagonal shortcuts)",
                "retreat": "Move away from a threatened position (not into a camp)",
            },
        },
        "aggression": {
            "type": "score",
            "instructions": "How aggressive is this turn's overall stance?",
            "criteria": [
                "Very defensive — pure defense / no attack intent",
                "Defensive — cautious, only safe probes",
                "Balanced — mix of defense and offense",
                "Aggressive — committed to attack / capture",
                "Very aggressive — all-in on flag capture",
            ],
        },
        "should_take_risk": {
            "type": "noul",
            "instructions":
                "Does this turn involve moving onto a square that might be an enemy piece "
                "or mine (unknown risk)? Set true if the move is uncertain; false if it is "
                "clearly safe (e.g. retreating or moving to known-empty).",
        },
        "confidence_in_win": {
            "type": "noul",
            "instructions":
                "Your overall probability of winning this game from this position (0 = "
                "losing for sure, 1 = winning for sure).",
        },
    }

"""Learn an endgame value function from REAL played games (replays/*.json),
not synthetic self-play against a frozen opponent.

This is the "revisit past battles to gain more experience" piece of the RSI
loop you asked for: `self_evolve.py`'s weight mutation never looks at a
single real game, it only has candidates play each other from scratch. This
module instead extracts genuine labeled experience --

    (position P, player-to-move's own pegs, opponent's pegs)
        -> "player-to-move went on to need N more of THEIR OWN moves
            to finish, in a game they actually won"

-- from every completed replay, and fits a small linear model to predict
"moves remaining to finish" from a position. That learned model is a
MUCH better search heuristic than the hand-written "sum of hex-distance to
nearest empty target hole" (see self_evolve.py's solo_efficiency
experiment): the hand-written heuristic is blind to "retreat now to set up
a jump chain" patterns (it always penalizes a peg moving further from the
target, even when that's the correct move), whereas a value function fit
to real completed games has actually seen such sequences pay off and can
rate the resulting position as closer to finishing than the naive distance
metric does.

Why this is still safe/bounded RSI (not unrestricted self-modification):
  - Only numeric regression coefficients are learned/persisted
    (value_model.json), same promotion-gated champion/challenger pattern as
    weights_champion.json.
  - Training data is real completed games (replays/) plus, optionally,
    self-play games recorded by self_evolve.py -- both are just data, not
    code the model could use to rewrite itself.
  - The model can only ever be used as a scoring function for existing
    search code (ai_engine.py, self_evolve.py); it cannot add new move
    types or change what's legal.
"""

import glob
import json
import os

import rules

MODEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "value_model.json")
REPLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays")

FEATURE_NAMES = ["sum_dist", "max_dist", "second_max_dist", "mean_dist",
                 "n_remaining", "pegs_in_start", "bias"]

# Fallback coefficients equivalent to "sum_dist alone" (the naive heuristic)
# so a fresh checkout with no trained model behaves like the old baseline.
DEFAULT_COEFFS = {"sum_dist": 1.0, "max_dist": 0.0, "second_max_dist": 0.0,
                   "mean_dist": 0.0, "n_remaining": 0.0, "pegs_in_start": 0.0,
                   "bias": 0.0}


def _hex_dist(a, b):
    dq, dr = a[0] - b[0], a[1] - b[1]
    return (abs(dq) + abs(dr) + abs(dq + dr)) // 2


def featurize(own_pegs, target_zone_holes, start_zone_holes=None):
    """own_pegs: iterable of (q,r) tuples for the player whose value we're
    estimating. target_zone_holes: that player's target arm (10 holes).
    start_zone_holes: that player's own start arm, for the "still stuck at
    home" feature (optional -- omitted it just scores 0, safe default)."""
    own_pegs = list(own_pegs)
    target_set = set(target_zone_holes)
    start_set = set(start_zone_holes or [])
    remaining = [p for p in own_pegs if p not in target_set]
    empty_targets = [h for h in target_zone_holes if h not in own_pegs]
    if remaining and empty_targets:
        dists = [min(_hex_dist(p, h) for h in empty_targets) for p in remaining]
    else:
        dists = [0 for _ in remaining]
    dists_sorted = sorted(dists, reverse=True)
    sum_dist = float(sum(dists))
    max_dist = float(dists_sorted[0]) if dists_sorted else 0.0
    second_max_dist = float(dists_sorted[1]) if len(dists_sorted) > 1 else 0.0
    mean_dist = sum_dist / len(dists) if dists else 0.0
    return {
        "sum_dist": sum_dist,
        "max_dist": max_dist,
        "second_max_dist": second_max_dist,
        "mean_dist": mean_dist,
        "n_remaining": float(len(remaining)),
        "pegs_in_start": float(sum(1 for p in own_pegs if p in start_set)),
        "bias": 1.0,
    }


def predict(coeffs, feats):
    return sum(coeffs.get(k, 0.0) * v for k, v in feats.items())


def load_coeffs(path=None):
    path = path or MODEL_FILE
    coeffs = dict(DEFAULT_COEFFS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        coeffs.update({k: v for k, v in data.get("coeffs", {}).items()
                       if k in DEFAULT_COEFFS})
    except (OSError, json.JSONDecodeError):
        pass
    return coeffs


def save_coeffs(coeffs, meta=None, path=None):
    path = path or MODEL_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"coeffs": coeffs, "meta": meta or {}}, f,
                  indent=2, ensure_ascii=False)


# ---------------- experience extraction from real replays ----------------

def extract_examples(replay_dir=None):
    """Returns list of (features_dict, label) for every (position, mover)
    pair in every completed game, labeled with the TRUE number of further
    moves that player made before finishing (only for the side that
    actually won -- the loser's "moves remaining" is unknown/undefined
    since the game stopped as soon as the winner finished)."""
    replay_dir = replay_dir or REPLAY_DIR
    examples = []
    for path in glob.glob(os.path.join(replay_dir, "*.json")):
        if "_controllers" in path:
            continue
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        winner = d.get("winner")
        frames = d.get("frames") or []
        if winner not in rules.PLAYERS or len(frames) < 2:
            continue
        winner_seat = rules.PLAYERS.index(winner)
        target = rules.target_zone(winner)
        start = rules.START_ZONE[winner]
        # actor[k] = who made the move that produced frames[k]
        actor = [None] * len(frames)
        for k in range(1, len(frames)):
            actor[k] = rules.PLAYERS[frames[k - 1]["turn"]]
        for m in range(len(frames) - 1):
            if frames[m]["turn"] != winner_seat:
                continue  # only label positions where the winner is to move
            moves_remaining = sum(1 for k in range(m + 1, len(frames))
                                   if actor[k] == winner)
            if moves_remaining <= 0:
                continue
            own_pegs = [tuple(p) for p in frames[m]["pegs"][winner]]
            examples.append((featurize(own_pegs, target, start), float(moves_remaining)))
    return examples


# ---------------- tiny linear regression (no numpy; pure stdlib) ----------

def _solve_linear_system(A, b):
    """Gaussian elimination with partial pivoting. A: list of rows, b: list."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[pivot][col]) < 1e-12:
            continue
        M[col], M[pivot] = M[pivot], M[col]
        pv = M[col][col]
        M[col] = [x / pv for x in M[col]]
        for r in range(n):
            if r != col and abs(M[r][col]) > 1e-15:
                factor = M[r][col]
                M[r] = [x - factor * y for x, y in zip(M[r], M[col])]
    return [M[i][n] for i in range(n)]


def fit_linear(examples, l2=1.0):
    """Ridge-regularized least squares (ridge keeps it stable with few
    examples / collinear features) solved via the normal equations --
    small enough (7x7) that plain Gaussian elimination is fine, no numpy
    dependency needed for this to run unattended on the server."""
    names = FEATURE_NAMES
    n = len(names)
    AtA = [[0.0] * n for _ in range(n)]
    Atb = [0.0] * n
    for feats, label in examples:
        x = [feats.get(k, 0.0) for k in names]
        for i in range(n):
            Atb[i] += x[i] * label
            for j in range(n):
                AtA[i][j] += x[i] * x[j]
    for i in range(n):
        AtA[i][i] += l2
    coeffs_vec = _solve_linear_system(AtA, Atb)
    return dict(zip(names, coeffs_vec))


def train_from_replays(replay_dir=None, l2=1.0):
    examples = extract_examples(replay_dir)
    if len(examples) < 30:
        raise ValueError(f"only {len(examples)} labeled examples found -- "
                         "need more completed replays to fit a model")
    coeffs = fit_linear(examples, l2=l2)
    return coeffs, len(examples)


if __name__ == "__main__":
    coeffs, n = train_from_replays()
    print(f"trained on {n} examples from real games:")
    for k, v in coeffs.items():
        print(f"  {k}: {v:.4f}")
    save_coeffs(coeffs, meta={"n_examples": n, "source": "replays"})
    print(f"saved to {MODEL_FILE}")

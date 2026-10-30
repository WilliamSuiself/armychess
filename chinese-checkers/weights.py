"""Tunable heuristic weights for the Chinese Checkers evaluation function.

Only the pure *coefficients* (how much each factor matters) are evolvable —
geometric thresholds (what counts as "deep"/"central"/"in escort range") stay
as fixed constants in ai_engine.py, since they're tied to this specific
board's topology and mutating them produces discontinuous, often degenerate
behavior rather than a smooth fitness landscape. Keeping the search space to
smoothly-varying magnitudes is what makes an unattended evolutionary loop
safe to run without a human looking at every generation.

`load()` reads weights_champion.json if present (the currently-promoted best
weights from self_evolve.py) and falls back to DEFAULT_WEIGHTS — which are
exactly the magic numbers that were previously hardcoded inline, so a fresh
checkout behaves identically to before this refactor.
"""

import json
import os

WEIGHTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "weights_champion.json")

DEFAULT_WEIGHTS = {
    # opening_score
    "open_deep_leave_base": 0.4,
    "open_deep_leave_per_depth": 0.4,
    "open_deep_stay_penalty": -0.3,
    "open_center_bonus_near": 0.5,
    "open_center_bonus_far": 0.2,
    "open_scaffold_bonus": 0.25,
    # formation_score
    "form_rear_weight": 1.2,
    "form_rear2_weight": 0.6,
    "form_escort_bonus": 0.3,
    "form_escort_penalty": -1.6,
    "form_corner_fill_weight": 0.15,
    # ladder / two-ply lookahead
    "ladder_weight": 0.5,
    # move_fallback (local heuristic used when no model backend is available)
    "fallback_leave_zone_bonus": 0.6,
    "fallback_low_score_jump_penalty": 0.1,
}

# Reasonable clamp range for mutation — keeps self-play evolution from
# wandering into degenerate magnitudes (e.g. a weight so large it swamps
# every other term, or flips sign in a way that inverts the strategy).
BOUNDS = {k: (-3.0, 3.0) if v < 0 else (0.0, 3.0) for k, v in DEFAULT_WEIGHTS.items()}


def load(path=None):
    path = path or WEIGHTS_FILE
    w = dict(DEFAULT_WEIGHTS)
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            w.update({k: v for k, v in data.get("weights", data).items()
                      if k in DEFAULT_WEIGHTS})
    except (OSError, json.JSONDecodeError):
        pass
    return w


def save_champion(weights, meta=None, path=None):
    path = path or WEIGHTS_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"weights": weights, "meta": meta or {}}, f,
                  indent=2, ensure_ascii=False)

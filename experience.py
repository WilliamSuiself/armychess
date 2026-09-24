"""Local "experience" layer for the Jev-powered army chess AI.

This does NOT fine-tune Jev itself (the hosted jev-latest model behind
knox.chat is a stateless, frozen model — there is no weight-update endpoint
on /v1/systemone). Instead we keep a small local, genuinely-trainable file
(`weights.json`) that:

  1. Records, for every game, which "move_purpose" / "aggression" choices
     Jev made during the AI's turns, and whether the AI eventually won.
  2. Aggregates that into win-rate statistics per (game phase, purpose) and
     per (game phase, aggression bucket).
  3. Feeds a short natural-language summary of those statistics back into
     the `state` we send to Jev on the next turn ("experience_notes"), so
     Jev's zero-shot judgement is conditioned on real track record instead
     of nothing.
  4. Exposes a small numeric bias that can nudge move selection when Jev's
     own confidence is low and two candidate moves imply different
     historical performance.

The file grows smarter purely from self-play: every finished game updates
the counts, so win rates (and the hint text derived from them) keep
converging as more games are recorded.
"""

import json
import os
import time

DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "weights.json")

PURPOSES = [
    "exploration", "aggressive", "flag_hunt", "defensive", "bomb_trade",
    "mine_clear", "camp_retreat", "camp_hub", "rail_maneuver", "retreat",
]
PHASES = ["opening", "midgame", "endgame"]


def _empty_stat():
    return {"n": 0, "wins": 0}


def default_weights():
    return {
        "games_played": 0,
        "wins": 0,
        "losses": 0,
        "purpose_stats": {phase: {p: _empty_stat() for p in PURPOSES} for phase in PHASES},
        "aggression_stats": {phase: {str(i): _empty_stat() for i in range(5)} for phase in PHASES},
        "updated_at": None,
    }


def load(path=DEFAULT_PATH):
    if not os.path.exists(path):
        return default_weights()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return default_weights()
    # Backfill any keys missing from an older file version.
    base = default_weights()
    base.update({k: v for k, v in data.items() if k in base})
    for phase in PHASES:
        base["purpose_stats"].setdefault(phase, {})
        for p in PURPOSES:
            base["purpose_stats"][phase].setdefault(p, _empty_stat())
        base["aggression_stats"].setdefault(phase, {})
        for i in range(5):
            base["aggression_stats"][phase].setdefault(str(i), _empty_stat())
    return base


def save(weights, path=DEFAULT_PATH):
    weights["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(weights, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


# === Per-game buffer (kept in the in-memory GAME dict, not persisted) ===

def new_buffer():
    return []


def game_phase(own_piece_count, enemy_known_or_guessed_count):
    total = own_piece_count + enemy_known_or_guessed_count
    if total >= 40:
        return "opening"
    if total >= 16:
        return "midgame"
    return "endgame"


def record_ai_turn(buffer, purpose, aggression_score, phase):
    if purpose not in PURPOSES:
        purpose = "exploration"
    bucket = max(0, min(4, round(aggression_score or 0)))
    buffer.append({"purpose": purpose, "aggression_bucket": bucket, "phase": phase})


def finalize_game(weights, buffer, ai_won, draw=False):
    weights["games_played"] += 1
    if draw:
        weights["draws"] = weights.get("draws", 0) + 1
    else:
        weights["wins" if ai_won else "losses"] += 1
    for turn in buffer:
        phase, purpose, bucket = turn["phase"], turn["purpose"], turn["aggression_bucket"]
        pstat = weights["purpose_stats"][phase][purpose]
        pstat["n"] += 1
        pstat["wins"] += int(ai_won)
        astat = weights["aggression_stats"][phase][str(bucket)]
        astat["n"] += 1
        astat["wins"] += int(ai_won)
    save(weights)
    return weights


# === Turning stats into something useful for the next decision =========

def _winrate(stat, min_n=3):
    if stat["n"] < min_n:
        return None
    return stat["wins"] / stat["n"]


# Below this many recorded games the win-rate stats are pure noise — don't
# inject them into the prompt at all (a "0% win rate" prior from 2 games
# only teaches Jev to distrust everything).
MIN_GAMES_FOR_HINT = 5


def build_hint_text(weights, phase):
    """Short natural-language experience summary for the prompt, or None
    when there isn't enough history to be meaningful."""
    games = weights["games_played"]
    if games < MIN_GAMES_FOR_HINT:
        return None

    overall_wr = weights["wins"] / games if games else 0.0
    lines = [
        f"Local experience log: {games} self-played games so far, "
        f"AI overall win rate {overall_wr:.0%}.",
        f"Stats for the current game phase ({phase}):",
    ]

    purpose_rates = []
    for p, stat in weights["purpose_stats"][phase].items():
        wr = _winrate(stat)
        if wr is not None:
            purpose_rates.append((p, wr, stat["n"]))
    if purpose_rates:
        purpose_rates.sort(key=lambda x: -x[1])
        summary = ", ".join(f"{p}={wr:.0%} (n={n})" for p, wr, n in purpose_rates[:5])
        lines.append(f"  move_purpose win rate: {summary}")
    else:
        lines.append("  move_purpose win rate: not enough data yet")

    agg_rates = []
    for b, stat in weights["aggression_stats"][phase].items():
        wr = _winrate(stat)
        if wr is not None:
            agg_rates.append((b, wr, stat["n"]))
    if agg_rates:
        agg_rates.sort(key=lambda x: int(x[0]))
        summary = ", ".join(f"level{b}={wr:.0%} (n={n})" for b, wr, n in agg_rates)
        lines.append(f"  aggression-level win rate: {summary}")

    lines.append(
        "Use this only as a soft prior alongside the board state — the rules and "
        "current position always take priority over historical stats."
    )
    return "\n".join(lines)


def purpose_prior_bonus(weights, phase, purpose, scale=0.05):
    """Small nudge (-scale..+scale) derived from historical win rate vs 50%."""
    stat = weights["purpose_stats"].get(phase, {}).get(purpose)
    wr = _winrate(stat) if stat else None
    if wr is None:
        return 0.0
    return (wr - 0.5) * 2 * scale

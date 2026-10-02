"""AI decision layer for competitive Tetris — same state+questions ->
typed-answers contract used by the army-chess project (Jev's /systemone
endpoint; Laya is a drop-in local replacement). Instead of asking the
model to drive the piece key-by-key in real time (which would need one
slow network round trip per keystroke), we enumerate every legal final
resting placement for the current piece — like army-chess enumerates
every legal move — and ask the model to pick ONE. The engine then
animates the piece into that placement.
"""

import concurrent.futures
import random
import time

from tetris import (
    COLS, ROWS, ROTATIONS, Piece, cells_valid, hard_drop_row,
    column_heights, count_holes, bumpiness, garbage_for_clear,
    board_to_rows,
)

MAX_PLACEMENT_OPTIONS = 10

# Real backend calls run here so a slow one can be abandoned (falling back to the local
# heuristic) without blocking the game loop; bounded so a string of slow calls queues
# rather than spawning unbounded threads.
_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="ai-call")

RULES_DESCRIPTION = """COMPETITIVE TETRIS (2-player battle) — 10 cols x 20 rows board per side.
Coordinates are (row,col), row 0 = top, row 19 = bottom, col 0 = left, col 9 = right.
A piece is placed by choosing its final RESTING position (rotation + column) after a hard
drop — you are not asked to move it step by step.

PIECES: I,O,T,S,Z,J,L (standard tetrominoes). Rotations are numbered 0-3 (0 = spawn
orientation, each step is 90° clockwise).

LINE CLEARS send GARBAGE to the OPPONENT's board (rows of near-solid blocks with one
random gap, added to the bottom of their board, pushing their stack up — can top them out):
  1 line = 0 garbage, 2 lines = 1, 3 lines = 2, 4 lines (TETRIS) = 4. Consecutive
  clearing placements build a COMBO that adds a small garbage bonus on top.

LOSING CONDITION: a new piece has nowhere to spawn (the stack reached the top) — either
from your own placements or from garbage pushed in by the opponent's attacks.

GOAL: maximize lines cleared / garbage sent to the opponent while keeping your own stack
low, flat, and hole-free (a HOLE = an empty cell with a filled cell somewhere above it in
the same column — holes are hard to clear later and should be avoided)."""

# Some backends (notably Laya, a small local model with a 512-token context) truncate a
# long `state` from the END if it doesn't fit — so anything placed late in the dict can be
# silently dropped. Keep a short one-liner for that tail slot; the full RULES_DESCRIPTION
# above is only used for backends with room to spare.
SHORT_RULES = ("Tetris battle: clear lines to send garbage to the opponent (1L=0,2L=1,"
               "3L=2,4L/Tetris=4 garbage); avoid holes and keep your stack low; "
               "topping out (no room to spawn) loses.")


def enumerate_placements(board, letter):
    """All distinct legal final placements (after hard drop) for `letter`
    on `board`. Each entry describes the placement + resulting metrics."""
    base_holes = count_holes(board)
    seen = set()
    out = []
    for rot in range(4):
        shape = ROTATIONS[letter][rot]
        min_c = min(c for _, c in shape)
        max_c = max(c for _, c in shape)
        for col in range(-min_c, COLS - max_c):
            piece = Piece(letter, rot, row=0, col=col)
            if not cells_valid(board, piece.cells()):
                continue  # stack already too tall at spawn for this column
            landed = hard_drop_row(board, piece)
            key = (rot_signature(letter, rot), landed.col, landed.row)
            cell_set = tuple(sorted(landed.cells()))
            if cell_set in seen:
                continue
            seen.add(cell_set)

            sim = [row[:] for row in board]
            for (r, c) in cell_set:
                if 0 <= r < ROWS:
                    sim[r][c] = letter
            lines_cleared = _clear_lines_inplace(sim)
            heights = column_heights(sim)
            holes_after = count_holes(sim)

            out.append({
                "rot": rot, "col": landed.col, "row": landed.row,
                "cells": cell_set,
                "lines_cleared": lines_cleared,
                "holes_after": holes_after,
                "holes_delta": holes_after - base_holes,
                "max_height_after": max(heights) if heights else 0,
                "bumpiness_after": bumpiness(heights),
                "garbage_sent": garbage_for_clear(lines_cleared),
                "touches_left_wall": landed.col == 0,
                "touches_right_wall": (landed.col + _shape_width(letter, rot) - 1) == COLS - 1,
            })
    return out


def rot_signature(letter, rot):
    return tuple(sorted(ROTATIONS[letter][rot]))


def _shape_width(letter, rot):
    shape = ROTATIONS[letter][rot]
    return max(c for _, c in shape) - min(c for _, c in shape) + 1


def _clear_lines_inplace(board):
    full_idx = [r for r in range(len(board)) if all(cell is not None for cell in board[r])]
    if not full_idx:
        return 0
    keep = [row for r, row in enumerate(board) if r not in full_idx]
    n = len(full_idx)
    board[:] = [[None] * COLS for _ in range(n)] + keep
    return n


# Danger thresholds on pre-placement max stack height (board is 20 rows tall).
# Past CRITICAL the model's attack/tetris-setup instincts must be structurally
# overridden — a model that keeps "building toward a big clear" at height 17
# tops out before the garbage it dreams of ever lands.
WARN_HEIGHT = 13
CRITICAL_HEIGHT = 16


def _danger_level(max_h):
    if max_h >= CRITICAL_HEIGHT:
        return "critical"
    if max_h >= WARN_HEIGHT:
        return "warning"
    return "safe"


def heuristic_score(c):
    """Higher is better — used only to shortlist candidates for the model
    (like army-chess's prune_moves), never to pick the move outright. Weighted
    fairly aggressively against holes/height so that even a so-so pick among the
    shortlist by a weak backend is still a reasonable move, not just a legal one."""
    score = 0.0
    score += {0: 0, 1: 2, 2: 5, 3: 9, 4: 16}.get(c["lines_cleared"], 0)
    score += c["garbage_sent"] * 2.0
    score -= c["holes_delta"] * 6.0
    score -= c["max_height_after"] * 0.4
    if c["max_height_after"] > WARN_HEIGHT:
        score -= (c["max_height_after"] - WARN_HEIGHT) * 1.5
    if c["max_height_after"] >= CRITICAL_HEIGHT:
        score -= 40                       # near-topout: survival dominates
    score -= c["bumpiness_after"] * 0.3
    if c["touches_left_wall"] or c["touches_right_wall"]:
        score += 0.3
    score += random.random() * 0.15  # tie-break jitter
    return score


def prune_placements(candidates, max_options=MAX_PLACEMENT_OPTIONS,
                     danger="safe"):
    """Rank candidates and keep the top `max_options` for the model. In the
    danger zones we additionally drop options that don't relieve the stack:
    survival overrides attack — same pattern as doudizhu's hard pruning."""
    pool = list(candidates)
    if danger == "critical":
        # only moves that clear a line or don't raise the stack further
        keep = [c for c in pool
                if c["lines_cleared"] > 0
                or c["max_height_after"] < CRITICAL_HEIGHT]
        pool = keep or sorted(pool, key=lambda c: c["max_height_after"])[:5]
    elif danger == "warning":
        keep = [c for c in pool
                if c["lines_cleared"] > 0
                or c["max_height_after"] < CRITICAL_HEIGHT]
        if keep:
            pool = keep
    if len(pool) <= max_options:
        return pool
    ranked = sorted(pool, key=heuristic_score, reverse=True)
    return ranked[:max_options]


ROT_LABEL = {0: "spawn朝向", 1: "顺时针90°", 2: "180°", 3: "顺时针270°"}


def compact_label(c):
    """Short, plain-word option label used in the `placement` choice criteria sent to the
    model. Small local backends (Laya) budget only ~192 tokens across ALL options combined
    and hard-truncate each one when that's exceeded, so this stays terse — but Laya's
    tokenizer maps common English words ('rotate', 'col', 'clear', 'hole', 'height') to a
    single token each (verified against its actual tokenizer), so plain words cost almost
    the same as cryptic abbreviations while being far more likely to match something the
    encoder actually learned during pretraining. Rotation/column still come first so they
    survive even the worst-case truncation. The rich Chinese description used for the UI
    panel (`describe_placement`, below) is built separately and never sent to the model."""
    tags = [f"rotate{c['rot']}", f"col{c['col']}", f"clear{c['lines_cleared']}"]
    if c["garbage_sent"]:
        tags.append(f"attack{c['garbage_sent']}")
    tags.append(f"hole{c['holes_delta']:+d}" if c["holes_delta"] else "hole0")
    tags.append(f"height{c['max_height_after']}")
    if c["touches_left_wall"] or c["touches_right_wall"]:
        tags.append("wall")
    return " ".join(tags)


def describe_placement(c):
    tags = []
    if c["lines_cleared"] == 4:
        tags.append("TETRIS!清4行")
    elif c["lines_cleared"] > 0:
        tags.append(f"清{c['lines_cleared']}行")
    if c["garbage_sent"] > 0:
        tags.append(f"倒垫{c['garbage_sent']}行给对手")
    if c["holes_delta"] > 0:
        tags.append(f"新增{c['holes_delta']}个空洞")
    elif c["holes_delta"] < 0:
        tags.append(f"消除{-c['holes_delta']}个旧空洞")
    if c["touches_left_wall"]:
        tags.append("贴左墙")
    if c["touches_right_wall"]:
        tags.append("贴右墙")
    tags.append(f"落地后最高{c['max_height_after']}格")
    label = f"旋转{c['rot']}({ROT_LABEL[c['rot']]}) → 列{c['col']}"
    if tags:
        label += " [" + ", ".join(tags) + "]"
    return label


# How many bottom rows of each board to include as text. Small local backends have a tight
# token budget and a `state` that doesn't fit gets truncated from the END, so we keep this
# compact rather than sending all 20 (mostly-empty) rows — column_heights/holes already give
# the exact numeric picture of anything above this window.
BOARD_TEXT_ROWS = 12


def build_state(letter, hold_letter, next_letters, board, opp_board,
                 own_stats, opp_stats, pending_garbage):
    """Field order matters: some backends (Laya) truncate an over-budget `state` from the
    END, so the fields a placement decision actually needs — current piece, heights, holes,
    incoming garbage — go first; the bulkier board text and the rules blurb go last and are
    the first things to be silently dropped if space runs out."""
    heights = column_heights(board)
    max_h = max(heights) if heights else 0
    return {
        "current_piece": letter,
        "hold_piece": hold_letter,
        "next_pieces": next_letters,
        "your_column_heights": heights,
        "your_max_height": max_h,
        "rows_until_top_out": ROWS - max_h,
        "your_danger_level": _danger_level(max_h),
        "danger_note": ("DANGER: your stack is critically high — survival "
                        "first: prefer clear/lower placements, do NOT set up "
                        "a Tetris well now." if _danger_level(max_h) == "critical"
                        else ""),
        "your_holes": count_holes(board),
        "garbage_incoming_next_spawn": pending_garbage,
        "opponent_column_heights": column_heights(opp_board),
        "opponent_holes": count_holes(opp_board),
        "your_score": own_stats.get("score", 0),
        "your_lines_cleared": own_stats.get("lines", 0),
        "your_combo": own_stats.get("combo", 0),
        "opponent_score": opp_stats.get("score", 0),
        "your_goal": "Clear lines (esp. multi-line/Tetris) to send garbage and outlast the "
                      "opponent, while keeping your own board low and hole-free.",
        "your_board_bottom_rows": board_to_rows(board)[-BOARD_TEXT_ROWS:],
        "opponent_board_bottom_rows": board_to_rows(opp_board)[-BOARD_TEXT_ROWS:],
        "board_size": f"{ROWS}x{COLS}",
        "rules": SHORT_RULES,
    }


def build_questions():
    return {
        "placement": {
            "type": "choice",
            "instructions":
                "Pick ONE placement for the current piece. Each option lists: rotate "
                "(0-3), col (landing column), clear (lines cleared this drop; clear4 = "
                "Tetris, sends the most garbage to the opponent), attack (garbage sent, "
                "only shown if > 0), hole (change in hole count — negative removes old "
                "holes, positive creates new ones, avoid positive), height (resulting max "
                "stack height — lower is safer), wall (touches a side wall). Prefer "
                "options with a high clear/attack and a low/negative hole and height. "
                "IMPORTANT: check your_danger_level first — if it is 'critical' "
                "(stack almost at the top), survival overrides attack: pick the "
                "option that clears lines or lowers height, and do NOT delay a "
                "clear to set up a future Tetris.",
            "criteria": {},
        },
        "placement_purpose": {
            "type": "choice",
            "instructions":
                "What is the strategic purpose of the SAME placement you picked?",
            "criteria": {
                "clear_lines": "Complete one or more full rows this placement",
                "tetris_setup": "Keep one column empty (a 'well') to set up a future "
                    "4-line Tetris clear instead of clearing fewer lines now — "
                    "only valid when your stack is low (danger_level 'safe')",
                "flat_build": "Keep the surface flat/low with no immediate clear intent",
                "hole_avoidance": "Placed specifically to avoid creating a hole, even if "
                    "it's not the lowest resting spot",
                "attack": "Prioritize sending garbage to the opponent over your own board's "
                    "safety",
                "survival": "Your own stack is dangerously high — prioritize not topping "
                    "out over anything else",
            },
        },
        "aggression": {
            "type": "score",
            "instructions": "How aggressive is this placement, in terms of prioritizing "
                             "attacking the opponent over your own board's safety?",
            "criteria": [
                "Very defensive — pure survival, no attack intent",
                "Defensive — cautious, safe placement only",
                "Balanced — reasonable mix of safety and offense",
                "Aggressive — actively building toward a big clear",
                "Very aggressive — all-in on maximizing garbage sent",
            ],
        },
        "should_take_risk": {
            "type": "noul",
            "instructions":
                "Does this placement risk creating a new hole or raising the stack "
                "dangerously high? true if risky, false if clearly safe.",
        },
        "confidence_in_win": {
            "type": "noul",
            "instructions":
                "Your overall probability of winning this match from the current position "
                "(0 = losing for sure, 1 = winning for sure), based on both boards.",
        },
    }


def _pick_from_response(shortlist, response):
    answers = response.get("answers", {}) if isinstance(response, dict) else {}
    pm = answers.get("placement", {})
    try:
        idx = int(str(pm.get("choice", "m0")).lstrip("m"))
    except (ValueError, AttributeError):
        idx = 0
    idx = max(0, min(idx, len(shortlist) - 1))
    return shortlist[idx]


def decide_placement(client, letter, hold_letter, next_letters, board, opp_board,
                      own_stats, opp_stats, pending_garbage, deadline=None,
                      on_late_result=None):
    """Full decision pipeline for one piece. Returns
    (chosen_candidate, request, response, used_fallback) — chosen_candidate is None
    only if the piece has no legal placement at all (board topped out).

    If `deadline` (seconds) is given, the backend call is made on a worker thread and
    only waited on for that long: a request that's still running past the deadline is a
    rare upstream latency spike (occasionally 10-15s+ even though the normal p90 is
    ~1s — see DEPLOY.md/README notes) that would otherwise freeze this piece for however
    long it takes. Past the deadline we immediately fall back to the best candidate by
    our own heuristic_score instead of blocking the game on it. The real call keeps
    running in the background; if/when it eventually finishes, `on_late_result(candidate,
    request, response, elapsed)` is invoked (purely for logging/comparison — the fallback
    has already been used for the actual move, this can't retroactively change it)."""
    candidates = enumerate_placements(board, letter)
    if not candidates:
        return None, None, None, False

    own_max_h = max(column_heights(board), default=0)
    danger = _danger_level(own_max_h)
    ranked = sorted(candidates, key=heuristic_score, reverse=True)
    fallback = ranked[0]
    shortlist = prune_placements(candidates, danger=danger)
    random.shuffle(shortlist)  # avoid positional bias, mirrors army-chess

    state = build_state(letter, hold_letter, next_letters, board, opp_board,
                         own_stats, opp_stats, pending_garbage)
    questions = build_questions()
    questions["placement"]["criteria"] = {
        f"m{i}": compact_label(c) for i, c in enumerate(shortlist)
    }
    request = {"state": state, "questions": questions}

    if deadline is None:
        response = client.system_one(state=state, questions=questions)
        return _pick_from_response(shortlist, response), request, response, False

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
                on_late_result(_pick_from_response(shortlist, resp), request, resp,
                                time.time() - t0)
            future.add_done_callback(_finish)
        return fallback, request, {"fallback": "deadline_exceeded", "deadline": deadline}, True
    except Exception as e:
        return fallback, request, {"error": repr(e)}, True
    return _pick_from_response(shortlist, response), request, response, False

"""Flask server for the simplified army chess game with Jev AI opponent."""

import json
import os
import random
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from game import (
    IMMOVABLE,
    ROWS,
    COLS,
    TERRAIN,
    adjacent,
    all_formations,
    battle,
    build_jev_questions,
    build_jev_state,
    execute_move,
    in_bounds,
    movable_pieces,
    new_game,
    new_game_for_setup,
    possible_moves,
    save_current_setup_as_formation,
    setup_apply_preset,
    setup_board_view,
    setup_place,
    setup_remove,
    setup_start,
    view_for_owner,
)
from jev_client import JevClient
import experience


def load_dotenv():
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


load_dotenv()

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
# Pick up template edits without a server restart (Jinja caches them otherwise).
app.config["TEMPLATES_AUTO_RELOAD"] = True

# Per-client game state. The frontend generates a random id per browser tab
# (kept in sessionStorage) and sends it as the X-Game-Id header, so each tab
# — and each test script — gets its own isolated game. Switching tabs or
# another session hitting /api/reset can no longer clobber a live game.
# Requests without the header share the "default" game.
GAMES = {}
MAX_GAMES = 200
GAME_TTL = 12 * 3600  # evict games untouched for 12h

# Local experience file (win-rate stats per game phase / move purpose / aggression).
# Global across all games — that's the whole point of the experience layer.
WEIGHTS = experience.load()

# Jev client (lazy init so app still starts without key)
_JEV = None


def get_jev():
    global _JEV
    if _JEV is None:
        try:
            _JEV = JevClient()
        except ValueError as e:
            print(f"[warn] Jev client disabled: {e}", file=sys.stderr)
            _JEV = False  # sentinel
    return _JEV or None


def _new_ctx():
    return {
        "game": new_game_for_setup(),
        "exp_buffer": experience.new_buffer(),
        "exp_finalized": False,
        "last_seen": time.time(),
    }


def get_ctx():
    """Return the per-client game context for this request's X-Game-Id."""
    gid = request.headers.get("X-Game-Id") or "default"
    ctx = GAMES.get(gid)
    if ctx is None:
        # Opportunistic cleanup of stale games so GAMES can't grow forever.
        cutoff = time.time() - GAME_TTL
        for k in [k for k, v in GAMES.items() if v["last_seen"] < cutoff]:
            del GAMES[k]
        if len(GAMES) >= MAX_GAMES:
            oldest = min(GAMES, key=lambda k: GAMES[k]["last_seen"])
            del GAMES[oldest]
        ctx = _new_ctx()
        GAMES[gid] = ctx
    ctx["last_seen"] = time.time()
    return ctx


def reset_ctx(ctx):
    fresh = _new_ctx()
    ctx.update(fresh)


def current_phase(game):
    ai_count = sum(1 for p in game["board"].values() if p["owner"] == "ai" and p["alive"])
    enemy_count = sum(1 for p in game["board"].values() if p["owner"] == "player" and p["alive"])
    return experience.game_phase(ai_count, enemy_count)


def maybe_finalize_experience(ctx):
    """Persist local experience stats exactly once when the game ends."""
    game = ctx["game"]
    if game.get("winner") and not ctx["exp_finalized"]:
        experience.finalize_game(WEIGHTS, ctx["exp_buffer"], ai_won=(game["winner"] == "ai"))
        ctx["exp_finalized"] = True


# ---------- AI move logic ----------

# Jev's response latency scales with how many "choice" options it has to
# score in one call. Rail sliding can legally produce 60-100+ candidate
# moves on a busy turn, which makes the call noticeably slower. We ask a
# small rule-based filter to narrow that down to a sane shortlist before
# handing it to Jev — every capture is kept (those are the decisions that
# matter most), plus one move per otherwise-idle piece for diversity.
MAX_MOVE_OPTIONS = 14


def enumerate_ai_moves(game):
    """Return list of all legal AI moves as (from_pos, to_pos) tuples."""
    moves = []
    for fp in movable_pieces(game, "ai"):
        for tp in possible_moves(game, "ai", fp):
            moves.append((fp, tp))
    return moves


def prune_moves(game, moves, max_options=MAX_MOVE_OPTIONS):
    """Cut down a large legal-move list to a shortlist Jev can score quickly,
    keeping every capture and one representative move per other piece."""
    if len(moves) <= max_options:
        return moves

    attacks = [m for m in moves if game_at(game, m[1]) and game_at(game, m[1])["alive"]]
    non_attacks = [m for m in moves if m not in attacks]

    if len(attacks) >= max_options:
        random.shuffle(attacks)
        return attacks[:max_options]

    remaining = max_options - len(attacks)
    by_piece = {}
    for m in non_attacks:
        by_piece.setdefault(m[0], []).append(m)
    reps = [random.choice(lst) for lst in by_piece.values()]
    random.shuffle(reps)

    chosen = reps[:remaining]
    pruned = attacks + chosen
    if len(pruned) < max_options:
        leftover = [m for m in non_attacks if m not in chosen]
        random.shuffle(leftover)
        pruned += leftover[: max_options - len(pruned)]
    random.shuffle(pruned)
    return pruned


def build_move_choice_criteria(game, moves):
    """Build a Choice criteria dict from a list of legal moves.

    Each label is annotated with what the move actually *does* terrain-wise
    (enters/leaves a camp, is a diagonal camp-hub hop, rail-slides, or gets
    stuck in HQ) so Jev doesn't have to re-derive that from raw coordinates —
    this is what lets it actually weigh camp protection/support moves.
    """
    criteria = {}
    for fp, tp in moves:
        piece = game_at(game=game, pos=fp)
        target_piece = game_at(game=game, pos=tp)
        label = f"{piece['type']}({fp[0]},{fp[1]}) → ({tp[0]},{tp[1]})"

        if target_piece and target_piece["owner"] == "player":
            label += " [attack]"
        else:
            label += " [move]"

        from_terrain = TERRAIN[fp]["kind"]
        to_terrain = TERRAIN[tp]["kind"]
        distance = abs(fp[0] - tp[0]) + abs(fp[1] - tp[1])
        is_diagonal = fp[0] != tp[0] and fp[1] != tp[1]

        if to_terrain == "camp":
            label += " [→camp: becomes UNATTACKABLE]"
        if from_terrain == "camp" and to_terrain != "camp":
            label += " [leaves camp: gives up safety]"
        if is_diagonal:
            label += " [diagonal camp-hub hop]"
        if to_terrain == "hq":
            label += " [→HQ: gets stuck there forever]"
        if not is_diagonal and distance > 1:
            label += " [rail slide]"

        criteria[f"m{len(criteria)}"] = label
    return criteria


def game_at(game, pos):
    return game["board"].get(pos)


def jev_choose_ai_move(ctx):
    """Call Jev to choose AI's move. Returns dict with move + probs, or error info."""
    game = ctx["game"]
    moves = enumerate_ai_moves(game)
    if not moves:
        return {"error": "no-legal-moves"}
    moves = prune_moves(game, moves)

    client = get_jev()
    if client is None:
        return {"error": "jev-disabled", "moves": moves}

    phase = current_phase(game)
    state = build_jev_state(game, "ai")
    hint = experience.build_hint_text(WEIGHTS, phase)
    if hint:
        state["experience_notes"] = hint
    questions = build_jev_questions()
    questions["primary_move"]["criteria"] = build_move_choice_criteria(game, moves)

    # Full request dump so the prompt can be reviewed/tuned from the console.
    print(f"\n{'=' * 20} JEV REQUEST · turn {game['turn']} {'=' * 20}",
          flush=True)
    print(json.dumps({"state": state, "questions": questions},
                     ensure_ascii=False, indent=2), flush=True)

    try:
        resp = client.system_one(state=state, questions=questions)
    except Exception as e:
        print(f"[warn] Jev call failed: {e}", file=sys.stderr)
        return {"error": f"jev-call-failed: {e}", "moves": moves}

    print(f"{'=' * 20} JEV RESPONSE {'=' * 20}", flush=True)
    print(json.dumps(resp, ensure_ascii=False, indent=2), flush=True)
    print("=" * 60, flush=True)

    answers = resp.get("answers", {})
    pm = answers.get("primary_move", {})
    chosen = pm.get("choice", "m0")
    probabilities = pm.get("probabilities", {})
    confidence = pm.get("confidence", 0)

    purpose = answers.get("move_purpose", {}).get("choice", "exploration")
    aggression = answers.get("aggression", {}).get("score", 0)
    experience.record_ai_turn(ctx["exp_buffer"], purpose, aggression, phase)

    # Map "m0", "m1" etc back to (from, to)
    try:
        idx = int(chosen.lstrip("m"))
    except (ValueError, AttributeError):
        idx = 0
    idx = max(0, min(idx, len(moves) - 1))
    fp, tp = moves[idx]

    return {
        "ok": True,
        "from": list(fp),
        "to": list(tp),
        "chosen_idx": idx,
        "probabilities": {k: float(v) for k, v in probabilities.items()},
        "confidence": float(confidence),
        "purpose": answers.get("move_purpose", {}).get("choice", "unknown"),
        "aggression": answers.get("aggression", {}).get("score", 0),
        "take_risk": answers.get("should_take_risk", {}).get("noul", 0),
        "win_conf": answers.get("confidence_in_win", {}).get("noul", 0),
        "all_answers": answers,
        # Raw request/response for the right-panel Jev I/O viewer.
        "jev_io": {
            "request": {"state": state, "questions": questions},
            "response": resp,
        },
    }


# ---------- Flask routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET"])
def get_state():
    game = get_ctx()["game"]
    board = setup_board_view(game, "player") if game.get("phase") == "setup" \
        else view_for_owner(game, "player")
    state = {
        "phase": game.get("phase"),
        "board": board,
        "turn": game["turn"],
        "winner": game["winner"],
        "last_ai_move": game["log"][-1] if game["log"] else None,
        "ai_probs": game.get("last_ai_probs"),
        "awaiting_ai": bool(game.get("awaiting_ai")),
    }
    if game.get("phase") == "setup":
        state["setup"] = {
            "placed": game["setup"]["player"],
            "pool": game["setup_pool"]["player"],
            "preset": game.get("setup_preset", {}).get("player"),
        }
        state["formations"] = [
            {"name": f["name"], "desc": f["desc"], "custom": f.get("custom", False)}
            for f in all_formations()
        ]
    return jsonify(state)


@app.route("/api/moves", methods=["POST"])
def get_moves():
    """Return list of legal moves for the player's piece at the given pos."""
    data = request.get_json(force=True) or {}
    pos = tuple(data.get("pos", [-1, -1]))
    moves = possible_moves(get_ctx()["game"], "player", pos)
    return jsonify({
        "from": list(pos),
        "moves": [list(m) for m in moves],
    })


def _experience_fields():
    return {
        "games_played": WEIGHTS["games_played"],
        "wins": WEIGHTS["wins"],
        "losses": WEIGHTS["losses"],
    }


@app.route("/api/move", methods=["POST"])
def player_move():
    """Player move only — resolves combat locally and returns immediately.
    The AI move is fetched separately via /api/ai_move so the player's own
    result animates instantly instead of waiting on the Jev call."""
    ctx = get_ctx()
    game = ctx["game"]
    if game.get("phase") == "setup":
        return jsonify({"ok": False, "error": "complete setup first"}), 400
    if game.get("phase") == "ended":
        return jsonify({"ok": False, "error": "game already ended"}), 400
    if game.get("awaiting_ai"):
        return jsonify({"ok": False, "error": "ai move still pending"}), 400
    data = request.get_json(force=True) or {}
    from_pos = tuple(data.get("from"))
    to_pos = tuple(data.get("to"))
    if None in (from_pos, to_pos) or len(from_pos) != 2 or len(to_pos) != 2:
        return jsonify({"ok": False, "error": "invalid pos"}), 400

    result = execute_move(game, "player", from_pos, to_pos)
    if not result.get("ok"):
        return jsonify(result), 400

    game["awaiting_ai"] = not game["winner"]
    maybe_finalize_experience(ctx)
    return jsonify({
        "ok": True,
        "winner": game["winner"],
        "event": result["event"],
        "turn": game["turn"],
        "phase": game.get("phase"),
        "awaiting_ai": game["awaiting_ai"],
        "experience": _experience_fields(),
        "board": view_for_owner(game, "player"),
    })


@app.route("/api/ai_move", methods=["POST"])
def ai_move():
    """Ask Jev for the AI's move and execute it. Called by the frontend after
    the player's own move has been shown."""
    ctx = get_ctx()
    game = ctx["game"]
    if not game.get("awaiting_ai"):
        return jsonify({"ok": False, "error": "no ai move pending"}), 400
    game["awaiting_ai"] = False

    response = {"ok": True}
    ai_decision = jev_choose_ai_move(ctx)
    if ai_decision.get("ok"):
        game["last_ai_probs"] = ai_decision
        ai_result = execute_move(game, "ai",
                                 tuple(ai_decision["from"]),
                                 tuple(ai_decision["to"]))
        if ai_result.get("ok"):
            response["ai_move"] = {
                "decision": ai_decision,
                "event": ai_result["event"],
                "winner": game["winner"],
            }
        else:
            response["ai_move_error"] = ai_result.get("error")
    else:
        # Jev unavailable — surface error to UI instead of silent random
        response["ai_unavailable"] = ai_decision.get("error", "unknown")

    response["turn"] = game["turn"]
    response["winner"] = game["winner"]
    maybe_finalize_experience(ctx)
    response["experience"] = _experience_fields()
    response["board"] = view_for_owner(game, "player")
    return jsonify(response)


@app.route("/api/reset", methods=["POST"])
def reset():
    ctx = get_ctx()
    reset_ctx(ctx)
    return jsonify({"ok": True, "phase": ctx["game"].get("phase")})


@app.route("/api/setup/place", methods=["POST"])
def setup_place_endpoint():
    data = request.get_json(force=True) or {}
    pos = tuple(data.get("pos"))
    ptype = data.get("type")
    if pos is None or len(pos) != 2 or not ptype:
        return jsonify({"ok": False, "error": "invalid args"}), 400
    game = get_ctx()["game"]
    result = setup_place(game, "player", pos, ptype)
    if not result.get("ok"):
        return jsonify(result), 400
    result["setup"] = {
        "placed": game["setup"]["player"],
        "pool": game["setup_pool"]["player"],
    }
    return jsonify(result)


@app.route("/api/setup/remove", methods=["POST"])
def setup_remove_endpoint():
    data = request.get_json(force=True) or {}
    pos = tuple(data.get("pos"))
    if pos is None or len(pos) != 2:
        return jsonify({"ok": False, "error": "invalid args"}), 400
    game = get_ctx()["game"]
    result = setup_remove(game, "player", pos)
    if not result.get("ok"):
        return jsonify(result), 400
    result["setup"] = {
        "placed": game["setup"]["player"],
        "pool": game["setup_pool"]["player"],
    }
    return jsonify(result)


@app.route("/api/experience", methods=["GET"])
def experience_endpoint():
    """Expose the local experience/weights file for inspection."""
    return jsonify(WEIGHTS)


@app.route("/api/setup/preset", methods=["POST"])
def setup_preset_endpoint():
    data = request.get_json(force=True) or {}
    idx = data.get("index")
    if idx is None:
        return jsonify({"ok": False, "error": "missing index"}), 400
    game = get_ctx()["game"]
    result = setup_apply_preset(game, "player", int(idx))
    if not result.get("ok"):
        return jsonify(result), 400
    result["setup"] = {
        "placed": game["setup"]["player"],
        "pool": game["setup_pool"]["player"],
        "preset": game.get("setup_preset", {}).get("player"),
    }
    return jsonify(result)


@app.route("/api/setup/save_custom", methods=["POST"])
def setup_save_custom_endpoint():
    data = request.get_json(force=True) or {}
    name = data.get("name", "")
    result = save_current_setup_as_formation(get_ctx()["game"], "player", name)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/setup/start", methods=["POST"])
def setup_start_endpoint():
    result = setup_start(get_ctx()["game"])
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Army Chess demo running on http://127.0.0.1:{port}")
    print(f"  base URL: {os.environ.get('KNOX_BASE_URL', 'https://api.knox.chat/v1')}")
    print(f"  Jev enabled: {get_jev() is not None}")
    app.run(host="127.0.0.1", port=port, debug=False)
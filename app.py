"""Flask server for the simplified army chess game with Jev AI opponent."""

import json
import os
import random
import re
import sys
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, render_template, request

from game import (
    HQ_CELLS,
    IMMOVABLE,
    RANK,
    ROWS,
    COLS,
    TERRAIN,
    adjacent,
    all_formations,
    battle,
    build_jev_questions,
    build_jev_state,
    execute_move,
    full_board_view,
    in_bounds,
    tactical_override,
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
    auto_fill_setup,
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

# Decision backends: "jev" (cloud API) or "laya" (local open-source model —
# same state+questions -> typed answers contract, no API key, no per-move
# network call). Each game picks its own via the X-AI-Backend header or the
# setup payload; AI_BACKEND env sets the default.
AI_BACKEND = os.environ.get("AI_BACKEND", "jev").lower()
# Laya needs torch + a 322M model (~1.4GB RSS) — too heavy for small cloud
# servers. Set LAYA_ENABLED=0 to hide the option and reject laya games.
LAYA_ENABLED = os.environ.get("LAYA_ENABLED", "1") != "0"

# Lazily-initialised clients keyed by backend name; False = failed init.
# The lock also guards against concurrent first-use: importing transformers
# mid-import in a second thread raises spurious "cannot import name" errors.
_CLIENTS = {}
_CLIENT_LOCK = threading.Lock()


def get_client(backend=None):
    backend = (backend or AI_BACKEND).lower()
    if backend not in ("jev", "laya"):
        backend = "jev"
    if backend == "laya" and not LAYA_ENABLED:
        return None
    with _CLIENT_LOCK:
        c = _CLIENTS.get(backend)
        if c is None:
            try:
                if backend == "laya":
                    from laya_client import LayaClient
                    c = LayaClient()
                else:
                    c = JevClient()
            except Exception as e:
                print(f"[warn] {backend} backend disabled: {e}", file=sys.stderr)
                c = False  # sentinel
            _CLIENTS[backend] = c
        return c or None


def get_jev():
    return get_client()


def _new_ctx():
    game = new_game_for_setup()
    # Pre-fill BOTH sides with presets: the human side sees a ready-made
    # formation it can tweak (or just click 开始), AI sides get overwritten
    # by a perturbed preset at setup_start anyway.
    setup_apply_preset(game, "ai", random.randrange(len(all_formations())))
    return {
        "game": game,
        "exp_buffer": experience.new_buffer(),
        "exp_finalized": False,
        "last_seen": time.time(),
        # Who controls each side: "human", "jev", or "laya".
        # ai = 蓝方/上方/先手, player = 红方/下方/后手.
        "controllers": {"player": AI_BACKEND, "ai": "human"},
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
        ctx["game_id"] = gid
        GAMES[gid] = ctx
    for side, hdr in (("player", "X-Ctl-Player"), ("ai", "X-Ctl-Ai")):
        v = request.headers.get(hdr)
        if v in ("human", "jev", "laya") and (v != "laya" or LAYA_ENABLED):
            ctx["controllers"][side] = v
    ctx["last_seen"] = time.time()
    return ctx


def reset_ctx(ctx):
    controllers = ctx.get("controllers")  # keep side assignments across resets
    fresh = _new_ctx()
    ctx.update(fresh)
    if controllers:
        ctx["controllers"] = controllers


def side_to_move(game):
    """蓝方 (ai side, top) moves first; strict alternation after that."""
    return "ai" if game["turn"] % 2 == 0 else "player"


def setup_owner(ctx):
    """Which side the setup UI edits — the human-controlled side (the
    first-moving blue/ai side wins ties in hotseat). None = both AI."""
    c = ctx["controllers"]
    if c.get("ai") == "human":
        return "ai"
    if c.get("player") == "human":
        return "player"
    return None


def board_view_for(ctx):
    """Which board view this client should see: a human side's hidden-info
    view, or the god view when both sides are AI (spectator mode)."""
    game = ctx["game"]
    if game.get("phase") == "setup":
        return setup_board_view(game, setup_owner(ctx) or "ai")
    human = [s for s in ("player", "ai") if ctx["controllers"].get(s) == "human"]
    if not human:
        return full_board_view(game)          # spectating an AI-vs-AI match
    if len(human) == 2:
        return view_for_owner(game, side_to_move(game))   # hotseat
    return view_for_owner(game, human[0])


def current_phase(game):
    ai_count = sum(1 for p in game["board"].values() if p["owner"] == "ai" and p["alive"])
    enemy_count = sum(1 for p in game["board"].values() if p["owner"] == "player" and p["alive"])
    return experience.game_phase(ai_count, enemy_count)


def maybe_finalize_experience(ctx):
    """Persist local experience stats exactly once when the game ends."""
    game = ctx["game"]
    if (game.get("winner") and not ctx["exp_finalized"]
            and ctx["controllers"].get("ai") != "human"):
        experience.finalize_game(WEIGHTS, ctx["exp_buffer"],
                                 ai_won=(game["winner"] == "ai"),
                                 draw=(game["winner"] == "draw"))
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


def _predict_score(att_type, known_label):
    """Rough value of attacking a defender labelled `known_label`
    (exact type, deduction marker, or None = unknown)."""
    if not known_label:
        return 1.5                              # face-down defender — a probe
    if known_label in RANK or known_label in IMMOVABLE or known_label == "炸弹":
        out = battle(att_type, known_label)[0]
        return {"attacker_wins": 3.0, "flag_taken_player": 4.0,
                "both_die": 2.0, "defender_wins": 0.0}.get(out, 0.0)
    if isinstance(known_label, str):
        if known_label == "地雷":
            return 3.0 if att_type == "工兵" else 0.0
        if known_label == "军旗":
            return 4.0
        if known_label == "非地雷":
            return 1.6                          # combat piece, unknown rank
        if known_label.startswith(">"):
            base = known_label[1:].split("或")[0]
            vr = RANK.get(base, 0)
            if "或雷" in known_label and att_type == "工兵":
                return 2.0                      # might be a mine we can defuse
            return 2.5 if RANK.get(att_type, 0) > vr else 0.3
        if known_label in ("同级或炸弹", "工兵或炸弹"):
            return 1.0
    return 1.5


def _na_score(game, move, owner, opponent):
    """Heuristic for a non-attack move: forward progress toward the enemy
    flag, camp shelter for revealed pieces, plan continuity, then jitter."""
    fp, tp = move
    piece = game["board"][fp]
    s = 0.0
    fwd = (tp[0] - fp[0]) if owner == "ai" else (fp[0] - tp[0])
    s += fwd                                    # toward the enemy flag row
    if TERRAIN[tp]["kind"] == "camp":
        # revealed pieces gain more from camp shelter than hidden ones
        s += 0.8 if game["revealed_to"][opponent].get(f"{fp[0]},{fp[1]}") else 0.3
    log = game["log"]
    if len(log) >= 2 and log[-1]["actor"] == opponent:
        prev = log[-2]                          # our previous move
        if tuple(prev.get("from", ())) == fp or tuple(prev.get("to", ())) == fp:
            s += 0.7                            # keep pushing the same plan
    for ev in reversed(log):
        if ev["actor"] == owner:
            # Moving a piece straight back to where it came from is the
            # classic aimless shuffle — heavy penalty (kept, just ranked last)
            if fp == tuple(ev["to"]) and tp == tuple(ev["from"]):
                s -= 2.5
            break
    s += random.random() * 0.3                  # tie-breaker jitter
    return s


def prune_moves(game, moves, max_options=MAX_MOVE_OPTIONS, owner="ai"):
    """Cut a large legal-move list to a shortlist the model can score.

    Priority tiers (per user spec): capture/defend the flag first, then
    attacks ordered by predicted value, then directional non-attacks —
    one representative per piece, ranked by a small heuristic."""
    if len(moves) <= max_options:
        return moves

    opponent = "player" if owner == "ai" else "ai"
    known = game["revealed_to"][owner]

    # Squares where the enemy flag is known/deduced to sit, plus both enemy
    # HQ squares — stepping onto an unknown HQ is a flag-capture attempt.
    flag_sq = {tuple(int(x) for x in k.split(","))
               for k, v in known.items() if v == "军旗"} | set(HQ_CELLS[opponent])

    # Enemy pieces that can take our flag next turn — capturing them is the
    # highest-priority defensive move in the shortlist.
    my_flag = next((p for p, pc in game["board"].items()
                    if pc["owner"] == owner and pc["type"] == "军旗"
                    and pc["alive"]), None)
    threats = set()
    if my_flag is not None:
        for ep, pc in game["board"].items():
            if (pc["owner"] == opponent and pc["alive"]
                    and my_flag in possible_moves(game, opponent, ep)):
                threats.add(ep)

    flag_cap, defense, attacks, others = [], [], [], []
    for m in moves:
        fp, tp = m
        tgt = game_at(game, tp)
        if tp in flag_sq or (tgt and tgt["type"] == "军旗"):
            flag_cap.append(m)
        elif tp in threats:
            defense.append((_predict_score(game["board"][fp]["type"],
                                           known.get(f"{tp[0]},{tp[1]}")), m))
        elif tgt and tgt["owner"] == opponent:
            attacks.append((_predict_score(game["board"][fp]["type"],
                                           known.get(f"{tp[0]},{tp[1]}")), m))
        else:
            others.append(m)

    defense.sort(key=lambda x: -x[0])
    attacks.sort(key=lambda x: -x[0])
    keep = flag_cap + [m for _, m in defense] + [m for _, m in attacks]
    keep = keep[:max_options]

    if len(keep) < max_options:
        by_piece = {}
        for m in others:
            by_piece.setdefault(m[0], []).append(m)
        reps = []
        for fp, lst in by_piece.items():
            lst.sort(key=lambda m: -_na_score(game, m, owner, opponent))
            reps.append(lst[0])
        reps.sort(key=lambda m: -_na_score(game, m, owner, opponent))
        keep += reps[:max_options - len(keep)]
        if len(keep) < max_options:
            rest = sorted((m for m in others if m not in keep),
                          key=lambda m: -_na_score(game, m, owner, opponent))
            keep += rest[:max_options - len(keep)]

    random.shuffle(keep)
    return keep


def build_move_choice_criteria(game, moves, owner="ai"):
    """Build a Choice criteria dict from a list of legal moves.

    Each label is annotated with what the move actually *does* terrain-wise
    (enters/leaves a camp, is a diagonal camp-hub hop, rail-slides, or gets
    stuck in HQ) so Jev doesn't have to re-derive that from raw coordinates —
    this is what lets it actually weigh camp protection/support moves.
    `owner` = the side choosing, so attack annotations read that side's
    reveal intel (used by the head-to-head match runner for both backends).
    """
    enemy = "player" if owner == "ai" else "ai"
    criteria = {}
    for fp, tp in moves:
        piece = game_at(game=game, pos=fp)
        target_piece = game_at(game=game, pos=tp)
        label = f"{piece['type']}({fp[0]},{fp[1]}) → ({tp[0]},{tp[1]})"

        if target_piece and target_piece["owner"] == enemy:
            # Annotate what is KNOWN or DEDUCED about the defender so Jev can
            # avoid suicidal attacks — without this the options all look the
            # same and it will happily feed a 排长 into a revealed 司令.
            known = game["revealed_to"][owner].get(f"{tp[0]},{tp[1]}")
            if known and (known in RANK or known in IMMOVABLE or known == "炸弹"):
                predicted = battle(piece["type"], known)[0]
                tag = {
                    "attacker_wins": "必胜",
                    "defender_wins": "必死",
                    "both_die": "同归于尽",
                    "flag_taken_player": "夺旗获胜!",
                }.get(predicted, predicted)
                label += f" [attack: vs {known} → {tag}]"
            elif known:
                label += f" [attack: vs deduced {known}]"
            else:
                label += " [attack: vs ?]"
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


def model_choose_move(ctx, side):
    """Ask the side's configured backend for a move. Returns dict with move
    + probs, or error info. `side` is "player" or "ai"."""
    game = ctx["game"]
    moves = [(fp, tp) for fp in movable_pieces(game, side)
             for tp in possible_moves(game, side, fp)]
    if not moves:
        return {"error": "no-legal-moves"}
    moves = prune_moves(game, moves, owner=side)

    client = get_client(ctx["controllers"].get(side))
    if client is None:
        return {"error": "backend-disabled", "moves": moves}

    phase = current_phase(game)
    state = build_jev_state(game, side)
    hint = experience.build_hint_text(WEIGHTS, phase)
    if hint:
        state["experience_notes"] = hint
    questions = build_jev_questions()
    questions["primary_move"]["criteria"] = build_move_choice_criteria(
        game, moves, owner=side)

    # Full request dump so the prompt can be reviewed/tuned from the console.
    print(f"\n{'=' * 20} MODEL REQUEST · turn {game['turn']} side={side} {'=' * 20}",
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
    ctx = get_ctx()
    game = ctx["game"]
    state = {
        "phase": game.get("phase"),
        "board": board_view_for(ctx),
        "controllers": ctx["controllers"],
        "laya_enabled": LAYA_ENABLED,
        "side_to_move": side_to_move(game) if not game.get("winner") else None,
        "turn": game["turn"],
        "winner": game["winner"],
        "last_ai_move": game["log"][-1] if game["log"] else None,
        "ai_probs": game.get("last_ai_probs"),
        "awaiting_ai": bool(game.get("awaiting_ai")),
    }
    if game.get("phase") == "setup":
        owner = setup_owner(ctx)
        state["setup"] = {
            "owner": owner,
            "placed": game["setup"][owner] if owner else {},
            "pool": game["setup_pool"][owner] if owner else [],
            "preset": game.get("setup_preset", {}).get(owner) if owner else None,
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
    ctx = get_ctx()
    moves = possible_moves(ctx["game"], side_to_move(ctx["game"]), pos)
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


# === Replay frames: god-view snapshots of the board after every move ======
# Persisted per game_id so a replay survives a page refresh or even a
# server restart.

REPLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays")

OUTCOME_ZH = {
    "attacker_wins": "攻方胜",
    "defender_wins": "守方胜",
    "both_die": "同归于尽",
    "flag_taken_player": "玩家夺旗",
    "flag_taken_ai": "AI夺旗",
}


def _replay_path(game_id):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", game_id or "default")[:64]
    return os.path.join(REPLAY_DIR, safe + ".json")


@app.route("/replay")
def replay_page():
    return render_template("replay.html")


@app.route("/api/replays", methods=["GET"])
def list_replays():
    """List saved replay files (live games and head-to-head matches)."""
    files = []
    try:
        for name in os.listdir(REPLAY_DIR):
            if not name.endswith(".json"):
                continue
            path = os.path.join(REPLAY_DIR, name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                files.append({
                    "name": name,
                    "frames": len(data.get("frames", [])),
                    "winner": data.get("winner"),
                    "meta": data.get("meta"),
                    "mtime": os.path.getmtime(path),
                })
            except (OSError, json.JSONDecodeError):
                continue
    except OSError:
        pass
    files.sort(key=lambda x: -x["mtime"])
    return jsonify({"files": files})


@app.route("/api/replay_file", methods=["GET"])
def replay_file():
    """Return one saved replay file by basename."""
    name = os.path.basename(request.args.get("name", ""))
    if not name.endswith(".json"):
        return jsonify({"error": "not found"}), 404
    path = os.path.join(REPLAY_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    with open(path, encoding="utf-8") as f:
        return jsonify(json.load(f))


def _frame_label(actor, event, controllers=None):
    if controllers:
        who = controllers.get(actor, actor)
        who += "（下·先手）" if actor == "player" else "（上·后手）"
    else:
        who = "玩家" if actor == "player" else "AI"
    s = f"{who} ({event['from'][0]},{event['from'][1]})→({event['to'][0]},{event['to'][1]})"
    if event.get("combat"):
        s += f" 战斗:{OUTCOME_ZH.get(event.get('outcome'), event.get('outcome'))}"
    if event.get("flag_deduced"):
        s += " 排除大本营→定位军旗"
    if event.get("flag_revealed"):
        s += " 亮旗"
    return s


def append_replay_frame(ctx, label):
    game = ctx["game"]
    game.setdefault("replay", []).append({
        "turn": game["turn"],
        "label": label,
        "board": full_board_view(game),
    })
    try:
        os.makedirs(REPLAY_DIR, exist_ok=True)
        ctl = ctx.get("controllers", {})
        with open(_replay_path(ctx["game_id"]), "w", encoding="utf-8") as f:
            json.dump({"frames": game["replay"], "winner": game.get("winner"),
                       "meta": {"player_side": ctl.get("player"),
                                "ai_side": ctl.get("ai")}},
                      f, ensure_ascii=False)
    except OSError:
        pass


@app.route("/api/move", methods=["POST"])
def player_move():
    """Human move — resolves combat locally and returns immediately.
    AI turns are fetched separately via /api/turn_move so the human's own
    result animates instantly instead of waiting on the model call."""
    ctx = get_ctx()
    game = ctx["game"]
    if game.get("phase") == "setup":
        return jsonify({"ok": False, "error": "complete setup first"}), 400
    if game.get("phase") == "ended":
        return jsonify({"ok": False, "error": "game already ended"}), 400
    side = side_to_move(game)
    if ctx["controllers"].get(side) != "human":
        return jsonify({"ok": False, "error": "not a human turn"}), 400
    data = request.get_json(force=True) or {}
    from_pos = tuple(data.get("from"))
    to_pos = tuple(data.get("to"))
    if None in (from_pos, to_pos) or len(from_pos) != 2 or len(to_pos) != 2:
        return jsonify({"ok": False, "error": "invalid pos"}), 400

    result = execute_move(game, side, from_pos, to_pos)
    if not result.get("ok"):
        return jsonify(result), 400

    append_replay_frame(ctx, _frame_label(side, result["event"], ctx["controllers"]))
    maybe_finalize_experience(ctx)
    return jsonify({
        "ok": True,
        "winner": game["winner"],
        "event": result["event"],
        "turn": game["turn"],
        "phase": game.get("phase"),
        "side_to_move": side_to_move(game) if not game["winner"] else None,
        "controllers": ctx["controllers"],
        "experience": _experience_fields(),
        "board": board_view_for(ctx),
    })


@app.route("/api/turn_move", methods=["POST"])
@app.route("/api/ai_move", methods=["POST"])   # legacy alias
def turn_move():
    """Execute one AI turn for whichever side is to move — the side's
    controller (jev/laya) comes from the game config."""
    ctx = get_ctx()
    game = ctx["game"]
    if game.get("phase") != "playing" or game.get("winner"):
        return jsonify({"ok": False, "error": "no game in progress"}), 400
    side = side_to_move(game)
    ctrl = ctx["controllers"].get(side, "human")
    if ctrl == "human":
        return jsonify({"ok": False, "error": "it's a human turn"}), 400
    if ctx.get("turn_in_flight"):
        return jsonify({"ok": False, "error": "ai move still pending"}), 400
    ctx["turn_in_flight"] = True
    try:
        response = {"ok": True}
        # Tactical priorities outrank the model: take the enemy flag if
        # possible, else save our own flag from an immediate capture threat.
        override = tactical_override(game, side)
        if override:
            ofp, otp, reason = override
            ai_decision = {
                "ok": True, "from": list(ofp), "to": list(otp),
                "purpose": reason, "confidence": 1.0,
                "aggression": 4, "take_risk": 0, "win_conf": 1.0,
                "probabilities": {}, "override": True,
            }
        else:
            ai_decision = model_choose_move(ctx, side)
        if ai_decision.get("ok"):
            game["last_ai_probs"] = ai_decision
            ai_result = execute_move(game, side,
                                     tuple(ai_decision["from"]),
                                     tuple(ai_decision["to"]))
            if ai_result.get("ok"):
                response["ai_move"] = {
                    "decision": ai_decision,
                    "event": ai_result["event"],
                    "winner": game["winner"],
                    "side": side,
                    "backend": ctrl,
                }
                append_replay_frame(
                    ctx, _frame_label(side, ai_result["event"], ctx["controllers"]))
            else:
                response["ai_move_error"] = ai_result.get("error")
        else:
            response["ai_unavailable"] = ai_decision.get("error", "unknown")

        response["turn"] = game["turn"]
        response["phase"] = game.get("phase")
        response["winner"] = game["winner"]
        response["side_to_move"] = side_to_move(game) if not game["winner"] else None
        response["controllers"] = ctx["controllers"]
        maybe_finalize_experience(ctx)
        response["experience"] = _experience_fields()
        response["board"] = board_view_for(ctx)
        return jsonify(response)
    finally:
        ctx["turn_in_flight"] = False


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
    ctx = get_ctx()
    game = ctx["game"]
    owner = setup_owner(ctx)
    if not owner:
        return jsonify({"ok": False, "error": "no human side"}), 400
    result = setup_place(game, owner, pos, ptype)
    if not result.get("ok"):
        return jsonify(result), 400
    result["setup"] = {
        "placed": game["setup"][owner],
        "pool": game["setup_pool"][owner],
    }
    return jsonify(result)


@app.route("/api/setup/remove", methods=["POST"])
def setup_remove_endpoint():
    data = request.get_json(force=True) or {}
    pos = tuple(data.get("pos"))
    if pos is None or len(pos) != 2:
        return jsonify({"ok": False, "error": "invalid args"}), 400
    ctx = get_ctx()
    game = ctx["game"]
    owner = setup_owner(ctx)
    if not owner:
        return jsonify({"ok": False, "error": "no human side"}), 400
    result = setup_remove(game, owner, pos)
    if not result.get("ok"):
        return jsonify(result), 400
    result["setup"] = {
        "placed": game["setup"][owner],
        "pool": game["setup_pool"][owner],
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
    ctx = get_ctx()
    game = ctx["game"]
    owner = setup_owner(ctx)
    if not owner:
        return jsonify({"ok": False, "error": "no human side"}), 400
    result = setup_apply_preset(game, owner, int(idx))
    if not result.get("ok"):
        return jsonify(result), 400
    result["setup"] = {
        "placed": game["setup"][owner],
        "pool": game["setup_pool"][owner],
        "preset": game.get("setup_preset", {}).get(owner),
    }
    return jsonify(result)


@app.route("/api/setup/save_custom", methods=["POST"])
def setup_save_custom_endpoint():
    data = request.get_json(force=True) or {}
    name = data.get("name", "")
    ctx = get_ctx()
    owner = setup_owner(ctx) or "player"
    result = save_current_setup_as_formation(ctx["game"], owner, name)
    if not result.get("ok"):
        return jsonify(result), 400
    return jsonify(result)


@app.route("/api/setup/start", methods=["POST"])
def setup_start_endpoint():
    ctx = get_ctx()
    data = request.get_json(force=True) or {}
    for side in ("player", "ai"):
        v = data.get(f"{side}_controller")
        if v in ("human", "jev", "laya"):
            ctx["controllers"][side] = v
    # AI-controlled sides get randomly perturbed presets inside setup_start;
    # the human side's hand-placed formation (if any) is validated there.
    # Warm the local model in the background while setup completes —
    # Laya's first call compiles kernels and would stall the first turn.
    if LAYA_ENABLED and "laya" in ctx["controllers"].values():
        threading.Thread(target=get_client, args=("laya",), daemon=True).start()
    result = setup_start(ctx["game"], setup_owner(ctx))
    if not result.get("ok"):
        return jsonify(result), 400
    # Frame 0 = the opening deployment (god view, all types visible).
    ctx["game"]["replay"] = []
    append_replay_frame(ctx, "开局")
    return jsonify(result)


@app.route("/api/replay", methods=["GET"])
def replay_endpoint():
    """All replay frames for this game — full-visibility board snapshots so
    the player can review what actually happened. Falls back to the on-disk
    replay file if this game session was restarted."""
    ctx = get_ctx()
    frames = ctx["game"].get("replay")
    if not frames:
        path = _replay_path(ctx["game_id"])
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    frames = json.load(f).get("frames", [])
                ctx["game"]["replay"] = frames
            except (json.JSONDecodeError, OSError):
                frames = []
    return jsonify({"frames": frames or []})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    print(f"Army Chess demo running on http://127.0.0.1:{port}")
    print(f"  base URL: {os.environ.get('KNOX_BASE_URL', 'https://api.knox.chat/v1')}")
    print(f"  decision backend: {AI_BACKEND} (enabled: {get_jev() is not None})")
    app.run(host="127.0.0.1", port=port, debug=False)
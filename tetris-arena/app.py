"""Flask server for competitive Tetris with Jev / Laya AI opponents.

Two boards per match ("left" and "right"). Each side is controlled by
"human" (real-time keyboard input via /api/action) or an AI backend
("jev"/"laya") — the AI decides a placement per piece via the same
state+questions -> answers contract army-chess uses (see ai_engine.py),
then the engine animates the piece dropping into place. Clearing lines
sends garbage to the opponent. Supports human-vs-AI and AI-vs-AI
(spectate Jev vs Laya) by simply setting both sides to AI backends.
"""

import json
import os
import random
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import ai_engine
import tetris
from tetris import Piece

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False
app.config["TEMPLATES_AUTO_RELOAD"] = True


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

LAYA_ENABLED = os.environ.get("LAYA_ENABLED", "1") != "0"

# Measured p50/p90 for a real backend call is ~0.9s/~1s, but a rare upstream latency
# spike can take 10-15s+ (see DEPLOY.md/README) — waiting that long would freeze a piece
# on-screen. Past this deadline we use our own heuristic's best candidate instead and let
# the real call finish in the background purely for comparison/logging.
AI_DECISION_DEADLINE = 2.0

TICK_INTERVAL = 0.04       # engine loop tick, seconds
LOCK_DELAY = 0.4           # grace period after a piece "lands" before it locks
FAST_DROP_STEP_ROWS = 1    # rows/tick during the AI's animated drop
MATCH_TTL = 3 * 3600
MAX_MATCHES = 100

REPLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays")
BACKEND_CN = {"human": "玩家", "jev": "Jev", "laya": "Laya"}
SIDE_CN = {"left": "左方", "right": "右方"}

# ---------- AI backend clients (shared singletons, lazily created) -------

_CLIENTS = {}
_CLIENT_LOCK = threading.Lock()


def _log_late_ai_result(backend, side_name, piece, candidate, elapsed):
    """Called (from a background thread) when a backend call that already missed
    AI_DECISION_DEADLINE finally finishes. The fallback was already applied to the game —
    this is purely a log line to see how often/how much the real answer would have
    differed, and how bad the upstream stall actually was."""
    label = ai_engine.describe_placement(candidate) if candidate else None
    print(f"[ai-timing] LATE backend={backend} side={side_name} piece={piece} "
          f"total_elapsed={elapsed:.2f}s would_have_picked={label!r} "
          f"(fallback already used for this piece)", flush=True)


def get_client(backend):
    backend = (backend or "").lower()
    if backend not in ("jev", "laya"):
        return None
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
                    from jev_client import JevClient
                    c = JevClient()
            except Exception as e:
                print(f"[warn] {backend} backend disabled: {e}", file=sys.stderr)
                c = False
            _CLIENTS[backend] = c
        return c or None


# ---------- Game state -----------------------------------------------

class Side:
    def __init__(self, controller):
        self.controller = controller          # "human" | "jev" | "laya"
        self.board = tetris.new_board()
        self.piece_index = 0     # how many pieces this side has drawn from the match's
                                  # SHARED sequence — see Match._next_letter/_peek_next.
        self.piece = None
        self.hold_letter = None
        self.hold_used = False
        self.score = 0
        self.lines = 0
        self.combo = 0
        self.pending_garbage = 0
        self.game_over = False
        self.landed = False
        self.landed_since = 0.0
        self.last_gravity = time.time()
        self.ai_thinking = False
        self.fast_drop = False
        self.fast_drop_target_row = None
        self.last_decision = None


class Match:
    def __init__(self, mid, left_ctl, right_ctl):
        self.id = mid
        self.sides = {"left": Side(left_ctl), "right": Side(right_ctl)}
        # ONE shared piece sequence for the whole match (fed by 7-bag), indexed
        # separately per side by Side.piece_index — so piece #1 is identical on both
        # boards, piece #2 is identical, etc., no matter which side is ahead/behind in
        # pace. This is the standard competitive-Tetris fairness rule (same as Tetris 99
        # / Puyo Puyo Tetris versus mode): nobody can win just by getting a luckier bag.
        self.rng = random.Random()
        self.shared_bag = []
        self.winner = None
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True
        self.last_seen = time.time()
        # Board-snapshot replay — one frame per piece lock (+ opening/closing frames),
        # persisted to disk as it goes so a replay survives a page refresh or even a
        # server restart (see REPLAY_DIR below). This is separate from the live in-memory
        # match state, which is still lost on restart — see DEPLOY.md known limitations.
        self.frames = []
        self.started_at = time.strftime("%Y%m%d-%H%M%S")
        # A fresh id PER MATCH (not per browser tab/gid!) — gid stays the same across
        # "再来一局" in the same tab, so keying the replay filename on gid alone made
        # every rematch silently overwrite the previous match's replay on disk.
        self.replay_id = f"{self.started_at}_{uuid.uuid4().hex[:8]}"
        self._append_frame("对局开始")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _replay_path(self):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", self.id or "default")[:40]
        return os.path.join(REPLAY_DIR, f"{safe}_{self.replay_id}.json")

    def _append_frame(self, label):
        frame = {
            "n": len(self.frames),
            "label": label,
            "left": {"board": [row[:] for row in self.sides["left"].board],
                      "score": self.sides["left"].score, "lines": self.sides["left"].lines},
            "right": {"board": [row[:] for row in self.sides["right"].board],
                       "score": self.sides["right"].score, "lines": self.sides["right"].lines},
        }
        self.frames.append(frame)
        try:
            os.makedirs(REPLAY_DIR, exist_ok=True)
            with open(self._replay_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "frames": self.frames,
                    "winner": self.winner,
                    "colors": tetris.COLORS,
                    "meta": {"left": self.sides["left"].controller,
                             "right": self.sides["right"].controller,
                             "started_at": self.started_at},
                }, f, ensure_ascii=False)
        except OSError as e:
            print(f"[warn] could not save replay for gid={self.id!r}: {e!r}", file=sys.stderr)

    def other(self, name):
        return "right" if name == "left" else "left"

    def _loop(self):
        while self.running:
            time.sleep(TICK_INTERVAL)
            with self.lock:
                if self.winner is not None:
                    continue
                for name, side in self.sides.items():
                    if not side.game_over:
                        self._tick_side(name, side)
                lo, ro = self.sides["left"].game_over, self.sides["right"].game_over
                if lo and ro:
                    self.winner = "draw"
                elif lo:
                    self.winner = "right"
                elif ro:
                    self.winner = "left"
                if self.winner is not None:
                    self._append_frame(f"对局结束 — 胜者: {SIDE_CN.get(self.winner, self.winner)}"
                                        if self.winner != "draw" else "对局结束 — 平局")

    def _refill_shared_bag(self, upto):
        while len(self.shared_bag) < upto:
            self.shared_bag.extend(tetris.new_bag(self.rng))

    def _next_letter(self, side):
        self._refill_shared_bag(side.piece_index + 1)
        letter = self.shared_bag[side.piece_index]
        side.piece_index += 1
        return letter

    def _peek_next(self, side, n=3):
        self._refill_shared_bag(side.piece_index + n)
        return self.shared_bag[side.piece_index: side.piece_index + n]

    def _apply_pending_garbage(self, side):
        if side.pending_garbage > 0:
            overflow = tetris.add_garbage(side.board, side.pending_garbage)
            side.pending_garbage = 0
            if overflow:
                side.game_over = True

    def _spawn(self, side):
        self._apply_pending_garbage(side)
        if side.game_over:
            return
        letter = self._next_letter(side)
        piece = tetris.spawn_piece(letter)
        if not tetris.cells_valid(side.board, piece.cells()):
            side.game_over = True
            return
        side.piece = piece
        side.hold_used = False
        side.landed = False
        side.last_gravity = time.time()

    def _gravity_interval(self, side):
        level = side.lines // 10
        return max(0.12, 0.8 - level * 0.05)

    def _lock_now(self, name, side):
        letter = side.piece.letter
        overflow = tetris.lock_piece(side.board, side.piece)
        lines = tetris.clear_full_lines(side.board)
        side.piece = None
        side.fast_drop = False
        side.fast_drop_target_row = None
        side.landed = False
        side.combo = side.combo + 1 if lines > 0 else 0
        side.lines += lines
        side.score += {0: 0, 1: 100, 2: 300, 3: 500, 4: 800}.get(lines, 0) \
            + side.combo * 20 if lines > 0 else 0
        if overflow:
            side.game_over = True
        garbage = tetris.garbage_for_clear(lines, side.combo) if lines > 0 else 0
        if garbage > 0:
            self.sides[self.other(name)].pending_garbage += garbage

        label = f"{SIDE_CN[name]}({BACKEND_CN.get(side.controller, side.controller)}) {letter}"
        if lines:
            label += f" 清{lines}行"
        if garbage:
            label += f" →倒垫{garbage}行"
        if overflow:
            label += " ⚠顶格"
        self._append_frame(label)

    def _tick_side(self, name, side):
        if side.piece is None:
            self._spawn(side)
            if side.game_over or side.piece is None:
                return
            if side.controller in ("jev", "laya") and not side.ai_thinking:
                side.ai_thinking = True
                gen = self.seq
                threading.Thread(target=self._ai_worker, args=(name, gen), daemon=True).start()
            return

        if side.fast_drop:
            target = side.fast_drop_target_row
            if side.piece.row < target:
                side.piece = Piece(side.piece.letter, side.piece.rot, side.piece.row + 1,
                                    side.piece.col)
            else:
                self._lock_now(name, side)
            return

        if side.controller == "human":
            now = time.time()
            if side.landed:
                if now - side.landed_since >= LOCK_DELAY:
                    self._lock_now(name, side)
            elif now - side.last_gravity >= self._gravity_interval(side):
                nxt = tetris.try_move(side.board, side.piece, 1, 0)
                if nxt:
                    side.piece = nxt
                    side.last_gravity = now
                else:
                    side.landed = True
                    side.landed_since = now
        # AI sides with a piece already out just wait for the worker thread.

    def _ai_worker(self, name, gen):
        with self.lock:
            if self.seq != gen or self.winner is not None:
                return
            side = self.sides[name]
            other = self.sides[self.other(name)]
            letter = side.piece.letter
            hold = side.hold_letter
            next3 = self._peek_next(side, 3)
            board_copy = [row[:] for row in side.board]
            opp_board_copy = [row[:] for row in other.board]
            own_stats = {"score": side.score, "lines": side.lines, "combo": side.combo}
            opp_stats = {"score": other.score}
            pending = side.pending_garbage
            backend = side.controller

        client = get_client(backend)
        chosen = req = resp = None
        used_fallback = False
        t0 = time.time()
        if client is None:
            resp = {"error": f"{backend}-backend-disabled"}
        else:
            try:
                chosen, req, resp, used_fallback = ai_engine.decide_placement(
                    client, letter, hold, next3, board_copy, opp_board_copy,
                    own_stats, opp_stats, pending, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda cand, rq, rp, el, _b=backend, _n=name, _p=letter:
                        _log_late_ai_result(_b, _n, _p, cand, el))
            except Exception as e:
                print(f"[warn] {backend} decide failed: {e!r}", file=sys.stderr)
                resp = {"error": repr(e)}
        elapsed = time.time() - t0
        usage = (resp or {}).get("usage", {}) if isinstance(resp, dict) else {}
        print(f"[ai-timing] backend={backend} side={name} piece={letter} "
              f"elapsed={elapsed:.2f}s fallback={used_fallback} "
              f"tokens_in={usage.get('input_tokens')} tokens_out={usage.get('output_tokens')} "
              f"error={(resp or {}).get('error') if isinstance(resp, dict) else None}",
              flush=True)

        with self.lock:
            if self.seq != gen or self.winner is not None:
                return
            side = self.sides[name]
            side.ai_thinking = False
            if chosen is None:
                side.game_over = True
                side.last_decision = {"error": (resp or {}).get("error", "no-placement")}
                return
            newp = Piece(side.piece.letter, chosen["rot"], side.piece.row, chosen["col"])
            if not tetris.cells_valid(side.board, newp.cells()):
                newp = side.piece
                target_row = tetris.hard_drop_row(side.board, newp).row
            else:
                target_row = chosen["row"]
            side.piece = newp
            side.fast_drop = True
            side.fast_drop_target_row = target_row
            if used_fallback:
                # `backend` didn't answer within AI_DECISION_DEADLINE — used our own
                # heuristic pick instead of freezing the piece waiting on a slow upstream
                # call. Surfaced distinctly in the UI so this is never silently confused
                # with a real model decision.
                side.last_decision = {
                    "label": "⚡ " + ai_engine.describe_placement(chosen),
                    "purpose": "fallback_timeout",
                    "aggression": None, "confidence": None,
                    "take_risk": None, "win_conf": None,
                    "io": {"request": req, "response": resp},
                }
            else:
                answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
                side.last_decision = {
                    "label": ai_engine.describe_placement(chosen),
                    "purpose": answers.get("placement_purpose", {}).get("choice"),
                    "aggression": answers.get("aggression", {}).get("score"),
                    "confidence": answers.get("placement", {}).get("confidence"),
                    "take_risk": answers.get("should_take_risk", {}).get("noul"),
                    "win_conf": answers.get("confidence_in_win", {}).get("noul"),
                    "io": {"request": req, "response": resp},
                }

    # ---- human actions (called with self.lock held by caller) ----

    def human_action(self, name, action):
        side = self.sides[name]
        if self.winner is not None or side.game_over or side.piece is None or side.fast_drop:
            return
        board, piece = side.board, side.piece
        if action == "left":
            nxt = tetris.try_move(board, piece, 0, -1)
            if nxt:
                side.piece = nxt
                side.landed = False
        elif action == "right":
            nxt = tetris.try_move(board, piece, 0, 1)
            if nxt:
                side.piece = nxt
                side.landed = False
        elif action == "softdrop":
            nxt = tetris.try_move(board, piece, 1, 0)
            if nxt:
                side.piece = nxt
                side.score += 1
                side.last_gravity = time.time()
                side.landed = False
            else:
                side.landed = True
                side.landed_since = time.time()
        elif action == "harddrop":
            side.piece = tetris.hard_drop_row(board, piece)
            self._lock_now(name, side)
        elif action in ("rotate_cw", "rotate_ccw"):
            direction = 1 if action == "rotate_cw" else -1
            nxt = tetris.try_rotate(board, piece, direction)
            if nxt:
                side.piece = nxt
                side.landed = False
        elif action == "hold":
            if not side.hold_used:
                old_hold = side.hold_letter
                side.hold_letter = piece.letter
                new_letter = old_hold if old_hold else self._next_letter(side)
                newp = tetris.spawn_piece(new_letter)
                if tetris.cells_valid(side.board, newp.cells()):
                    side.piece = newp
                    side.hold_used = True
                    side.landed = False
                else:
                    side.game_over = True


MATCHES = {}
_MATCHES_LOCK = threading.Lock()


def _evict_stale():
    cutoff = time.time() - MATCH_TTL
    for k in [k for k, m in MATCHES.items() if m.last_seen < cutoff]:
        idle = time.time() - MATCHES[k].last_seen
        print(f"[match] evicting gid={k!r} idle={idle:.0f}s (TTL={MATCH_TTL}s) "
              f"winner={MATCHES[k].winner}", flush=True)
        MATCHES[k].running = False
        del MATCHES[k]
    while len(MATCHES) >= MAX_MATCHES:
        oldest = min(MATCHES, key=lambda k: MATCHES[k].last_seen)
        print(f"[match] evicting gid={oldest!r} — MAX_MATCHES={MAX_MATCHES} reached "
              f"(currently {len(MATCHES)} tracked)", flush=True)
        MATCHES[oldest].running = False
        del MATCHES[oldest]


# gid -> last explicitly chosen controllers, persisted to disk so that an
# auto-recreated match (service restart / TTL eviction wipes MATCHES) keeps
# the same side assignments instead of silently reverting to defaults —
# reverting turns the side the user was controlling into an AI side
# mid-session, which looks exactly like "someone else is moving my pieces".
CTL_MAP_FILE = os.path.join(REPLAY_DIR, "_controllers.json")


def _load_ctl_map():
    try:
        with open(CTL_MAP_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


LAST_CTL = _load_ctl_map()
LAST_CTL_CAP = 500


def _save_ctl_map():
    try:
        os.makedirs(REPLAY_DIR, exist_ok=True)
        with open(CTL_MAP_FILE, "w", encoding="utf-8") as f:
            json.dump(LAST_CTL, f)
    except OSError as e:
        print(f"[warn] ctl map save failed: {e!r}", file=sys.stderr)


def get_match(create_with=None):
    gid = request.headers.get("X-Game-Id") or "default"
    with _MATCHES_LOCK:
        if create_with is not None:
            if len(LAST_CTL) >= LAST_CTL_CAP and gid not in LAST_CTL:
                LAST_CTL.pop(next(iter(LAST_CTL)))
            if LAST_CTL.get(gid) != create_with:
                LAST_CTL[gid] = create_with
                _save_ctl_map()
        m = MATCHES.get(gid)
        if m is None or create_with is not None:
            # Distinguish *why* we're creating a fresh Match, since both look identical
            # from here on but mean very different things to the player: an explicit
            # "开始新对局" click (create_with set, expected) vs this gid simply not being
            # tracked anymore — e.g. it aged out (see _evict_stale above) or the browser
            # lost its sessionStorage id (tab reload after being backgrounded, private
            # browsing, etc.) and is presenting a gid we've never seen. Either of the
            # latter silently resets the board with the *default* jev/human controllers,
            # which is exactly what a "sudden restart mid-game" report would look like.
            if m is not None:
                reason = "explicit new_match (replacing a live match)"
                m.running = False
            else:
                reason = "unknown/expired gid (fresh default match)" if create_with is None \
                    else "explicit new_match (gid not currently tracked)"
            print(f"[match] creating gid={gid!r} reason={reason} "
                  f"controllers={create_with or 'DEFAULT(jev,human)'} "
                  f"tracked_before={len(MATCHES)}", flush=True)
            _evict_stale()
            left_ctl, right_ctl = (create_with or LAST_CTL.get(gid)
                                   or ("jev", "human"))
            m = Match(gid, left_ctl, right_ctl)
            MATCHES[gid] = m
        m.last_seen = time.time()
        return m


@app.route("/api/_debug/matches", methods=["GET"])
def api_debug_matches():
    """Not linked from the UI — diagnostic peek at what's currently tracked in memory,
    to check for silent TTL/MAX_MATCHES eviction without needing to reproduce it live."""
    now = time.time()
    with _MATCHES_LOCK:
        return jsonify({
            "count": len(MATCHES),
            "matches": [
                {"gid": k, "idle_s": round(now - m.last_seen, 1), "winner": m.winner,
                 "left": m.sides["left"].controller, "right": m.sides["right"].controller,
                 "left_score": m.sides["left"].score, "right_score": m.sides["right"].score}
                for k, m in MATCHES.items()
            ],
        })


def human_side_name(match):
    for name, side in match.sides.items():
        if side.controller == "human":
            return name
    return None


def side_view(match, name):
    side = match.sides[name]
    with match.lock:
        board = [row[:] for row in side.board]
        piece_cells, ghost_cells, letter = [], [], None
        if side.piece is not None:
            letter = side.piece.letter
            piece_cells = [list(p) for p in side.piece.cells() if p[0] >= 0]
            if not side.fast_drop:
                ghost = tetris.hard_drop_row(side.board, side.piece)
                ghost_cells = [list(p) for p in ghost.cells() if p[0] >= 0]
        return {
            "controller": side.controller,
            "board": board,
            "piece": letter,
            "piece_cells": piece_cells,
            "ghost_cells": ghost_cells,
            "hold": side.hold_letter,
            "next": match._peek_next(side, 3),
            "score": side.score,
            "lines": side.lines,
            "combo": side.combo,
            "pending_garbage": side.pending_garbage,
            "game_over": side.game_over,
            "ai_thinking": side.ai_thinking,
            "last_decision": side.last_decision,
        }


def serialize_match(match):
    with match.lock:
        winner = match.winner
    return {
        "id": match.id,
        "winner": winner,
        "replay_file": os.path.basename(match._replay_path()),
        "human_side": human_side_name(match),
        "left": side_view(match, "left"),
        "right": side_view(match, "right"),
        "colors": tetris.COLORS,
    }


# ---------- Flask routes ----------

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET"])
def api_state():
    match = get_match()
    return jsonify(serialize_match(match))


@app.route("/api/new_match", methods=["POST"])
def api_new_match():
    body = request.get_json(silent=True) or {}
    left = (body.get("left") or "jev").lower()
    right = (body.get("right") or "human").lower()
    valid = {"human", "jev", "laya"}
    if left not in valid or right not in valid:
        return jsonify({"ok": False, "error": "invalid controller"}), 400
    if right == "laya" and not LAYA_ENABLED or left == "laya" and not LAYA_ENABLED:
        return jsonify({"ok": False, "error": "laya backend disabled on this server"}), 400
    match = get_match(create_with=(left, right))
    return jsonify(serialize_match(match))


@app.route("/api/action", methods=["POST"])
def api_action():
    body = request.get_json(silent=True) or {}
    action = body.get("action")
    match = get_match()
    name = human_side_name(match)
    if name is None:
        return jsonify({"ok": False, "error": "no human side in this match"}), 400
    with match.lock:
        match.human_action(name, action)
    return jsonify(serialize_match(match))


@app.route("/replay")
def replay_page():
    return render_template("replay.html")


@app.route("/api/replays", methods=["GET"])
def list_replays():
    """List saved replay files (newest first)."""
    files = []
    try:
        for name in os.listdir(REPLAY_DIR):
            if not name.endswith(".json"):
                continue
            path = os.path.join(REPLAY_DIR, name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            files.append({
                "name": name,
                "frames": len(data.get("frames", [])),
                "winner": data.get("winner"),
                "meta": data.get("meta"),
                "mtime": os.path.getmtime(path),
            })
    except OSError:
        pass
    files.sort(key=lambda f: f["mtime"], reverse=True)
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
    try:
        with open(path, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "corrupt replay file"}), 500


def _preload_laya():
    """Laya's checkpoint load (~1.3GB, moved onto MPS/CUDA/CPU) takes ~15-20s the first
    time it's used in a process. Doing that lazily on the first AI turn makes the very
    first Laya move look like the game has hung — warm it up in the background at server
    boot instead, so it's ready by the time anyone actually starts a match."""
    t0 = time.time()
    print("[boot] preloading Laya model in the background...", flush=True)
    client = get_client("laya")
    if client is None:
        print("[boot] Laya preload skipped/failed (see warning above, if any)", flush=True)
    else:
        print(f"[boot] Laya ready in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    if LAYA_ENABLED:
        threading.Thread(target=_preload_laya, daemon=True).start()
    port = int(os.environ.get("PORT", 5050))
    app.run(host="0.0.0.0", port=port, debug=True, threaded=True, use_reloader=False)

"""Flask server for Chinese Checkers (跳棋) with Jev / Laya AI seats.

Two seats (red = seat 0 bottom, blue = seat 1 top); each is "human" or an AI
backend sharing the state+questions -> answers contract. Same production
patterns as the other arenas: 2s decision deadline + heuristic fallback,
per-match replay files on disk, in-memory live matches, one Match thread.
"""

import json
import os
import re
import sys
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request

import ai_engine
import rules
from rules import CN, PLAYERS

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
AI_DECISION_DEADLINE = 2.0
TICK_INTERVAL = 0.05
AI_MOVE_DELAY = 0.6
MATCH_TTL = 3 * 3600
MAX_MATCHES = 100

REPLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays")
BACKEND_CN = {"human": "玩家", "jev": "Jev", "laya": "Laya"}

_CLIENTS = {}
_CLIENT_LOCK = threading.Lock()


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


def _log_late(backend, seat, pick, elapsed):
    print(f"[ai-timing] LATE backend={backend} seat={seat} "
          f"total_elapsed={elapsed:.2f}s would_have_picked={pick!r} "
          f"(fallback already used)", flush=True)


class Match:
    def __init__(self, mid, controllers):
        self.id = mid
        self.controllers = list(controllers)
        self.game = rules.Game()
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True
        self.last_seen = time.time()
        self.next_ai_move_at = 0.0
        self.ai_busy = {p: False for p in PLAYERS}
        self.last_decision = {p: None for p in PLAYERS}
        self.frames = []
        self.decisions = []
        self.started_at = time.strftime("%Y%m%d-%H%M%S")
        self.replay_id = f"{self.started_at}_{uuid.uuid4().hex[:8]}"
        self._append_frame("对局开始 — 红方先行")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _replay_path(self):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", self.id or "default")[:40]
        return os.path.join(REPLAY_DIR, f"{safe}_{self.replay_id}.json")

    def _append_frame(self, label):
        g = self.game
        self.frames.append({
            "n": len(self.frames),
            "label": label,
            "turn": g.turn,
            "move_no": g.move_no,
            "pegs": {p: sorted(tuple(x) for x in g.pegs[p]) for p in PLAYERS},
            "winner": g.winner,
        })
        try:
            os.makedirs(REPLAY_DIR, exist_ok=True)
            with open(self._replay_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "frames": self.frames,
                    "winner": g.winner,
                    "decisions": self.decisions,
                    "meta": {"controllers": self.controllers,
                             "started_at": self.started_at},
                }, f, ensure_ascii=False)
        except OSError as e:
            print(f"[warn] replay save failed: {e!r}", file=sys.stderr)

    def _loop(self):
        while self.running:
            time.sleep(TICK_INTERVAL)
            with self.lock:
                g = self.game
                if g.winner:
                    continue
                player = PLAYERS[g.turn]
                if self.controllers[g.turn] == "human" \
                        or self.ai_busy[player]:
                    continue
                if time.time() < self.next_ai_move_at:
                    continue
                self.ai_busy[player] = True
                threading.Thread(target=self._ai_worker, args=(g.turn,),
                                 daemon=True).start()

    def _ai_worker(self, seat):
        player = PLAYERS[seat]
        backend = self.controllers[seat]
        client = get_client(backend)
        chosen = req = resp = None
        used_fallback = False
        t0 = time.time()
        try:
            if client is None:
                resp = {"error": f"{backend}-backend-disabled"}
                with self.lock:
                    chosen = ai_engine.move_fallback(self.game, player)
                used_fallback = True
            else:
                chosen, req, resp, used_fallback = ai_engine.decide_move(
                    client, self.game, player,
                    deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late(backend, seat, p, el))
        except Exception as e:
            print(f"[warn] {backend} move failed: {e!r}", file=sys.stderr)
            resp = {"error": repr(e)}
            with self.lock:
                try:
                    chosen = ai_engine.move_fallback(self.game, player)
                except Exception:
                    chosen = None
            used_fallback = True
        elapsed = time.time() - t0
        usage = (resp or {}).get("usage", {}) if isinstance(resp, dict) else {}
        print(f"[ai-timing] backend={backend} seat={seat} elapsed={elapsed:.2f}s "
              f"fallback={used_fallback} tokens_in={usage.get('input_tokens')} "
              f"error={(resp or {}).get('error') if isinstance(resp, dict) else None}",
              flush=True)

        with self.lock:
            self.ai_busy[player] = False
            if self.game.winner:
                return
            answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
            try:
                self.game.apply_move(player, chosen)
            except ValueError as e:
                print(f"[warn] move rejected ({e}); using fallback",
                      file=sys.stderr)
                chosen = ai_engine.move_fallback(self.game, player)
                try:
                    self.game.apply_move(player, chosen)
                except ValueError:
                    return
            label = rules.path_label(chosen)
            prefix = "⚡ " if used_fallback else ""
            self.last_decision[player] = {
                "label": prefix + label,
                "reason": answers.get("intent", {}).get("choice"),
                "confidence": answers.get("confidence_in_win", {}),
                "io": {"request": req, "response": resp},
            }
            self.decisions.append({
                "frame": len(self.frames), "seat": seat, "backend": backend,
                "move": label, "fallback": used_fallback,
                "elapsed_s": round(elapsed, 2)})
            self._append_frame(
                f"{CN[player]}({BACKEND_CN.get(backend)}) {label}")
            self.next_ai_move_at = time.time() + AI_MOVE_DELAY

    def human_move(self, seat, path):
        if self.controllers[seat] != "human":
            return
        self.game.apply_move(PLAYERS[seat], path)
        self._append_frame(
            f"{CN[PLAYERS[seat]]}(玩家) {rules.path_label(path)}")


MATCHES = {}
_MATCHES_LOCK = threading.Lock()


def _evict_stale():
    cutoff = time.time() - MATCH_TTL
    for k in [k for k, m in MATCHES.items() if m.last_seen < cutoff]:
        MATCHES[k].running = False
        del MATCHES[k]
    while len(MATCHES) >= MAX_MATCHES:
        oldest = min(MATCHES, key=lambda k: MATCHES[k].last_seen)
        MATCHES[oldest].running = False
        del MATCHES[oldest]


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
            _evict_stale()
            if m is not None:
                m.running = False
                m.seq += 1
            ctls = create_with or LAST_CTL.get(gid) or ["human", "jev"]
            m = Match(gid, ctls)
            MATCHES[gid] = m
        m.last_seen = time.time()
        return m


def serialize_match(match):
    with match.lock:
        g = match.game
        human = match.controllers.index("human") \
            if "human" in match.controllers else None
        seats = []
        for i, p in enumerate(PLAYERS):
            seats.append({
                "seat": i, "player": p, "controller": match.controllers[i],
                "ai_thinking": match.ai_busy[p],
                "last_decision": match.last_decision[p],
                "pegs": sorted(tuple(x) for x in g.pegs[p]),
                "in_home": g.pegs_in(p, rules.target_zone(p)),
            })
        return {
            "id": match.id,
            "turn": g.turn, "move_no": g.move_no, "winner": g.winner,
            "human_seat": human,
            "seats": seats,
            "legal_moves": [list(p) for p in
                            (g.legal_moves(PLAYERS[human])
                             if human is not None and not g.winner else [])],
            "replay_file": os.path.basename(match._replay_path()),
            "laya_enabled": LAYA_ENABLED,
            "last_moves": [
                {"player": m["player"], "move": rules.path_label(m["path"]),
                 "kind": m["kind"]} for m in g.last_moves[-10:]],
        }


@app.route("/")
def index():
    return render_template("index.html", holes=rules.HOLES)


@app.route("/api/state")
def api_state():
    return jsonify(serialize_match(get_match()))


@app.route("/api/new_match", methods=["POST"])
def api_new_match():
    body = request.get_json(force=True, silent=True) or {}
    ctls = body.get("controllers")
    if (not isinstance(ctls, list) or len(ctls) != 2
            or any(c not in ("human", "jev", "laya") for c in ctls)):
        return jsonify({"ok": False, "error": "controllers must be 2 of "
                        "human/jev/laya"}), 400
    if ctls.count("human") > 1:
        return jsonify({"ok": False, "error": "每个标签页只能操作一个人类座位"}), 400
    if "laya" in ctls and not LAYA_ENABLED:
        return jsonify({"ok": False, "error": "本服务器未启用 Laya"}), 400
    return jsonify(serialize_match(get_match(create_with=ctls)))


@app.route("/api/move", methods=["POST"])
def api_move():
    m = get_match()
    body = request.get_json(force=True, silent=True) or {}
    seat = body.get("seat")
    path = body.get("path")
    with m.lock:
        if seat not in (0, 1) or m.controllers[seat] != "human":
            return jsonify({"ok": False, "error": "not a human seat"}), 400
        if not isinstance(path, list) or not path:
            return jsonify({"ok": False, "error": "bad path"}), 400
        try:
            m.human_move(seat, [tuple(p) for p in path])
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(serialize_match(m))


@app.route("/replay")
def replay_page():
    return render_template("replay.html", holes=rules.HOLES)


@app.route("/api/replays", methods=["GET"])
def list_replays():
    files = []
    try:
        for name in os.listdir(REPLAY_DIR):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            path = os.path.join(REPLAY_DIR, name)
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            files.append({"name": name, "frames": len(data.get("frames", [])),
                          "winner": data.get("winner"), "meta": data.get("meta"),
                          "mtime": os.path.getmtime(path)})
    except OSError:
        pass
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return jsonify({"files": files})


@app.route("/api/replay_file", methods=["GET"])
def replay_file():
    name = os.path.basename(request.args.get("name", ""))
    if not name.endswith(".json") or name.startswith("_"):
        return jsonify({"error": "not found"}), 404
    path = os.path.join(REPLAY_DIR, name)
    if not os.path.isfile(path):
        return jsonify({"error": "not found"}), 404
    try:
        with open(path, encoding="utf-8") as f:
            return jsonify(json.load(f))
    except (json.JSONDecodeError, OSError):
        return jsonify({"error": "bad file"}), 500


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5056"))
    app.run(host="127.0.0.1", port=port, threaded=True)

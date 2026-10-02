"""Flask server for Texas Hold'em with Jev / Laya AI seats."""

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
import holdem
from holdem import card_label, best_score, hand_name, STREET_CN

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
AI_MOVE_DELAY = 0.8
HAND_END_PAUSE = 5.0
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
          f"total_elapsed={elapsed:.2f}s would_have_picked={pick!r}",
          flush=True)


def _action_label(action):
    if action.startswith("raise:"):
        return f"加注到{action.split(':')[1]}"
    return {"fold": "弃牌", "check": "过牌", "call": "跟注"}.get(action, action)


class Match:
    def __init__(self, mid, controllers):
        self.id = mid
        self.controllers = list(controllers)
        self.game = holdem.Game()
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True
        self.last_seen = time.time()
        self.next_ai_action_at = 0.0
        self.hand_end_at = 0.0
        self.ai_busy = {s: False for s in range(3)}
        self.last_decision = {s: None for s in range(3)}
        self.frames = []
        self.decisions = []
        self.started_at = time.strftime("%Y%m%d-%H%M%S")
        self.replay_id = f"{self.started_at}_{uuid.uuid4().hex[:8]}"
        self._append_frame(f"第1手牌开始 — 按钮=seat{self.game.button}")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _replay_path(self):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", self.id or "default")[:40]
        return os.path.join(REPLAY_DIR, f"{safe}_{self.replay_id}.json")

    def _frame_snapshot(self, label):
        g = self.game
        h = g.hand
        showdown = bool(h and h.showdown)
        holes = {}
        for i in range(3):
            s = g.seats[i]
            if showdown or g.phase == "over":
                holes[str(i)] = list(s.hole)          # full record in replay
            else:
                holes[str(i)] = list(s.hole) if s.hole else []
        return {
            "n": len(self.frames), "label": label, "phase": g.phase,
            "hand_no": g.hand_no,
            "street": h.street if h else None,
            "board": list(h.board) if h else [],
            "turn": h.turn if h else None,
            "pot": sum(s.invested for s in g.seats),
            "current_bet": h.current_bet if h else 0,
            "stacks": [s.stack for s in g.seats],
            "bets": [s.bet for s in g.seats],
            "folded": [s.folded for s in g.seats],
            "allin": [s.allin for s in g.seats],
            "holes": holes,
            "showdown": showdown,
            "button": g.button,
            "history": list(h.events) if h else [],
        }

    def _append_frame(self, label):
        self.frames.append(self._frame_snapshot(label))
        try:
            os.makedirs(REPLAY_DIR, exist_ok=True)
            with open(self._replay_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "frames": self.frames,
                    "winner": self.game.winner(),
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
                if g.over:
                    continue
                if g.phase == "handover":
                    if not self.hand_end_at:
                        res = g.last_result or {}
                        lbl = "摊牌结算" if res.get("type") == "showdown" \
                            else f"座位{res.get('winners',[None])[0]}收池"
                        self._append_frame(lbl)
                        self.hand_end_at = time.time() + HAND_END_PAUSE
                    elif time.time() >= self.hand_end_at:
                        self.hand_end_at = 0.0
                        g.next_hand()
                        if not g.over:
                            self._append_frame(
                                f"第{g.hand_no}手牌 — 按钮=seat{g.button}")
                        else:
                            self._append_frame("对局结束")
                    continue
                h = g.hand
                if h is None or h.turn is None:
                    continue
                seat = h.turn
                if self.controllers[seat] == "human" or self.ai_busy[seat]:
                    continue
                if time.time() < self.next_ai_action_at:
                    continue
                self.ai_busy[seat] = True
                threading.Thread(target=self._ai_worker,
                                 args=(seat, self.seq), daemon=True).start()

    def _ai_worker(self, seat, gen):
        backend = self.controllers[seat]
        client = get_client(backend)
        chosen = req = resp = None
        used_fallback = False
        t0 = time.time()
        try:
            if client is None:
                resp = {"error": f"{backend}-backend-disabled"}
                with self.lock:
                    chosen = ai_engine.fallback(self.game, seat)
                used_fallback = True
            else:
                chosen, req, resp, used_fallback = ai_engine.decide(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late(backend, seat, p, el))
        except Exception as e:
            print(f"[warn] {backend} failed: {e!r}", file=sys.stderr)
            resp = {"error": repr(e)}
            with self.lock:
                try:
                    chosen = ai_engine.fallback(self.game, seat)
                except Exception:
                    chosen = "check"
            used_fallback = True
        elapsed = time.time() - t0
        usage = (resp or {}).get("usage", {}) if isinstance(resp, dict) else {}
        print(f"[ai-timing] backend={backend} seat={seat} "
              f"elapsed={elapsed:.2f}s fallback={used_fallback} "
              f"tokens_in={usage.get('input_tokens')} "
              f"error={(resp or {}).get('error') if isinstance(resp, dict) else None}",
              flush=True)

        with self.lock:
            self.ai_busy[seat] = False
            if self.seq != gen or self.game.over:
                return
            answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
            legal = self.game.legal_actions(seat)
            if chosen not in legal:
                chosen = ai_engine.fallback(self.game, seat)
                if chosen not in legal:
                    chosen = legal[0]
                used_fallback = True
            try:
                self.game.apply_action(seat, chosen)
            except ValueError:
                self.game.apply_action(seat, legal[0])
                chosen = legal[0]
            prefix = "⚡ " if used_fallback else ""
            label = _action_label(chosen)
            self.last_decision[seat] = {
                "label": prefix + label,
                "reason": answers.get("reason", {}).get("choice"),
                "range_guess": answers.get("opponent_range_guess",
                                           {}).get("choice"),
                "confidence": answers.get("action", {}).get("confidence"),
                "io": {"request": req, "response": resp},
            }
            self.decisions.append({
                "frame": len(self.frames), "seat": seat,
                "backend": backend, "label": label,
                "fallback": used_fallback, "elapsed_s": round(elapsed, 2)})
            self._append_frame(
                f"seat{seat}({BACKEND_CN.get(backend)}) {label}")
            self.next_ai_action_at = time.time() + AI_MOVE_DELAY

    def human_action(self, seat, action):
        if self.controllers[seat] != "human":
            return
        self.game.apply_action(seat, action)
        self._append_frame(f"seat{seat}(玩家) {_action_label(action)}")
        if self.game.phase == "handover":
            self._append_frame("摊牌结算" if self.game.last_result.get(
                "type") == "showdown" else "收池")
            self.hand_end_at = time.time() + HAND_END_PAUSE


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


# gid -> last explicitly chosen controllers, persisted to disk so an
# auto-recreated match (service restart / TTL eviction wipes MATCHES) keeps
# the same seat assignments instead of silently reverting to defaults.
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
            ctls = create_with or LAST_CTL.get(gid) or ["jev", "human", "jev"]
            m = Match(gid, ctls)
            MATCHES[gid] = m
        m.last_seen = time.time()
        return m


def serialize_match(match):
    with match.lock:
        g = match.game
        h = g.hand
        human = match.controllers.index("human") \
            if "human" in match.controllers else None
        showdown = bool(h and h.showdown) or g.phase == "over"
        seats = []
        for i in range(3):
            s = g.seats[i]
            reveal = showdown or i == human or not s.hole
            seats.append({
                "seat": i, "controller": match.controllers[i],
                "stack": s.stack, "bet": s.bet, "invested": s.invested,
                "hole": list(s.hole) if reveal else None,
                "folded": s.folded, "allin": s.allin,
                "hand_name": hand_name(s.hole + h.board)
                    if h and s.hole and len(s.hole) + len(h.board) >= 5
                    and reveal else None,
                "ai_thinking": match.ai_busy[i],
                "last_decision": match.last_decision[i],
            })
        pos = {"button": g.button}
        return {
            "id": match.id, "phase": g.phase, "hand_no": g.hand_no,
            "street": h.street if h else None,
            "street_cn": STREET_CN.get(h.street) if h else None,
            "board": list(h.board) if h else [],
            "turn": h.turn if h else None,
            "pot": sum(s.invested for s in g.seats),
            "current_bet": h.current_bet if h else 0,
            "winner": g.winner(), "human_seat": human,
            "seats": seats, "positions": pos,
            "showdown": showdown,
            "last_result": g.last_result,
            "legal_actions": g.legal_actions(human) if human is not None else [],
            "replay_file": os.path.basename(match._replay_path()),
            "laya_enabled": LAYA_ENABLED,
            "history": ai_engine._action_history(g)[-20:] if h else [],
        }


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/state")
def api_state():
    return jsonify(serialize_match(get_match()))


@app.route("/api/new_match", methods=["POST"])
def api_new_match():
    body = request.get_json(force=True, silent=True) or {}
    ctls = body.get("controllers")
    if (not isinstance(ctls, list) or len(ctls) != 3
            or any(c not in ("human", "jev", "laya") for c in ctls)):
        return jsonify({"ok": False, "error": "controllers must be 3 of "
                        "human/jev/laya"}), 400
    if ctls.count("human") > 1:
        return jsonify({"ok": False, "error": "每个标签页只能操作一个人类座位"}), 400
    if "laya" in ctls and not LAYA_ENABLED:
        return jsonify({"ok": False, "error": "本服务器未启用 Laya"}), 400
    return jsonify(serialize_match(get_match(create_with=ctls)))


@app.route("/api/action", methods=["POST"])
def api_action():
    m = get_match()
    body = request.get_json(force=True, silent=True) or {}
    seat, action = body.get("seat"), body.get("action")
    with m.lock:
        if seat not in range(3) or m.controllers[seat] != "human":
            return jsonify({"ok": False, "error": "not a human seat"}), 400
        try:
            m.human_action(seat, str(action))
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(serialize_match(m))


@app.route("/replay")
def replay_page():
    return render_template("replay.html")


@app.route("/api/replays", methods=["GET"])
def list_replays():
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
    if not name.endswith(".json"):
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
    port = int(os.environ.get("PORT", "5054"))
    app.run(host="127.0.0.1", port=port, threaded=True)

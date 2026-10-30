"""Flask server for UNO with Jev / Laya AI seats.

Four seats, turn-based with direction; each seat is "human" or an AI
backend sharing the army-chess state+questions -> answers contract. Same
production patterns as the other arenas: 2s decision deadline + heuristic
fallback, per-match replay files on disk, in-memory live matches.
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
import uno
from uno import card_label

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
AI_MOVE_DELAY = 0.9
SETTLE_PAUSE = 4.0
MATCH_TTL = 3 * 3600
MAX_MATCHES = 100
N = uno.NUM_SEATS

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


def _log_late(backend, seat, kind, pick, elapsed):
    print(f"[ai-timing] LATE backend={backend} seat={seat} kind={kind} "
          f"total_elapsed={elapsed:.2f}s would_have_picked={pick!r} "
          f"(fallback already used)", flush=True)


class Match:
    def __init__(self, mid, controllers):
        self.id = mid
        self.controllers = list(controllers)
        self.game = uno.Game(num_seats=N)
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True
        self.last_seen = time.time()
        self.next_ai_action_at = 0.0
        self.settle_at = 0.0
        self.ai_busy = {s: False for s in range(N)}
        self.last_decision = {s: None for s in range(N)}
        self.frames = []
        self.decisions = []
        self.started_at = time.strftime("%Y%m%d-%H%M%S")
        self.replay_id = f"{self.started_at}_{uuid.uuid4().hex[:8]}"
        self._append_frame("对局开始 — 第1局发牌")
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
            "round": g.round_no,
            "turn": g.turn,
            "direction": g.direction,
            "hands": {str(s): list(g.hands[s]) for s in range(N)},
            "discard_top": g.top_card(),
            "active_color": g.active_color,
            "scores": list(g.scores),
            "round_over": g.round_over,
            "match_over": g.match_over,
        })
        try:
            os.makedirs(REPLAY_DIR, exist_ok=True)
            with open(self._replay_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "frames": self.frames,
                    "winner": g.match_winner,
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
                if g.match_over:
                    continue
                if g.round_over:
                    if not self.settle_at:
                        self.settle_at = time.time() + SETTLE_PAUSE
                        self._append_frame(
                            f"第{g.round_no}局结束 — seat{g.round_winner} 获胜")
                    elif time.time() >= self.settle_at:
                        self.settle_at = 0.0
                        g.next_round()
                        if g.match_over:
                            self._append_frame(
                                f"对局结束 — seat{g.match_winner} 获胜（{g.scores}）")
                        else:
                            self._append_frame(f"第{g.round_no}局发牌")
                    continue
                seat = g.turn
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
        card = color = req = resp = None
        used_fallback = False
        t0 = time.time()
        try:
            if client is None:
                with self.lock:
                    card, color = ai_engine.play_fallback(self.game, seat)
                resp = {"error": f"{backend}-backend-disabled"}
                used_fallback = True
            else:
                card, color, req, resp, used_fallback = ai_engine.decide_turn(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda c, rq, rp, el:
                        _log_late(backend, seat, "turn", c, el))
        except Exception as e:
            print(f"[warn] {backend} turn failed: {e!r}", file=sys.stderr)
            resp = {"error": repr(e)}
            with self.lock:
                try:
                    card, color = ai_engine.play_fallback(self.game, seat)
                except Exception:
                    card = None
            used_fallback = True
        elapsed = time.time() - t0

        with self.lock:
            self.ai_busy[seat] = False
            if self.seq != gen:
                return
            if self.game.match_over or self.game.round_over:
                return
            prefix = "⚡ " if used_fallback else ""
            try:
                if card is None:
                    drawn = self.game.apply_draw(seat)
                    label = f"摸{len(drawn)}张" if drawn else "摸牌(牌堆空)"
                else:
                    self.game.apply_play(seat, card, color)
                    label = card_label(card)
                    if color and uno.is_wild(card):
                        label += f"→指定{uno.COLOR_NAMES.get(color, color)}"
            except ValueError as e:
                print(f"[warn] invalid AI action seat={seat} card={card}: {e}",
                      file=sys.stderr)
                drawn = self.game.apply_draw(seat) if seat == self.game.turn else []
                label = f"摸{len(drawn)}张(兜底纠错)"
            print(f"[ai-timing] backend={backend} seat={seat} kind=turn "
                  f"elapsed={elapsed:.2f}s fallback={used_fallback} "
                  f"error={(resp or {}).get('error') if isinstance(resp, dict) else None}",
                  flush=True)
            self.last_decision[seat] = {
                "label": prefix + label,
                "confidence": (resp or {}).get("answers", {}).get("confidence", {}).get("score")
                             if isinstance(resp, dict) else None,
                "io": {"request": req, "response": resp},
            }
            self.decisions.append({
                "frame": len(self.frames), "seat": seat,
                "backend": backend, "label": label,
                "fallback": used_fallback, "elapsed_s": round(elapsed, 2)})
            self._append_frame(f"seat{seat}({BACKEND_CN.get(backend)}) {label}")
            self.next_ai_action_at = time.time() + AI_MOVE_DELAY

    def human_play(self, seat, card, color=None):
        if self.controllers[seat] != "human":
            return
        self.game.apply_play(seat, card, color)
        label = card_label(card)
        if color and uno.is_wild(card):
            label += f"→指定{uno.COLOR_NAMES.get(color, color)}"
        self._append_frame(f"seat{seat}(玩家) {label}")
        if self.game.round_over:
            self.settle_at = time.time() + SETTLE_PAUSE
            self._append_frame(f"第{self.game.round_no}局结束 — seat{self.game.round_winner} 获胜")

    def human_draw(self, seat):
        if self.controllers[seat] != "human":
            return
        drawn = self.game.apply_draw(seat)
        self._append_frame(f"seat{seat}(玩家) 摸{len(drawn)}张")


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
            ctls = create_with or LAST_CTL.get(gid) or ["jev", "human", "jev", "jev"]
            m = Match(gid, ctls)
            MATCHES[gid] = m
        m.last_seen = time.time()
        return m


def serialize_match(match):
    with match.lock:
        g = match.game
        human = match.controllers.index("human") if "human" in match.controllers else None
        reveal_all = human is None or g.match_over
        seats = []
        for i in range(N):
            visible = reveal_all or i == human
            seats.append({
                "seat": i, "controller": match.controllers[i],
                "hand": [card_label(c) for c in g.hands[i]] if visible else None,
                "hand_codes": list(g.hands[i]) if (i == human) else None,
                "hand_count": len(g.hands[i]),
                "score": g.scores[i],
                "ai_thinking": match.ai_busy[i],
                "last_decision": match.last_decision[i],
            })
        legal = []
        can_draw = False
        if human is not None and g.turn == human and not g.round_over:
            legal = g.legal_plays(human)
            can_draw = not legal or g.must_draw_cards() > 0
        return {
            "id": match.id,
            "round": g.round_no, "turn": g.turn, "direction": g.direction,
            "active_color": g.active_color, "active_color_cn": uno.COLOR_NAMES.get(g.active_color),
            "top_card": card_label(g.top_card()), "top_card_code": g.top_card(),
            "pending_draw": g.must_draw_cards(),
            "round_over": g.round_over, "round_winner": g.round_winner,
            "match_over": g.match_over, "winner": g.match_winner,
            "human_seat": human,
            "seats": seats,
            "legal_plays": legal,
            "can_draw": can_draw,
            "deck_remaining": len(g.deck),
            "replay_file": os.path.basename(match._replay_path()),
            "laya_enabled": LAYA_ENABLED,
            "history": g.history[-10:],
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
    if (not isinstance(ctls, list) or len(ctls) != N
            or any(c not in ("human", "jev", "laya") for c in ctls)):
        return jsonify({"ok": False, "error": f"controllers must be {N} of "
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
    kind = body.get("type")
    with m.lock:
        human = m.controllers.index("human") if "human" in m.controllers else None
        if human is None or m.game.turn != human:
            return jsonify({"ok": False, "error": "not your turn"}), 400
        try:
            if kind == "play":
                card = body.get("card")
                color = body.get("color")
                m.human_play(human, card, color)
            elif kind == "draw":
                m.human_draw(human)
            else:
                return jsonify({"ok": False, "error": "unknown"}), 400
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
            if not name.endswith(".json") or name == os.path.basename(CTL_MAP_FILE):
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
    port = int(os.environ.get("PORT", "5056"))
    app.run(host="127.0.0.1", port=port, threaded=True)

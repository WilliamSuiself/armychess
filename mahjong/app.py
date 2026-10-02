"""Flask server for 4-player Mahjong with Jev / Laya AI seats."""

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
import mahjong
from mahjong import tile_label, shanten, wait_tiles

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
AI_MOVE_DELAY = 0.7
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


def _log_late(backend, seat, kind, pick, elapsed):
    print(f"[ai-timing] LATE backend={backend} seat={seat} kind={kind} "
          f"total_elapsed={elapsed:.2f}s would_have_picked={pick!r}",
          flush=True)


class Match:
    def __init__(self, mid, controllers):
        self.id = mid
        self.controllers = list(controllers)
        self.game = mahjong.Game()
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True
        self.last_seen = time.time()
        self.next_ai_action_at = 0.0
        self.hand_end_at = 0.0
        self.ai_busy = {s: False for s in range(mahjong.NUM_SEATS)}
        self.last_decision = {s: None for s in range(mahjong.NUM_SEATS)}
        self.frames = []
        self.decisions = []
        self.started_at = time.strftime("%Y%m%d-%H%M%S")
        self.replay_id = f"{self.started_at}_{uuid.uuid4().hex[:8]}"
        self._append_frame(f"第1局开始 — 庄=seat{self.game.dealer}")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _replay_path(self):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", self.id or "default")[:40]
        return os.path.join(REPLAY_DIR, f"{safe}_{self.replay_id}.json")

    def _frame_snapshot(self, label):
        g = self.game
        return {
            "n": len(self.frames), "label": label, "phase": g.phase,
            "hand_no": g.hand_no, "dealer": g.dealer,
            "turn": g.turn, "turn_phase": g.turn_phase,
            "pending": tile_label(g.pending_tile)
                if g.pending_tile is not None else None,
            "wall": len(g.wall),
            # replays record full concealed hands (post-hoc record)
            "concealed": {str(i): list(g.seats[i].concealed)
                          for i in range(mahjong.NUM_SEATS)},
            "concealed_count": [len(g.seats[i].concealed) for i in range(mahjong.NUM_SEATS)],
            "melds": {str(i): [[m.kind, list(m.tiles)]
                              for m in g.seats[i].melds] for i in range(mahjong.NUM_SEATS)},
            "discards": {str(i): list(g.seats[i].discards) for i in range(mahjong.NUM_SEATS)},
            "points": [g.seats[i].points for i in range(mahjong.NUM_SEATS)],
            "last_drawn_seat": g.turn if g.last_drawn is not None else None,
        }

    def _append_frame(self, label):
        self.frames.append(self._frame_snapshot(label))
        try:
            os.makedirs(REPLAY_DIR, exist_ok=True)
            with open(self._replay_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "frames": self.frames,
                    "winner": self.game.match_winner(),
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
                        lbl = ("自摸" if res.get("self_drawn") else "点炮") \
                            if res.get("type") == "hu" else "流局"
                        w = res.get("winner")
                        self._append_frame(
                            f"{'座位'+str(w)+' '+lbl+'胡牌' if w is not None else lbl}")
                        self.hand_end_at = time.time() + HAND_END_PAUSE
                    elif time.time() >= self.hand_end_at:
                        self.hand_end_at = 0.0
                        g.next_hand()
                        if not g.over:
                            self._append_frame(
                                f"第{g.hand_no}局 — 庄=seat{g.dealer}")
                        else:
                            self._append_frame("对局结束")
                    continue
                if g.turn_phase == "claim":
                    qs = [s for _, s in g.claim_queue]
                    if not qs:
                        continue
                    seat = qs[0]
                    kind = "claim"
                else:
                    seat = g.turn
                    kind = "action"
                if self.controllers[seat] == "human" or self.ai_busy[seat]:
                    continue
                if time.time() < self.next_ai_action_at:
                    continue
                self.ai_busy[seat] = True
                threading.Thread(target=self._ai_worker,
                                 args=(seat, self.seq, kind),
                                 daemon=True).start()

    def _ai_worker(self, seat, gen, kind):
        backend = self.controllers[seat]
        client = get_client(backend)
        chosen = req = resp = None
        used_fallback = False
        t0 = time.time()
        try:
            if client is None:
                resp = {"error": f"{backend}-backend-disabled"}
                with self.lock:
                    chosen = (ai_engine.claim_fallback(self.game, seat)
                              if kind == "claim" else
                              ai_engine.action_fallback(self.game, seat))
                used_fallback = True
            elif kind == "claim":
                chosen, req, resp, used_fallback = ai_engine.decide_claim(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late(backend, seat, "claim", p, el))
            else:
                chosen, req, resp, used_fallback = ai_engine.decide_action(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late(backend, seat, "action", p, el))
        except Exception as e:
            print(f"[warn] {backend} {kind} failed: {e!r}", file=sys.stderr)
            resp = {"error": repr(e)}
            with self.lock:
                try:
                    chosen = (ai_engine.claim_fallback(self.game, seat)
                              if kind == "claim" else
                              ai_engine.action_fallback(self.game, seat))
                except Exception:
                    chosen = "pass" if kind == "claim" else None
            used_fallback = True
        elapsed = time.time() - t0
        usage = (resp or {}).get("usage", {}) if isinstance(resp, dict) else {}
        print(f"[ai-timing] backend={backend} seat={seat} kind={kind} "
              f"elapsed={elapsed:.2f}s fallback={used_fallback} "
              f"tokens_in={usage.get('input_tokens')} "
              f"error={(resp or {}).get('error') if isinstance(resp, dict) else None}",
              flush=True)

        with self.lock:
            self.ai_busy[seat] = False
            if self.seq != gen or self.game.over:
                return
            if chosen is None:
                self._next_draw_skip()
                return
            answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
            legal = (self.game.legal_claims(seat) if kind == "claim"
                     else self.game.legal_actions(seat))
            if chosen not in legal:
                chosen = (ai_engine.claim_fallback(self.game, seat)
                          if kind == "claim" else
                          ai_engine.action_fallback(self.game, seat))
                if chosen not in legal:
                    chosen = legal[0]
                used_fallback = True
            prefix = "⚡ " if used_fallback else ""
            if kind == "claim":
                self.game.apply_claim(seat, chosen)
                label = {"hu": "胡牌", "gang": "明杠", "peng": "碰",
                         "pass": "过"}.get(chosen, chosen)
            else:
                self.game.apply_action(seat, chosen)
                label = ai_engine._action_label(chosen)
            self.last_decision[seat] = {
                "label": prefix + label,
                "reason": answers.get("reason", {}).get("choice"),
                "confidence": answers.get("action", {}).get("confidence"),
                "io": {"request": req, "response": resp},
            }
            self.decisions.append({
                "frame": len(self.frames), "seat": seat, "kind": kind,
                "backend": backend, "label": label,
                "fallback": used_fallback, "elapsed_s": round(elapsed, 2)})
            self._append_frame(
                f"seat{seat}({BACKEND_CN.get(backend)}) {label}")
            self.next_ai_action_at = time.time() + AI_MOVE_DELAY

    def _next_draw_skip(self):
        """Claim queue emptied itself (auto-only path)."""
        if self.game.turn_phase == "claim" and not self.game.claim_queue:
            self.game._next_draw()

    def human_action(self, seat, action, kind):
        if self.controllers[seat] != "human":
            return
        if kind == "claim":
            self.game.apply_claim(seat, action)
        else:
            self.game.apply_action(seat, action)
        label = (ai_engine._action_label(action) if kind != "claim" else
                 {"hu": "胡牌", "gang": "明杠", "peng": "碰",
                  "pass": "过"}.get(action, action))
        self._append_frame(f"seat{seat}(玩家) {label}")
        if self.game.phase == "handover":
            res = self.game.last_result or {}
            lbl = ("自摸" if res.get("self_drawn") else "点炮") \
                if res.get("type") == "hu" else "流局"
            w = res.get("winner")
            self._append_frame(
                f"{'座位'+str(w)+' '+lbl+'胡牌' if w is not None else lbl}")
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
            ctls = create_with or LAST_CTL.get(gid) or ["jev", "human", "jev", "jev"]
            m = Match(gid, ctls)
            MATCHES[gid] = m
        m.last_seen = time.time()
        return m


def serialize_match(match):
    with match.lock:
        g = match.game
        human = match.controllers.index("human") \
            if "human" in match.controllers else None
        reveal = g.phase in ("handover", "over")
        seats = []
        for i in range(mahjong.NUM_SEATS):
            s = g.seats[i]
            show = reveal or i == human
            seats.append({
                "seat": i, "controller": match.controllers[i],
                "points": s.points,
                "concealed": sorted(s.concealed) if show else None,
                "concealed_count": len(s.concealed),
                "last_drawn": (i == g.turn and g.last_drawn is not None),
                "melds": [[m.kind, sorted(m.tiles)] for m in s.melds],
                "discards": sorted(s.discards),
                "shanten": shanten(s.concealed) if show else None,
                "ai_thinking": match.ai_busy[i],
                "last_decision": match.last_decision[i],
            })
        my_claim = g.legal_claims(human) if human is not None else []
        my_acts = g.legal_actions(human) if human is not None else []
        return {
            "id": match.id, "phase": g.phase, "hand_no": g.hand_no,
            "dealer": g.dealer, "turn": g.turn,
            "turn_phase": g.turn_phase,
            "pending_tile": g.pending_tile,
            "pending_from": g.pending_from,
            "wall": len(g.wall),
            "match_winner": g.match_winner(), "human_seat": human,
            "seats": seats,
            "live_tiles": (g.live_tile_counts(human)
                           if human is not None else None),
            "legal_claims": my_claim,
            "legal_actions": my_acts,
            "last_result": g.last_result,
            "replay_file": os.path.basename(match._replay_path()),
            "laya_enabled": LAYA_ENABLED,
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
    if (not isinstance(ctls, list) or len(ctls) != mahjong.NUM_SEATS
            or any(c not in ("human", "jev", "laya") for c in ctls)):
        return jsonify({"ok": False, "error": "controllers must be 4 of "
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
    seat, action = body.get("seat"), str(body.get("action"))
    kind = body.get("kind", "action")
    with m.lock:
        if seat not in range(mahjong.NUM_SEATS) or m.controllers[seat] != "human":
            return jsonify({"ok": False, "error": "not a human seat"}), 400
        try:
            m.human_action(seat, action, kind)
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
    port = int(os.environ.get("PORT", "5055"))
    app.run(host="127.0.0.1", port=port, threaded=True)

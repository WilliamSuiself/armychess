"""Flask server for Blackjack (21点) with Jev / Laya AI seats.

Three seats vs the dealer; each seat is "human" or an AI backend sharing the
army-chess state+questions -> answers contract. Same production patterns as
the other arenas: 2s decision deadline + heuristic fallback, per-match replay
files on disk, in-memory live matches.
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
from rules import hand_value, card_label

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
SETTLE_PAUSE = 4.0          # seconds to view results before next round
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
        self.next_ai_action_at = 0.0
        self.settle_at = 0.0
        self.ai_busy = {s: False for s in range(3)}
        self.last_decision = {s: None for s in range(3)}
        self.frames = []
        self.decisions = []
        self.started_at = time.strftime("%Y%m%d-%H%M%S")
        self.replay_id = f"{self.started_at}_{uuid.uuid4().hex[:8]}"
        self._append_frame("对局开始 — 第1轮下注")
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
            "phase": g.phase,
            "round": g.round_no,
            "turn": g.turn,
            "hands": {str(s): list(g.seats[s].hand) for s in range(3)},
            "chips": [g.seats[s].chips for s in range(3)],
            "bets": [g.seats[s].bet for s in range(3)],
            "dealer": list(g.dealer_hand),
            "dealer_hidden": g.dealer_hole_hidden,
            "running_count": rules.running_count(g.seen),
        })
        try:
            os.makedirs(REPLAY_DIR, exist_ok=True)
            with open(self._replay_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "frames": self.frames,
                    "winner": g.winner(),
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
                if g.phase == "settle":
                    if not self.settle_at:
                        self.settle_at = time.time() + SETTLE_PAUSE
                        self._append_frame(f"第{g.round_no}轮结算")
                    elif time.time() >= self.settle_at:
                        self.settle_at = 0.0
                        g.next_round()
                        if not g.over:
                            self._append_frame(f"第{g.round_no}轮下注")
                        else:
                            self._append_frame("对局结束")
                    continue
                if g.turn is None:
                    continue
                seat = g.turn
                if self.controllers[seat] == "human" or self.ai_busy[seat]:
                    continue
                if time.time() < self.next_ai_action_at:
                    continue
                self.ai_busy[seat] = True
                kind = "bet" if g.phase == "betting" else "act"
                threading.Thread(target=self._ai_worker,
                                 args=(seat, self.seq, kind), daemon=True).start()

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
                    chosen = (ai_engine.bet_fallback(self.game, seat)
                              if kind == "bet" else
                              ai_engine.action_fallback(self.game, seat))
                used_fallback = True
            elif kind == "bet":
                chosen, req, resp, used_fallback = ai_engine.decide_bet(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late(backend, seat, "bet", p, el))
            else:
                chosen, req, resp, used_fallback = ai_engine.decide_action(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late(backend, seat, "act", p, el))
        except Exception as e:
            print(f"[warn] {backend} {kind} failed: {e!r}", file=sys.stderr)
            resp = {"error": repr(e)}
            with self.lock:
                try:
                    chosen = (ai_engine.bet_fallback(self.game, seat)
                              if kind == "bet" else
                              ai_engine.action_fallback(self.game, seat))
                except Exception:
                    chosen = 0 if kind == "bet" else "stand"
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
            answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
            prefix = "⚡ " if used_fallback else ""
            if kind == "bet":
                self.game.apply_bet(seat, chosen or 0)
                label = f"押{self.game.seats[seat].bet}"
            else:
                try:
                    self.game.apply_action(seat, chosen)
                except ValueError:
                    self.game.apply_action(seat, "stand")
                    chosen = "stand"
                label = {"hit": "要牌", "stand": "停牌",
                         "double": "加倍"}[chosen]
            self.last_decision[seat] = {
                "label": prefix + label,
                "reason": answers.get("reason", {}).get("choice"),
                "confidence": (answers.get("action", {}) or
                               answers.get("bet", {})).get("confidence"),
                "io": {"request": req, "response": resp},
            }
            self.decisions.append({
                "frame": len(self.frames), "seat": seat, "kind": kind,
                "backend": backend, "label": label,
                "fallback": used_fallback, "elapsed_s": round(elapsed, 2)})
            self._append_frame(
                f"seat{seat}({BACKEND_CN.get(backend)}) {label}")
            self.next_ai_action_at = time.time() + AI_MOVE_DELAY

    def human_bet(self, seat, amount):
        if self.controllers[seat] != "human":
            return
        self.game.apply_bet(seat, int(amount))
        self._append_frame(f"seat{seat}(玩家) 押{amount}")

    def human_action(self, seat, action):
        if self.controllers[seat] != "human":
            return
        self.game.apply_action(seat, action)
        label = {"hit": "要牌", "stand": "停牌",
                 "double": "加倍"}.get(action, action)
        self._append_frame(f"seat{seat}(玩家) {label}")
        if self.game.phase == "settle":
            self.settle_at = time.time() + SETTLE_PAUSE
            self._append_frame(f"第{self.game.round_no}轮结算")


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
        human = match.controllers.index("human") if "human" in match.controllers else None
        reveal_all = human is None or g.over
        seats = []
        for i in range(3):
            s = g.seats[i]
            total, soft = hand_value(s.hand) if s.hand else (0, False)
            seats.append({
                "seat": i, "controller": match.controllers[i],
                "chips": s.chips, "bet": s.bet,
                "hand": list(s.hand) if (reveal_all or i == human) else None,
                "hand_count": len(s.hand),
                "total": total if (reveal_all or i == human) else None,
                "soft": soft, "busted": s.busted, "stood": s.stood,
                "doubled": s.doubled, "natural": s.natural,
                "ai_thinking": match.ai_busy[i],
                "last_decision": match.last_decision[i],
            })
        dealer_shown = g.dealer_hand if not g.dealer_hole_hidden else g.dealer_hand[:1]
        return {
            "id": match.id,
            "phase": g.phase, "round": g.round_no, "turn": g.turn,
            "winner": g.winner(), "human_seat": human,
            "seats": seats,
            "dealer_hand": dealer_shown,
            "dealer_total": (hand_value(dealer_shown)[0] if dealer_shown else None),
            "dealer_hidden": g.dealer_hole_hidden,
            "running_count": rules.running_count(g.seen),
            "decks_remaining": round(len(g.shoe) / 52, 2),
            "legal_bets": g.legal_bets(human) if human is not None else [],
            "legal_actions": g.legal_actions(human) if human is not None else [],
            "replay_file": os.path.basename(match._replay_path()),
            "laya_enabled": LAYA_ENABLED,
            "history": [h for h in g.history[-15:]],
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
    kind, seat = body.get("type"), body.get("seat")
    with m.lock:
        if seat not in range(3) or m.controllers[seat] != "human":
            return jsonify({"ok": False, "error": "not a human seat"}), 400
        try:
            if kind == "bet":
                m.human_bet(seat, int(body.get("value", 0)))
            elif kind == "action":
                m.human_action(seat, body.get("action"))
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
    port = int(os.environ.get("PORT", "5053"))
    app.run(host="127.0.0.1", port=port, threaded=True)

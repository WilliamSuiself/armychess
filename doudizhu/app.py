"""Flask server for Dou Dizhu (斗地主) with Jev / Laya AI opponents.

Three seats (0/1/2); each is controlled by "human" (browser input via
/api/action) or an AI backend ("jev"/"laya") using the same
state+questions -> answers contract as army-chess / tetris-arena. Setting all
three seats to AI gives a pure spectate mode. Same production patterns as
tetris-arena: per-match replay files on disk, 2s AI decision deadline with
local-heuristic fallback, in-memory live matches.
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
import cards
import doudizhu

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
AI_MOVE_DELAY = 1.0          # min seconds between applied AI actions (watchability)
MATCH_TTL = 3 * 3600
MAX_MATCHES = 100

REPLAY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "replays")
BACKEND_CN = {"human": "玩家", "jev": "Jev", "laya": "Laya"}
ROLE_CN = {"landlord": "地主", "peasant": "农民"}

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


def _log_late_ai_result(backend, seat, what, pick, elapsed):
    label = cards.play_label(pick) if isinstance(pick, list) else pick
    print(f"[ai-timing] LATE backend={backend} seat={seat} kind={what} "
          f"total_elapsed={elapsed:.2f}s would_have_picked={label!r} "
          f"(fallback already used)", flush=True)


class Match:
    def __init__(self, mid, controllers):
        self.id = mid
        self.controllers = list(controllers)          # [seat0, seat1, seat2]
        self.game = doudizhu.Game()
        self.lock = threading.Lock()
        self.seq = 0
        self.running = True
        self.last_seen = time.time()
        self.next_ai_action_at = 0.0                  # pacing for spectate
        self.ai_busy = {s: False for s in self.game.SEATS}
        self.last_decision = {s: None for s in self.game.SEATS}
        self.frames = []
        self.decisions = []          # compact AI decision log saved into replay
        self.started_at = time.strftime("%Y%m%d-%H%M%S")
        self.replay_id = f"{self.started_at}_{uuid.uuid4().hex[:8]}"
        self._append_frame("发牌，开始叫分")
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _replay_path(self):
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", self.id or "default")[:40]
        return os.path.join(REPLAY_DIR, f"{safe}_{self.replay_id}.json")

    def _append_frame(self, label):
        g = self.game
        frame = {
            "n": len(self.frames),
            "label": label,
            "phase": g.phase,
            "turn": g.turn,
            "landlord": g.landlord,
            "hands": {str(s): list(g.hands[s]) for s in g.SEATS},
            "bottom": list(g.bottom),
            "last_play": ({"seat": g.lead_play["seat"],
                           "cards": list(g.lead_play["cards"]),
                           "type": g.lead_play["type"]}
                          if g.lead_play else None),
            "bids": dict(g.bids),
        }
        self.frames.append(frame)
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
            print(f"[warn] could not save replay for gid={self.id!r}: {e!r}",
                  file=sys.stderr)

    def _loop(self):
        while self.running:
            time.sleep(TICK_INTERVAL)
            with self.lock:
                g = self.game
                if g.phase == "over":
                    continue
                if g.turn is None:
                    continue
                seat = g.turn
                backend = self.controllers[seat]
                if backend == "human" or self.ai_busy[seat]:
                    continue
                if time.time() < self.next_ai_action_at:
                    continue
                self.ai_busy[seat] = True
                gen = self.seq
                kind = "bid" if g.phase == "bidding" else "move"
                threading.Thread(target=self._ai_worker,
                                 args=(seat, gen, kind), daemon=True).start()

    def _ai_worker(self, seat, gen, kind):
        with self.lock:
            if self.seq != gen or self.game.phase == "over":
                self.ai_busy[seat] = False
                return
        backend = self.controllers[seat]
        client = get_client(backend)
        chosen = req = resp = None
        used_fallback = False
        t0 = time.time()
        try:
            if client is None:
                resp = {"error": f"{backend}-backend-disabled"}
                with self.lock:
                    chosen = (ai_engine.bid_fallback(self.game, seat)
                              if kind == "bid" else
                              ai_engine.pick_fallback(self.game, seat))
                used_fallback = True
            elif kind == "bid":
                chosen, req, resp, used_fallback = ai_engine.decide_bid(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late_ai_result(backend, seat, "bid", p, el))
            else:
                chosen, req, resp, used_fallback = ai_engine.decide_move(
                    client, self.game, seat, deadline=AI_DECISION_DEADLINE,
                    on_late_result=lambda p, rq, rp, el:
                        _log_late_ai_result(backend, seat, "move", p, el))
        except Exception as e:
            print(f"[warn] {backend} {kind} failed: {e!r}", file=sys.stderr)
            resp = {"error": repr(e)}
            with self.lock:
                try:
                    chosen = (ai_engine.bid_fallback(self.game, seat)
                              if kind == "bid" else
                              ai_engine.pick_fallback(self.game, seat))
                except Exception:
                    chosen = None if kind == "move" else 0
            used_fallback = True
        elapsed = time.time() - t0
        usage = (resp or {}).get("usage", {}) if isinstance(resp, dict) else {}
        print(f"[ai-timing] backend={backend} seat={seat} kind={kind} "
              f"elapsed={elapsed:.2f}s fallback={used_fallback} "
              f"tokens_in={usage.get('input_tokens')} "
              f"tokens_out={usage.get('output_tokens')} "
              f"error={(resp or {}).get('error') if isinstance(resp, dict) else None}",
              flush=True)

        with self.lock:
            self.ai_busy[seat] = False
            if self.seq != gen or self.game.phase == "over":
                return
            answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
            prefix = "⚡ " if used_fallback else ""
            if kind == "bid":
                try:
                    self.game.apply_bid(seat, chosen if chosen is not None else 0)
                except ValueError:
                    self.game.apply_bid(seat, 0)
                label = {0: "不叫", 1: "叫1分", 2: "叫2分", 3: "叫3分"}.get(
                    chosen, "不叫")
                self.last_decision[seat] = {
                    "label": prefix + label,
                    "confidence": answers.get("bid", {}).get("confidence"),
                    "win_conf": answers.get("confidence_in_win", {}).get("noul"),
                    "io": {"request": req, "response": resp},
                }
                self.decisions.append({
                    "frame": len(self.frames), "seat": seat, "kind": "bid",
                    "backend": backend, "label": label,
                    "fallback": used_fallback, "elapsed_s": round(elapsed, 2),
                    "win_conf": answers.get("confidence_in_win", {}).get("noul"),
                })
                self._append_frame(f"seat{seat}({BACKEND_CN.get(backend)}) {label}")
            else:
                play = chosen
                try:
                    self.game.apply_play(seat, play)
                except ValueError:
                    # model picked something illegal post-shuffle — pass or
                    # fall back to the top heuristic legal move
                    legal = self.game.legal_plays(seat)
                    fallback2 = (ai_engine.pick_fallback(self.game, seat)
                                 if legal else None)
                    self.game.apply_play(seat, fallback2)
                    play = fallback2
                if play:
                    label = cards.play_label(play)
                else:
                    label = "不出"
                self.last_decision[seat] = {
                    "label": prefix + label,
                    "intent": answers.get("intent", {}).get("choice"),
                    "aggression": answers.get("aggression", {}).get("score"),
                    "confidence": answers.get("move", {}).get("confidence"),
                    "win_conf": answers.get("confidence_in_win", {}).get("noul"),
                    "io": {"request": req, "response": resp},
                }
                self.decisions.append({
                    "frame": len(self.frames), "seat": seat, "kind": "move",
                    "backend": backend, "label": label,
                    "fallback": used_fallback, "elapsed_s": round(elapsed, 2),
                    "intent": answers.get("intent", {}).get("choice"),
                    "aggression": answers.get("aggression", {}).get("score"),
                    "win_conf": answers.get("confidence_in_win", {}).get("noul"),
                })
                cn = BACKEND_CN.get(backend, backend)
                self._append_frame(f"seat{seat}({cn}) {label}")
                if self.game.phase == "over":
                    self._append_frame(
                        f"对局结束 — {'地主' if self.game.winner == 'landlord' else '农民'}获胜")
            self.next_ai_action_at = time.time() + AI_MOVE_DELAY

    # ---- human actions (self.lock held by caller) ----

    def human_bid(self, seat, value):
        if self.controllers[seat] != "human":
            return
        self.game.apply_bid(seat, int(value))
        label = {0: "不叫", 1: "叫1分", 2: "叫2分", 3: "叫3分"}[int(value)]
        self._append_frame(f"seat{seat}(玩家) {label}")

    def human_play(self, seat, played):
        if self.controllers[seat] != "human":
            return
        self.game.apply_play(seat, played)
        label = cards.play_label(played) if played else "不出"
        self._append_frame(f"seat{seat}(玩家) {label}")
        if self.game.phase == "over":
            self._append_frame(
                f"对局结束 — {'地主' if self.game.winner == 'landlord' else '农民'}获胜")


MATCHES = {}
_MATCHES_LOCK = threading.Lock()


def _evict_stale():
    cutoff = time.time() - MATCH_TTL
    for k in [k for k, m in MATCHES.items() if m.last_seen < cutoff]:
        print(f"[match] evicting gid={k!r} idle={time.time()-MATCHES[k].last_seen:.0f}s",
              flush=True)
        MATCHES[k].running = False
        del MATCHES[k]
    while len(MATCHES) >= MAX_MATCHES:
        oldest = min(MATCHES, key=lambda k: MATCHES[k].last_seen)
        print(f"[match] evicting gid={oldest!r} (MAX_MATCHES)", flush=True)
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
            reason = ("explicit new_match (replacing a live match)" if m is not None
                      else "explicit new_match (gid not currently tracked)"
                      if create_with is not None
                      else "gid not tracked — auto-creating (TTL eviction or "
                           "fresh sessionStorage id)")
            print(f"[match] create gid={gid!r} reason={reason} "
                  f"tracked_before={len(MATCHES)}", flush=True)
            _evict_stale()
            if m is not None:
                m.running = False
                m.seq += 1
            ctls = create_with or LAST_CTL.get(gid) or ["jev", "human", "jev"]
            m = Match(gid, ctls)
            MATCHES[gid] = m
        m.last_seen = time.time()
        return m


def seat_view(match, seat, reveal):
    g = match.game
    hand = list(g.hands[seat])
    return {
        "seat": seat,
        "controller": match.controllers[seat],
        "is_landlord": g.landlord == seat,
        "hand": hand if reveal else None,
        "hand_count": len(hand),
        "ai_thinking": match.ai_busy[seat],
        "last_decision": match.last_decision[seat],
        "bid": g.bids.get(seat),
    }


def serialize_match(match):
    with match.lock:
        g = match.game
        has_human = "human" in match.controllers
        human_seat = match.controllers.index("human") if has_human else None
        reveal_all = (not has_human) or g.phase == "over"
        seats = [seat_view(match, s, reveal_all or s == human_seat)
                 for s in g.SEATS]
        legal_bids = g.legal_bids(human_seat) if human_seat is not None else []
        can_pass = g.can_pass(human_seat) if human_seat is not None else False
        lead = g.lead_play
        return {
            "id": match.id,
            "phase": g.phase,
            "winner": g.winner,
            "winner_seat": g.winner_seat,
            "landlord": g.landlord,
            "turn": g.turn,
            "human_seat": human_seat,
            "seats": seats,
            "bottom": list(g.bottom) if g.phase != "bidding" else [],
            "bottom_count": len(g.bottom),
            "lead_play": ({"seat": lead["seat"], "cards": lead["cards"],
                           "type": lead["type"],
                           "label": cards.play_label(lead["cards"])}
                          if lead else None),
            "legal_bids": legal_bids,
            "can_pass": can_pass,
            "replay_file": os.path.basename(match._replay_path()),
            "bids": {str(k): v for k, v in g.bids.items()},
            "history": [h for h in g.history[-12:]],
            "laya_enabled": LAYA_ENABLED,
        }


# ---------- Flask routes ----------

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
        return jsonify({"ok": False, "error": "每个标签页只能操作一个人类座位"
                        "（另两个请选 Jev/Laya）"}), 400
    if "laya" in ctls and not LAYA_ENABLED:
        return jsonify({"ok": False, "error": "本服务器未启用 Laya"}), 400
    m = get_match(create_with=ctls)
    return jsonify(serialize_match(m))


@app.route("/api/action", methods=["POST"])
def api_action():
    m = get_match()
    body = request.get_json(force=True, silent=True) or {}
    kind = body.get("type")
    seat = body.get("seat")
    with m.lock:
        if seat not in m.game.SEATS or m.controllers[seat] != "human":
            return jsonify({"ok": False, "error": "not a human seat"}), 400
        try:
            if kind == "bid":
                m.human_bid(seat, int(body.get("value", 0)))
            elif kind == "play":
                m.human_play(seat, body.get("cards") or None)
            else:
                return jsonify({"ok": False, "error": "unknown action"}), 400
        except ValueError as e:
            return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify(serialize_match(m))


@app.route("/api/hint", methods=["GET"])
def api_hint():
    """Legal plays for the human's current turn (for the 提示 button)."""
    m = get_match()
    with m.lock:
        human = m.controllers.index("human") if "human" in m.controllers else None
        plays = m.game.legal_plays(human) if human is not None else []
        can_pass = m.game.can_pass(human) if human is not None else False
    return jsonify({"plays": plays, "can_pass": can_pass,
                    "labels": [cards.play_label(p) for p in plays]})


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


@app.route("/api/_debug/matches")
def debug_matches():
    with _MATCHES_LOCK:
        return jsonify({
            "count": len(MATCHES),
            "matches": [
                {"gid": k, "idle_s": round(time.time() - m.last_seen, 1),
                 "phase": m.game.phase, "winner": m.game.winner,
                 "controllers": m.controllers}
                for k, m in MATCHES.items()
            ],
        })


def _preload_laya():
    """Warm the ~1.4GB local model in the background so the first Laya match
    doesn't stall for ~17s."""
    if not LAYA_ENABLED:
        return
    def _w():
        t0 = time.time()
        c = get_client("laya")
        print(f"[boot] Laya {'ready' if c else 'unavailable'} in "
              f"{time.time()-t0:.1f}s", flush=True)
    threading.Thread(target=_w, daemon=True).start()


if __name__ == "__main__":
    _preload_laya()
    port = int(os.environ.get("PORT", "5051"))
    app.run(host="127.0.0.1", port=port, threaded=True)

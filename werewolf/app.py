"""Flask server for Werewolf (狼人杀) with Jev / Laya AI seats.

Six seats, each "human" or an AI backend sharing the army-chess
state+questions -> answers contract. Same production patterns as the other
arenas: 2s decision deadline + heuristic fallback, per-match replay files on
disk, in-memory live matches. The game itself is turn-driven across five
distinct action kinds (wolf kill vote, seer check, witch potion, day speech,
day vote) via werewolf.Game.current_actor()/step().
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
import werewolf as ww
from werewolf import ROLE_CN

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
AI_DECISION_DEADLINE = 3.0     # speeches/claims need a bit more budget than a bare choice
TICK_INTERVAL = 0.08
AI_ACTION_DELAY = 1.1
SETTLE_PAUSE = 6.0
MATCH_TTL = 3 * 3600
MAX_MATCHES = 100
N = ww.NUM_SEATS

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
        self.game = ww.Game()
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
        self._append_frame("对局开始，正在分配身份...")
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
            "night_no": g.night_no, "day_no": g.day_no,
            "alive": list(g.alive),
            "roles_revealed": [ROLE_CN[g.roles[s]] if g.role_revealed[s] else None
                               for s in range(N)],
            "log_tail": list(g.log[-3:]),
            "winner": g.winner,
        })
        try:
            os.makedirs(REPLAY_DIR, exist_ok=True)
            with open(self._replay_path(), "w", encoding="utf-8") as f:
                json.dump({
                    "frames": self.frames,
                    "winner": g.winner,
                    "full_log": g.log,
                    "roles": [ROLE_CN[r] for r in g.roles] if g.winner else None,
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
                if g.phase == "over":
                    continue
                g.step()
                if g.phase == "over":
                    self._append_frame(g.log[-1] if g.log else "对局结束")
                    continue
                actor = g.current_actor()
                if actor is None:
                    continue
                if self.controllers[actor] == "human" or self.ai_busy[actor]:
                    continue
                if time.time() < self.next_ai_action_at:
                    continue
                self.ai_busy[actor] = True
                phase = g.phase
                threading.Thread(target=self._ai_worker,
                                 args=(actor, phase, self.seq), daemon=True).start()

    def _ai_worker(self, seat, phase, gen):
        backend = self.controllers[seat]
        client = get_client(backend)
        result = None
        req = resp = None
        used_fallback = False
        t0 = time.time()
        try:
            if client is None:
                resp = {"error": f"{backend}-backend-disabled"}
                used_fallback = True
                with self.lock:
                    result = self._local_fallback(phase, seat)
            else:
                result, req, resp, used_fallback = self._call_ai(client, phase, seat, backend)
        except Exception as e:
            print(f"[warn] {backend} {phase} failed: {e!r}", file=sys.stderr)
            resp = {"error": repr(e)}
            used_fallback = True
            with self.lock:
                try:
                    result = self._local_fallback(phase, seat)
                except Exception:
                    result = None
        elapsed = time.time() - t0

        with self.lock:
            self.ai_busy[seat] = False
            if self.seq != gen or self.game.phase == "over" or self.game.phase != phase:
                return
            if self.game.current_actor() != seat:
                return
            label = self._apply_result(phase, seat, result)
            print(f"[ai-timing] backend={backend} seat={seat} kind={phase} "
                  f"elapsed={elapsed:.2f}s fallback={used_fallback} "
                  f"error={(resp or {}).get('error') if isinstance(resp, dict) else None}",
                  flush=True)
            self.last_decision[seat] = {
                "label": ("⚡ " if used_fallback else "") + (label or ""),
                "io": {"request": req, "response": resp},
            }
            self.decisions.append({
                "frame": len(self.frames), "seat": seat, "phase": phase,
                "backend": backend, "label": label,
                "fallback": used_fallback, "elapsed_s": round(elapsed, 2)})
            self._append_frame(f"[{phase}] seat{seat}({BACKEND_CN.get(backend)}) {label}")
            self.next_ai_action_at = time.time() + AI_ACTION_DELAY

    def _call_ai(self, client, phase, seat, backend):
        g = self.game
        late = lambda kind: (lambda pick, rq, rp, el: _log_late(backend, seat, kind, pick, el))
        if phase == "night_wolf":
            target, req, resp, fb = ai_engine.decide_wolf_kill(
                client, g, seat, deadline=AI_DECISION_DEADLINE, on_late_result=late("kill"))
            return target, req, resp, fb
        if phase == "night_seer":
            target, req, resp, fb = ai_engine.decide_seer_check(
                client, g, seat, deadline=AI_DECISION_DEADLINE, on_late_result=late("check"))
            return target, req, resp, fb
        if phase == "night_witch":
            action, target, req, resp, fb = ai_engine.decide_witch_action(
                client, g, seat, deadline=AI_DECISION_DEADLINE, on_late_result=late("witch"))
            return (action, target), req, resp, fb
        if phase == "day_discuss":
            text, req, resp, fb = ai_engine.decide_speech(
                client, g, seat, deadline=AI_DECISION_DEADLINE, on_late_result=late("speech"))
            return text, req, resp, fb
        if phase == "day_vote":
            target, req, resp, fb = ai_engine.decide_vote(
                client, g, seat, deadline=AI_DECISION_DEADLINE, on_late_result=late("vote"))
            return target, req, resp, fb
        return None, None, None, True

    def _local_fallback(self, phase, seat):
        g = self.game
        if phase == "night_wolf":
            return ai_engine.wolf_fallback(g, seat)
        if phase == "night_seer":
            return ai_engine.seer_fallback(g, seat)
        if phase == "night_witch":
            return ai_engine.witch_fallback(g, seat)
        if phase == "day_discuss":
            return ai_engine.speech_fallback(g, seat)
        if phase == "day_vote":
            return ai_engine.vote_fallback(g, seat)
        return None

    def _apply_result(self, phase, seat, result):
        g = self.game
        if phase == "night_wolf":
            g.submit_wolf_vote(seat, result)
            return f"投票击杀 {result}号" if result is not None else "弃票"
        if phase == "night_seer":
            verdict = g.submit_seer_check(seat, result)
            return f"查验 {result}号 → {'狼人' if verdict == 'wolf' else '好人'}"
        if phase == "night_witch":
            action, target = result
            g.submit_witch_action(seat, action, target)
            return {"save": "使用解药", "poison": f"对{target}号使用毒药",
                    "none": "不使用药水"}.get(action, action)
        if phase == "day_discuss":
            g.submit_speech(seat, result)
            return result
        if phase == "day_vote":
            g.submit_vote(seat, result)
            return f"投票 {result}号" if result is not None else "弃票"
        return None

    # ---------------- human actions ----------------

    def human_action(self, seat, phase, payload):
        if self.controllers[seat] != "human":
            raise ValueError("not a human seat")
        g = self.game
        if g.phase != phase or g.current_actor() != seat:
            raise ValueError("not your turn")
        if phase == "night_wolf":
            result = int(payload["target"])
        elif phase == "night_seer":
            result = int(payload["target"])
        elif phase == "night_witch":
            result = (payload["action"], payload.get("target"))
        elif phase == "day_discuss":
            target = payload.get("target")
            stance = payload.get("stance", "neutral")
            claim = payload.get("claim", "claim_none")
            reasoning = payload.get("reasoning", "voting_pattern")
            result = ai_engine._compose_speech(
                g, seat, int(target) if target is not None else None,
                stance, claim, reasoning)
        elif phase == "day_vote":
            target = payload.get("target")
            result = int(target) if target is not None else None
        else:
            raise ValueError("unknown phase")
        label = self._apply_result(phase, seat, result)
        self.last_decision[seat] = {"label": label, "io": None}
        self._append_frame(f"[{phase}] seat{seat}(玩家) {label}")
        self.next_ai_action_at = time.time() + 0.3


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
            ctls = create_with or LAST_CTL.get(gid) or \
                ["jev", "human", "jev", "jev", "jev", "jev"]
            m = Match(gid, ctls)
            MATCHES[gid] = m
        m.last_seen = time.time()
        return m


def serialize_match(match):
    with match.lock:
        g = match.game
        human = match.controllers.index("human") if "human" in match.controllers else None
        over = g.phase == "over"
        seats = []
        for i in range(N):
            reveal = over or g.role_revealed[i] or i == human
            seats.append({
                "seat": i, "controller": match.controllers[i],
                "alive": g.alive[i],
                "role": ROLE_CN[g.roles[i]] if reveal else None,
                "is_you": i == human,
                "ai_thinking": match.ai_busy[i],
            })
        actor = g.current_actor() if g.phase != "over" else None
        human_turn_info = None
        if human is not None and actor == human:
            phase = g.phase
            if phase == "night_wolf":
                human_turn_info = {"phase": phase, "options": [s for s in g.good_alive()]}
            elif phase == "night_seer":
                human_turn_info = {"phase": phase,
                                   "options": [s for s in g.alive_seats() if s != human]}
            elif phase == "night_witch":
                can_save = g.night_wolf_target is not None and not g.witch_save_used
                poison_opts = [s for s in g.alive_seats() if s != human] if not g.witch_poison_used else []
                human_turn_info = {"phase": phase, "can_save": can_save,
                                   "victim": g.night_wolf_target, "poison_options": poison_opts}
            elif phase == "day_discuss":
                human_turn_info = {"phase": phase,
                                   "options": [s for s in g.alive_seats() if s != human]}
            elif phase == "day_vote":
                human_turn_info = {"phase": phase,
                                   "options": [s for s in g.alive_seats() if s != human]}

        your_private = {}
        if human is not None:
            role = g.roles[human]
            if role == "wolf":
                your_private["fellow_wolves"] = g.fellow_wolves(human)
            if role == "seer":
                your_private["check_history"] = [
                    {"night": n, "seat": s, "verdict": "狼人" if v == "wolf" else "好人"}
                    for n, s, v in g.seer_history]
            if role == "witch":
                your_private["antidote_used"] = g.witch_save_used
                your_private["poison_used"] = g.witch_poison_used
                your_private["last_night_victim"] = g.night_wolf_target if g.phase == "night_witch" else None

        return {
            "id": match.id,
            "phase": g.phase, "night_no": g.night_no, "day_no": g.day_no,
            "winner": g.winner,
            "human_seat": human, "your_role": ROLE_CN[g.roles[human]] if human is not None else None,
            "your_private": your_private,
            "seats": seats,
            "log": g.log,
            "current_actor": actor,
            "human_turn": human_turn_info,
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
    phase = body.get("phase")
    human = m.controllers.index("human") if "human" in m.controllers else None
    if human is None:
        return jsonify({"ok": False, "error": "no human seat"}), 400
    with m.lock:
        try:
            m.human_action(human, phase, body)
        except (ValueError, KeyError, TypeError) as e:
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
    port = int(os.environ.get("PORT", "5057"))
    app.run(host="127.0.0.1", port=port, threaded=True)

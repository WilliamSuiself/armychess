"""AI decision layer for Werewolf — same state+questions -> answers contract.

Free-form text generation isn't part of the proven army-chess contract (only
choice/score/noul questions are), so speeches aren't literally written by the
model word-for-word. Instead each speech is a small *composition* decided by
several choice questions (who to focus on, accuse/defend/neutral, whether to
claim a role and what to claim about whom, which line of reasoning to lean
on) — the model supplies the social-deduction judgment (who to suspect, when
to fake-claim seer, when to reveal real check results), and the server
renders that judgment into a Chinese sentence. This keeps the showcase on
the model's reasoning rather than on prose generation.

Five decision points: wolf night-kill vote, seer check, witch potion action,
day speech, day vote.
"""

import concurrent.futures
import random
import time

import werewolf as ww
from werewolf import ROLE_CN

_CALL_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="ww-ai-call")

SHORT_RULES = (
    "Werewolf, 6 seats: 2 wolves, 1 seer, 1 witch, 2 villagers. Each night "
    "wolves jointly pick one seat to kill (majority vote); the seer checks "
    "one seat's alignment (wolf/good); the witch may use at most one potion "
    "per night (save the wolves' victim, OR poison someone — only one of "
    "each for the whole game). By day everyone who's alive gives one speech "
    "in turn, then votes to eliminate a seat (most votes out; ties = nobody "
    "eliminated). Wolves win once they are >= surviving good seats; good "
    "wins once all wolves are dead.")


def _role_view(game, seat):
    role = game.seat_role(seat)
    view = {"your_seat": seat, "your_role": ROLE_CN[role], "your_role_code": role}
    if role == "wolf":
        view["fellow_wolves"] = game.fellow_wolves(seat)
    if role == "seer":
        view["your_check_history"] = [
            {"night": n, "seat": s, "verdict": "狼人" if v == "wolf" else "好人"}
            for n, s, v in game.seer_history]
    if role == "witch":
        view["antidote_used"] = game.witch_save_used
        view["poison_used"] = game.witch_poison_used
    return view


def build_state(game, seat):
    return {
        "night_no": game.night_no, "day_no": game.day_no, "phase": game.phase,
        "alive_seats": game.alive_seats(),
        "dead_seats": [{"seat": s, "role": (ROLE_CN[game.roles[s]]
                                            if game.role_revealed[s] else "未公开")}
                       for s in range(game.n) if not game.alive[s]],
        "num_wolves_alive": len(game.wolves_alive()),
        "num_good_alive": len(game.good_alive()),
        "public_transcript": list(game.log),
        **_role_view(game, seat),
        "rules": SHORT_RULES,
    }


# ---------------- night: wolf kill vote ----------------

def build_wolf_questions(options):
    return {
        "kill": {
            "type": "choice",
            "instructions":
                "Pick who the wolves should kill tonight. Prioritize the "
                "seer or witch if you can identify/guess them from the day's "
                "speeches (a confirmed seer claim is especially dangerous to "
                "you) — otherwise pick whoever seems most likely to be "
                "leading the good side's logic.",
            "criteria": {f"m{i}": f"{s}号" for i, s in enumerate(options)},
        },
    }


def wolf_fallback(game, seat):
    targets = [s for s in game.good_alive()]
    return random.choice(targets) if targets else random.choice(game.alive_seats())


# ---------------- night: seer check ----------------

def build_seer_questions(options):
    return {
        "check": {
            "type": "choice",
            "instructions":
                "Pick a living seat to check tonight (not yourself). Prefer "
                "seats you haven't checked yet and whose behavior is "
                "ambiguous — checking an already-obvious villager wastes "
                "the night.",
            "criteria": {f"m{i}": f"{s}号" for i, s in enumerate(options)},
        },
    }


def seer_fallback(game, seat):
    checked = {s for _, s, _ in game.seer_history}
    pool = [s for s in game.alive_seats() if s != seat and s not in checked]
    pool = pool or [s for s in game.alive_seats() if s != seat]
    return random.choice(pool)


# ---------------- night: witch potion ----------------

def build_witch_questions(game, poison_options):
    criteria = {}
    if game.night_wolf_target is not None and not game.witch_save_used:
        criteria["m_save"] = f"使用解药救下 {game.night_wolf_target}号（本局唯一一瓶）"
    criteria["m_none"] = "不使用药水"
    if not game.witch_poison_used:
        for i, s in enumerate(poison_options):
            criteria[f"m_poison_{i}"] = f"对 {s}号 使用毒药（本局唯一一瓶）"
    return {
        "action": {
            "type": "choice",
            "instructions":
                "Choose at most one potion action for tonight. Saving tonight's "
                "victim reveals nothing publicly but costs your only antidote — "
                "worth it early or if you're confident the victim is good. "
                "Poisoning is powerful against a seat you're confident is a "
                "wolf (e.g. from a seer claim you trust), but wastes your only "
                "poison if you're wrong.",
            "criteria": criteria,
        },
    }


def witch_fallback(game, seat):
    if game.night_wolf_target is not None and not game.witch_save_used and game.night_no <= 2:
        return "save", None
    return "none", None


def _parse_witch(resp, poison_options):
    answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
    key = str(answers.get("action", {}).get("choice", "m_none"))
    if key == "m_save":
        return "save", None
    if key.startswith("m_poison_"):
        try:
            idx = int(key.rsplit("_", 1)[1])
        except ValueError:
            idx = 0
        idx = max(0, min(idx, len(poison_options) - 1)) if poison_options else 0
        return ("poison", poison_options[idx]) if poison_options else ("none", None)
    return "none", None


# ---------------- day: speech ----------------

REASONING_LABELS = {
    "voting_pattern": "这几轮的投票站边很能说明问题",
    "death_timing": "昨晚死的人很关键，死亡顺序已经说明了一些事情",
    "behavior_suspicious": "刚才的发言逻辑很不自然，前后矛盾",
    "trust_me": "我说的都是实话，希望大家相信我的判断",
    "last_night_info": "结合昨晚的情况来看，我的判断是有依据的",
}


def build_speech_questions(game, seat, target_options):
    role = game.seat_role(seat)
    claim_criteria = {"claim_none": "不跳身份，只讲逻辑"}
    if target_options:
        claim_criteria["claim_seer_good"] = "跳预言家，宣称查验目标是「好人」"
        claim_criteria["claim_seer_wolf"] = "跳预言家，宣称查验目标是「狼人」"
    claim_criteria["claim_witch"] = "跳女巫身份"
    return {
        "target": {
            "type": "choice",
            "instructions":
                "Who should this speech focus on? If claiming seer, this is "
                "also the seat your claimed check result is about.",
            "criteria": {f"m{i}": f"{s}号" for i, s in enumerate(target_options)} or {"m0": "无人"},
        },
        "stance": {
            "type": "choice",
            "instructions": "Your stance toward the target seat this speech.",
            "criteria": {"accuse": "指控/怀疑对方", "defend": "为对方辩护/澄清",
                        "neutral": "中立分析，不下定论"},
        },
        "claim": {
            "type": "choice",
            "instructions":
                "Whether to claim a special role this speech. Wolves may "
                "bluff-claim seer to create confusion or protect a wolf "
                "teammate; the real seer usually benefits from revealing "
                "true checks to build credibility (but can also hide for "
                "safety). Only claim once per game for consistency.",
            "criteria": claim_criteria,
        },
        "reasoning": {
            "type": "choice",
            "instructions": "Main line of reasoning to cite.",
            "criteria": {k: v for k, v in REASONING_LABELS.items()},
        },
        "confidence": {
            "type": "noul",
            "instructions": "How confident you are in this read, 0..1.",
        },
    }


def _compose_speech(game, seat, target, stance, claim, reasoning_key):
    parts = []
    role_cn = ROLE_CN[game.seat_role(seat)]
    if claim == "claim_seer_good" and target is not None:
        parts.append(f"我是预言家，昨晚查验了{target}号，他是好人。")
    elif claim == "claim_seer_wolf" and target is not None:
        parts.append(f"我是预言家，昨晚查验了{target}号，他是狼人！")
    elif claim == "claim_witch":
        parts.append("我是女巫，手上的药水情况大家听我分析。")

    if target is not None:
        if stance == "accuse":
            parts.append(f"我怀疑{target}号，大家重点关注一下他。")
        elif stance == "defend":
            parts.append(f"我觉得{target}号是被冤枉的，不应该投他。")
        else:
            parts.append(f"关于{target}号，我觉得还需要再观察。")
    parts.append(REASONING_LABELS.get(reasoning_key, ""))
    return "".join(p for p in parts if p)


def speech_fallback(game, seat):
    pool = [s for s in game.alive_seats() if s != seat]
    target = random.choice(pool) if pool else None
    role = game.seat_role(seat)
    if role == "seer" and game.seer_history:
        night, chk_seat, verdict = game.seer_history[-1]
        if chk_seat in game.alive_seats():
            claim = "claim_seer_wolf" if verdict == "wolf" else "claim_seer_good"
            stance = "accuse" if verdict == "wolf" else "defend"
            return _compose_speech(game, seat, chk_seat, stance, claim, "last_night_info")
    stance = random.choice(["accuse", "defend", "neutral"])
    return _compose_speech(game, seat, target, stance, "claim_none", "voting_pattern")


# ---------------- day: vote ----------------

def build_vote_questions(options):
    return {
        "vote": {
            "type": "choice",
            "instructions":
                "Vote to eliminate one living seat based on the day's "
                "speeches and any claims made. Vote with the seer's claimed "
                "reads if you trust them; wolves should steer votes toward "
                "good seats (especially a credible seer) while avoiding "
                "exposing themselves.",
            "criteria": {f"m{i}": f"{s}号" for i, s in enumerate(options)},
        },
    }


def vote_fallback(game, seat):
    role = game.seat_role(seat)
    pool = [s for s in game.alive_seats() if s != seat]
    if role == "wolf":
        good_targets = [s for s in pool if s not in game.wolves_alive()]
        if good_targets:
            return random.choice(good_targets)
    elif role == "seer" and game.seer_history:
        for night, chk_seat, verdict in reversed(game.seer_history):
            if verdict == "wolf" and chk_seat in pool:
                return chk_seat
    return random.choice(pool) if pool else None


# ---------------- generic bounded-call plumbing ----------------

def _parse_choice(resp, options, key, default_key="m0"):
    answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
    try:
        idx = int(str(answers.get(key, {}).get("choice", default_key)).lstrip("m"))
    except (ValueError, AttributeError):
        idx = 0
    return options[max(0, min(idx, len(options) - 1))]


def _call_with_deadline(client, state, questions, deadline, on_late_result, parse):
    request = {"state": state, "questions": questions}
    if deadline is None:
        return parse(client.system_one(state=state, questions=questions)), request, None, False
    t0 = time.time()
    fut = _CALL_EXECUTOR.submit(client.system_one, state=state, questions=questions)
    try:
        resp = fut.result(timeout=deadline)
    except concurrent.futures.TimeoutError:
        if on_late_result:
            def _done(f):
                try:
                    r = f.result()
                except Exception as e:
                    r = {"error": repr(e)}
                on_late_result(parse(r), request, r, time.time() - t0)
            fut.add_done_callback(_done)
        return None, request, {"fallback": "deadline_exceeded"}, True
    except Exception as e:
        return None, request, {"error": repr(e)}, True
    return parse(resp), request, resp, False


def decide_wolf_kill(client, game, seat, deadline=None, on_late_result=None):
    options = [s for s in game.good_alive()] or game.alive_seats()
    if len(options) == 1:
        return options[0], None, {"auto": "only_option"}, False
    random.shuffle(options)
    state = build_state(game, seat)
    questions = build_wolf_questions(options)
    parse = lambda r: _parse_choice(r, options, "kill")
    picked, req, resp, fb = _call_with_deadline(client, state, questions, deadline, on_late_result, parse)
    return (picked if not fb else wolf_fallback(game, seat)), req, resp, fb


def decide_seer_check(client, game, seat, deadline=None, on_late_result=None):
    options = [s for s in game.alive_seats() if s != seat]
    if len(options) == 1:
        return options[0], None, {"auto": "only_option"}, False
    random.shuffle(options)
    state = build_state(game, seat)
    questions = build_seer_questions(options)
    parse = lambda r: _parse_choice(r, options, "check")
    picked, req, resp, fb = _call_with_deadline(client, state, questions, deadline, on_late_result, parse)
    return (picked if not fb else seer_fallback(game, seat)), req, resp, fb


def decide_witch_action(client, game, seat, deadline=None, on_late_result=None):
    poison_options = [s for s in game.alive_seats() if s != seat] if not game.witch_poison_used else []
    can_save = game.night_wolf_target is not None and not game.witch_save_used
    if not can_save and not poison_options:
        return "none", None, None, {"auto": "no_options"}, False
    random.shuffle(poison_options)
    state = build_state(game, seat)
    questions = build_witch_questions(game, poison_options)
    parse = lambda r: _parse_witch(r, poison_options)
    picked, req, resp, fb = _call_with_deadline(client, state, questions, deadline, on_late_result,
                                                lambda r: parse(r))
    if fb:
        action, target = witch_fallback(game, seat)
    else:
        action, target = picked
    return action, target, req, resp, fb


def decide_speech(client, game, seat, deadline=None, on_late_result=None):
    options = [s for s in game.alive_seats() if s != seat]
    state = build_state(game, seat)
    questions = build_speech_questions(game, seat, options)

    def parse(resp):
        answers = resp.get("answers", {}) if isinstance(resp, dict) else {}
        t_idx = str(answers.get("target", {}).get("choice", "m0"))
        try:
            idx = int(t_idx.lstrip("m"))
        except ValueError:
            idx = 0
        target = options[max(0, min(idx, len(options) - 1))] if options else None
        stance = str(answers.get("stance", {}).get("choice", "neutral"))
        claim = str(answers.get("claim", {}).get("choice", "claim_none"))
        reasoning = str(answers.get("reasoning", {}).get("choice", "voting_pattern"))
        return _compose_speech(game, seat, target, stance, claim, reasoning)

    text, req, resp, fb = _call_with_deadline(client, state, questions, deadline, on_late_result, parse)
    return (text if not fb else speech_fallback(game, seat)), req, resp, fb


def decide_vote(client, game, seat, deadline=None, on_late_result=None):
    options = [s for s in game.alive_seats() if s != seat]
    if not options:
        return None, None, {"auto": "no_options"}, False
    random.shuffle(options)
    state = build_state(game, seat)
    questions = build_vote_questions(options)
    parse = lambda r: _parse_choice(r, options, "vote")
    picked, req, resp, fb = _call_with_deadline(client, state, questions, deadline, on_late_result, parse)
    return (picked if not fb else vote_fallback(game, seat)), req, resp, fb

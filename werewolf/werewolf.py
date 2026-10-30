"""Core Werewolf (狼人杀) engine: roles, night/day phases, win conditions.

Six seats, classic simplified ruleset (same family as other army-chess
arenas: pure rules here, no Flask/AI deps, testable standalone):

  - 2 Werewolves (狼人) — each night jointly pick one seat to kill
    (majority vote among living wolves; ties broken randomly)
  - 1 Seer (预言家) — each night checks one seat, learns "狼人" or "好人"
  - 1 Witch (女巫) — has ONE antidote (解药) and ONE poison (毒药) for the
    whole game; can use at most one potion per night (simplification vs.
    some house rules that allow both in the same night)
  - 2 Villagers (平民) — no night action

Day phase: every living seat gives one speech (in seat order), then
everyone votes; the seat with the most votes is eliminated (ties =>
nobody eliminated, documented simplification — no runoff round).

Win conditions: wolves win once they are >= the number of surviving
good-aligned seats; good wins once all wolves are dead.
"""

import random

NUM_SEATS = 6
ROLES = ["wolf", "wolf", "seer", "witch", "villager", "villager"]
ROLE_CN = {"wolf": "狼人", "seer": "预言家", "witch": "女巫", "villager": "平民"}


def is_good(role):
    return role != "wolf"


class Game:
    def __init__(self, rng=None):
        self.rng = rng or random.Random()
        self.n = NUM_SEATS
        roles = list(ROLES)
        self.rng.shuffle(roles)
        self.roles = roles  # seat -> role
        self.alive = [True] * self.n
        # Role is only publicly known once revealed — vote-outs reveal
        # immediately (standard rule), night kills stay a mystery until the
        # game ends. Mirrors what a real table would actually know.
        self.role_revealed = [False] * self.n
        self.log = []                 # public transcript: list of str
        self.night_no = 0
        self.day_no = 0
        self.phase = "night_wolf"
        self.winner = None            # None | "wolves" | "good"

        # per-night scratch state (reset in _start_night)
        self.wolf_votes = {}          # wolf seat -> target seat
        self.night_wolf_target = None
        self.seer_done = False
        self.seer_last_result = None  # (checked_seat, "wolf"/"good") for this night
        self.seer_history = []        # all (night_no, seat, verdict) the seer has learned
        self.witch_done = False
        self.witch_action = None      # "none" | "save" | "poison"
        self.witch_poison_target = None
        self.witch_save_used = False
        self.witch_poison_used = False
        self.last_deaths = []         # seats that died last resolved night/day

        # day scratch state
        self.speech_order = []
        self.speech_idx = 0
        self.speeches = []            # this day's [{seat, text}]
        self.votes = {}               # seat -> target seat (or None = abstain)

        self._log(f"夜幕降临，第{self.night_no + 1}夜开始。")
        self.night_no = 1

    # ---------------- helpers ----------------

    def _log(self, text):
        self.log.append(text)

    def seat_role(self, seat):
        return self.roles[seat]

    def alive_seats(self):
        return [s for s in range(self.n) if self.alive[s]]

    def wolves_alive(self):
        return [s for s in self.alive_seats() if self.roles[s] == "wolf"]

    def good_alive(self):
        return [s for s in self.alive_seats() if self.roles[s] != "wolf"]

    def seer_seat(self):
        return self.roles.index("seer")

    def witch_seat(self):
        return self.roles.index("witch")

    def fellow_wolves(self, seat):
        return [s for s in self.alive_seats() if s != seat and self.roles[s] == "wolf"]

    def check_win(self):
        if not self.wolves_alive():
            self.winner = "good"
            self.phase = "over"
            self.role_revealed = [True] * self.n
            self._log("狼人全部出局，好人阵营获胜！")
            return True
        if len(self.wolves_alive()) >= len(self.good_alive()):
            self.winner = "wolves"
            self.phase = "over"
            self.role_revealed = [True] * self.n
            self._log("狼人数量已不少于好人，狼人阵营获胜！")
            return True
        return False

    # ---------------- turn-driver ----------------

    def current_actor(self):
        """Seat whose action is pending, or None if the phase is ready to
        resolve/advance. app.py polls this to know whether to wait for a
        human click or dispatch an AI worker."""
        if self.phase == "night_wolf":
            for s in self.wolves_alive():
                if s not in self.wolf_votes:
                    return s
            return None
        if self.phase == "night_seer":
            s = self.seer_seat()
            return s if self.alive[s] and not self.seer_done else None
        if self.phase == "night_witch":
            s = self.witch_seat()
            return s if self.alive[s] and not self.witch_done else None
        if self.phase == "day_discuss":
            return self.speech_order[self.speech_idx] if self.speech_idx < len(self.speech_order) else None
        if self.phase == "day_vote":
            for s in self.alive_seats():
                if s not in self.votes:
                    return s
            return None
        return None

    def step(self):
        """Resolve/transition phases that have no pending actor. Called on
        every server tick; loops internally (bounded) so a chain of dead
        roles (e.g. seer AND witch already dead) skips in one call."""
        for _ in range(8):
            if self.phase == "over":
                return
            if self.current_actor() is not None:
                return
            self._advance()

    def _advance(self):
        if self.phase == "night_wolf":
            self._resolve_wolf_vote()
            self.phase = "night_seer"
        elif self.phase == "night_seer":
            self.phase = "night_witch"
        elif self.phase == "night_witch":
            self._finish_night()
        elif self.phase == "day_discuss":
            self.phase = "day_vote"
        elif self.phase == "day_vote":
            self._finish_vote()

    def _resolve_wolf_vote(self):
        if not self.wolf_votes:
            self.night_wolf_target = None
            return
        tally = {}
        for t in self.wolf_votes.values():
            tally[t] = tally.get(t, 0) + 1
        best = max(tally.values())
        tied = [t for t, c in tally.items() if c == best]
        self.night_wolf_target = self.rng.choice(tied)

    def _finish_night(self):
        deaths = set()
        victim = self.night_wolf_target
        saved = (self.witch_action == "save" and victim is not None)
        if victim is not None and not saved:
            deaths.add(victim)
        if self.witch_action == "poison" and self.witch_poison_target is not None:
            deaths.add(self.witch_poison_target)
        for s in deaths:
            self.alive[s] = False
        self.last_deaths = sorted(deaths)
        self.day_no += 1
        if deaths:
            self._log(f"天亮了，第{self.day_no}天。昨夜 "
                      + "、".join(f"{s}号" for s in sorted(deaths)) + " 倒下了。")
        else:
            self._log(f"天亮了，第{self.day_no}天。昨夜是平安夜，无人死亡。")
        if self.check_win():
            return
        self.speech_order = self.alive_seats()
        self.speech_idx = 0
        self.speeches = []
        self.votes = {}
        self.phase = "day_discuss"

    def _finish_vote(self):
        detail = "、".join(f"{s}号→{('弃票' if t is None else str(t)+'号')}"
                          for s, t in sorted(self.votes.items()))
        self._log(f"投票明细：{detail}")
        tally = {}
        for v in self.votes.values():
            if v is None:
                continue
            tally[v] = tally.get(v, 0) + 1
        if not tally:
            self._log("无人投票，本轮无人出局。")
        else:
            best = max(tally.values())
            tied = [t for t, c in tally.items() if c == best]
            if len(tied) > 1:
                self._log("票数持平（" + "、".join(f"{s}号" for s in tied) + "），本轮无人出局。")
            else:
                out = tied[0]
                self.alive[out] = False
                self.role_revealed[out] = True
                self.last_deaths = [out]
                self._log(f"投票结果：{out}号被放逐，身份是「{ROLE_CN[self.roles[out]]}」。")
        if self.check_win():
            return
        self._start_night()

    def _start_night(self):
        self.night_no += 1
        self.wolf_votes = {}
        self.night_wolf_target = None
        self.seer_done = False
        self.seer_last_result = None
        self.witch_done = False
        self.witch_action = None
        self.witch_poison_target = None
        self.last_deaths = []
        self._log(f"夜幕降临，第{self.night_no}夜开始。")
        self.phase = "night_wolf"

    # ---------------- actions ----------------

    def submit_wolf_vote(self, seat, target):
        if self.phase != "night_wolf" or seat not in self.wolves_alive():
            raise ValueError("not a wolf's turn")
        if target not in self.alive_seats():
            raise ValueError("illegal target")
        self.wolf_votes[seat] = target

    def submit_seer_check(self, seat, target):
        if self.phase != "night_seer" or seat != self.seer_seat():
            raise ValueError("not the seer's turn")
        if target not in self.alive_seats() or target == seat:
            raise ValueError("illegal target")
        verdict = "wolf" if self.roles[target] == "wolf" else "good"
        self.seer_last_result = (target, verdict)
        self.seer_history.append((self.night_no, target, verdict))
        self.seer_done = True
        return verdict

    def submit_witch_action(self, seat, action, poison_target=None):
        if self.phase != "night_witch" or seat != self.witch_seat():
            raise ValueError("not the witch's turn")
        if action == "save":
            if self.witch_save_used or self.night_wolf_target is None:
                raise ValueError("no antidote available")
            self.witch_save_used = True
        elif action == "poison":
            if self.witch_poison_used:
                raise ValueError("no poison available")
            if poison_target not in self.alive_seats():
                raise ValueError("illegal poison target")
            self.witch_poison_used = True
            self.witch_poison_target = poison_target
        elif action != "none":
            raise ValueError("unknown witch action")
        self.witch_action = action
        self.witch_done = True

    def submit_speech(self, seat, text):
        if self.phase != "day_discuss" or self.current_actor() != seat:
            raise ValueError("not your turn to speak")
        self.speeches.append({"seat": seat, "text": text})
        self._log(f"{seat}号：{text}")
        self.speech_idx += 1

    def submit_vote(self, seat, target):
        if self.phase != "day_vote" or seat not in self.alive_seats():
            raise ValueError("not your turn to vote")
        if target is not None and target not in self.alive_seats():
            raise ValueError("illegal vote target")
        self.votes[seat] = target

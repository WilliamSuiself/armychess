"""Dou Dizhu (斗地主) game engine — pure rules, no AI/network.

3 seats (0,1,2). One landlord vs two peasants. Flow:
  deal -> bidding (each seat bids once: 不叫/1/2/3, must beat current max;
         all-pass auto-redeals) -> landlord takes the 3 bottom cards and leads ->
  turns circulate; a follow must beat the leading play or pass; two consecutive
  passes reset the lead -> first empty hand decides: landlord out = landlord
  wins, either peasant out = peasants win.
"""

import time

import cards


class Game:
    SEATS = (0, 1, 2)

    def __init__(self, rng=None):
        import random as _r
        self.rng = rng or _r.Random()
        self.hands = {s: [] for s in self.SEATS}
        self.bottom = []                    # 3 底牌
        self.landlord = None
        self.phase = "bidding"              # bidding | play | over
        self.turn = None
        self.lead_play = None               # {"seat","cards","type","main","length"}
        self.pass_streak = 0
        self.bid_order = []                 # seats in bidding order
        self.bid_idx = 0
        self.bids = {}                      # seat -> score (0 = 不叫)
        self.current_max_bid = 0
        self.winner = None                  # "landlord" | "peasant"
        self.winner_seat = None
        self.deal_count = 0
        self.history = []                   # {"seat","kind",...}
        self._deal()

    # ---------- dealing / bidding ----------

    def _deal(self):
        deck = cards.new_deck(self.rng)
        self.hands = {s: cards.sort_cards(deck[s * 17:(s + 1) * 17]) for s in self.SEATS}
        self.bottom = cards.sort_cards(deck[51:])
        self.landlord = None
        self.phase = "bidding"
        self.turn = None
        self.lead_play = None
        self.pass_streak = 0
        first = self.rng.randrange(3)
        self.bid_order = [first, (first + 1) % 3, (first + 2) % 3]
        self.bid_idx = 0
        self.bids = {}
        self.current_max_bid = 0
        self.winner = None
        self.winner_seat = None
        self.deal_count += 1
        self.turn = self.bid_order[0]
        self.history.append({"kind": "deal", "n": self.deal_count})

    def legal_bids(self, seat):
        if self.phase != "bidding" or seat != self.turn:
            return []
        return [v for v in range(4) if v == 0 or v > self.current_max_bid]

    def apply_bid(self, seat, value):
        if value not in self.legal_bids(seat):
            raise ValueError(f"illegal bid {value} for seat {seat}")
        self.bids[seat] = value
        self.current_max_bid = max(self.current_max_bid, value)
        self.history.append({"kind": "bid", "seat": seat, "value": value})
        self.bid_idx += 1

        if self.bid_idx < len(self.bid_order):
            self.turn = self.bid_order[self.bid_idx]
            return

        # bidding finished
        if self.current_max_bid == 0:
            # nobody called -> reshuffle and re-deal automatically
            self._deal()
            return
        self.landlord = max(self.bids, key=lambda s: self.bids[s])
        self.hands[self.landlord] = cards.sort_cards(self.hands[self.landlord] + self.bottom)
        self.history.append({"kind": "landlord", "seat": self.landlord,
                             "bottom": list(self.bottom)})
        self.phase = "play"
        self.turn = self.landlord

    # ---------- playing ----------

    def _hand_has_all(self, seat, cards_played):
        cnt = cards.counts(self.hands[seat])
        need = cards.counts(cards_played)
        return all(cnt.get(r, 0) >= v for r, v in need.items())

    def must_play(self, seat):
        """True when `seat` leads (no pass option)."""
        return self.lead_play is None

    def can_pass(self, seat):
        return (self.phase == "play" and seat == self.turn
                and self.lead_play is not None)

    def legal_plays(self, seat):
        """Card lists `seat` may play right now ([] = may only pass)."""
        if self.phase != "play" or seat != self.turn:
            return []
        if self.lead_play is None:
            return cards.all_lead_plays(self.hands[seat])
        return cards.follow_plays(self.hands[seat], self.lead_play)

    def apply_play(self, seat, cards_played):
        """cards_played=None/[] means pass. Returns the classified play or None."""
        if self.phase != "play" or seat != self.turn:
            raise ValueError("not your turn")
        if not cards_played:
            if not self.can_pass(seat):
                raise ValueError("must play — you are leading")
            self.pass_streak += 1
            self.history.append({"kind": "pass", "seat": seat})
            label_play = None
        else:
            play = cards.classify(cards_played)
            if play is None:
                raise ValueError("illegal card combination")
            if not self._hand_has_all(seat, cards_played):
                raise ValueError("cards not in hand")
            if self.lead_play is not None and not cards.beats(play, self.lead_play):
                raise ValueError("does not beat the current play")
            for c in cards_played:
                self.hands[seat].remove(c)
            play["seat"] = seat
            play["cards"] = list(cards_played)
            self.lead_play = play
            self.pass_streak = 0
            self.history.append({"kind": "play", "seat": seat,
                                 "cards": list(cards_played),
                                 "type": play["type"]})
            label_play = play

        self.turn = (seat + 1) % 3
        if self.pass_streak >= 2 and self.lead_play is not None:
            self.lead_play = None
            self.pass_streak = 0
            self.history.append({"kind": "lead_reset"})

        if label_play is not None and not self.hands[seat]:
            self.phase = "over"
            self.winner_seat = seat
            self.winner = "landlord" if seat == self.landlord else "peasant"
            self.history.append({"kind": "over", "winner": self.winner})
        return label_play


SEAT_CN = {0: "下家", 1: "对家", 2: "上家"}


def brief_history(game, n=10):
    """Compact last-n actions for AI state."""
    out = []
    for h in game.history[-n:]:
        if h["kind"] == "bid":
            out.append(f"seat{h['seat']}叫{h['value']}分" if h["value"] else f"seat{h['seat']}不叫")
        elif h["kind"] == "play":
            out.append(f"seat{h['seat']}出{cards.play_label(h['cards'])}")
        elif h["kind"] == "pass":
            out.append(f"seat{h['seat']}不出")
        elif h["kind"] == "landlord":
            out.append(f"seat{h['seat']}成为地主")
        elif h["kind"] == "deal":
            out.append("重新发牌")
    return out

"""Dou Dizhu (斗地主) card utilities.

Card encoding: 2-char strings — suit char + rank char.
  suits: s=♠ h=♥ d=♦ c=♣ ; jokers: "js"=小王(small), "jb"=大王(big)
  ranks: 3456789TJQKA2 -> values 3..15 ; jokers -> 16/17

Play types (type, main_rank, length):
  single pair triple triple1(三带一) triple2(三带二)
  straight(≥5单顺) pair_seq(≥3连对)
  airplane(≥2三顺) airplane1(三顺+同数单) airplane2(三顺+同数对)
  four_two(四带二单) four_two_pairs(四带二对)
  bomb(四张) rocket(双王)

Comparison rule: same type AND same length -> higher main wins;
bomb beats any non-bomb/non-rocket; rocket beats everything.
"""

import random
from collections import Counter

SUITS = ("s", "h", "d", "c")
RCHARS = "3456789TJQKA2"
RANK_OF_CHAR = {ch: i + 3 for i, ch in enumerate(RCHARS)}
CHAR_OF_RANK = {i + 3: ch for i, ch in enumerate(RCHARS)}
SMALL_JOKER, BIG_JOKER = 16, 17
MAX_SEQ_RANK = 14  # A — 2 and jokers can never be part of a sequence

RANK_LABEL = {**{r: CHAR_OF_RANK[r] for r in range(3, 16)},
              16: "小王", 17: "大王"}
RANK_LABEL[10] = "10"

SUIT_SYMBOL = {"s": "♠", "h": "♥", "d": "♦", "c": "♣", "j": ""}

TYPE_CN = {
    "single": "单", "pair": "对", "triple": "三", "triple1": "三带一",
    "triple2": "三带二", "straight": "顺子", "pair_seq": "连对",
    "airplane": "飞机", "airplane1": "飞机带单", "airplane2": "飞机带对",
    "four_two": "四带二", "four_two_pairs": "四带二对",
    "bomb": "炸弹", "rocket": "王炸",
}


def rank_of(card):
    if card[0] == "j":
        return SMALL_JOKER if card[1] == "s" else BIG_JOKER
    return RANK_OF_CHAR[card[1]]


def card_label(card):
    """Display string, e.g. ♠A / ♥10 / 小王."""
    r = rank_of(card)
    if r >= 16:
        return RANK_LABEL[r]
    return SUIT_SYMBOL[card[0]] + RANK_LABEL[r]


def is_red(card):
    return card[0] in ("h", "d") or card == "js"


def sort_cards(cards):
    return sorted(cards, key=lambda c: (rank_of(c), c))


def new_deck(rng=None):
    rng = rng or random
    deck = [s + ch for s in SUITS for ch in RCHARS] + ["js", "jb"]
    rng.shuffle(deck)
    return deck


def counts(hand):
    return Counter(rank_of(c) for c in hand)


def _is_consecutive(ranks):
    """ranks: sorted unique list, all <= A, strictly consecutive."""
    if any(r > MAX_SEQ_RANK for r in ranks):
        return False
    return all(ranks[i + 1] == ranks[i] + 1 for i in range(len(ranks) - 1))


def classify(cards):
    """Return {"type","main","length"} if `cards` is a legal play, else None."""
    n = len(cards)
    if n == 0:
        return None
    cnt = counts(cards)
    distinct = len(cnt)
    cvals = sorted(cnt.values(), reverse=True)
    top_rank = max(cnt)

    if n == 1:
        return {"type": "single", "main": top_rank, "length": 1}
    if n == 2:
        if set(cnt) == {16, 17}:
            return {"type": "rocket", "main": 17, "length": 1}
        if distinct == 1:
            return {"type": "pair", "main": top_rank, "length": 1}
        return None
    if n == 3 and distinct == 1:
        return {"type": "triple", "main": top_rank, "length": 1}
    if n == 4:
        if distinct == 1:
            return {"type": "bomb", "main": top_rank, "length": 1}
        if cvals == [3, 1]:
            main = [r for r, v in cnt.items() if v == 3][0]
            return {"type": "triple1", "main": main, "length": 1}
        return None
    if n == 5 and cvals == [3, 2]:
        main = [r for r, v in cnt.items() if v == 3][0]
        return {"type": "triple2", "main": main, "length": 1}

    if n >= 5 and distinct == n and _is_consecutive(sorted(cnt)):
        return {"type": "straight", "main": top_rank, "length": n}
    if n >= 6 and n % 2 == 0 and all(v == 2 for v in cnt.values()) \
            and _is_consecutive(sorted(cnt)):
        return {"type": "pair_seq", "main": top_rank, "length": n // 2}

    # airplane family: k consecutive triples (rank<=A) + 0 / k singles / k pairs
    triple_ranks = sorted(r for r, v in cnt.items() if v >= 3 and r <= MAX_SEQ_RANK)
    best = None
    for k in range(2, len(triple_ranks) + 1):
        for i in range(len(triple_ranks) - k + 1):
            window = triple_ranks[i:i + k]
            if not _is_consecutive(window):
                continue
            rest = n - 3 * k
            other_cnt = {r: v for r, v in cnt.items() if r not in window}
            ok_type = None
            if rest == 0:
                ok_type = "airplane"
            elif rest == k:
                ok_type = "airplane1"
            elif rest == 2 * k and sum(v // 2 for v in other_cnt.values()) == k \
                    and all(v % 2 == 0 for v in other_cnt.values()):
                ok_type = "airplane2"
            if ok_type:
                cand = {"type": ok_type, "main": window[-1], "length": k}
                if best is None or k > best["length"]:
                    best = cand
    if best:
        return best

    # 四带二
    if n == 6 and cvals[0] == 4:
        main = [r for r, v in cnt.items() if v == 4][0]
        return {"type": "four_two", "main": main, "length": 1}
    if n == 8 and cvals == [4, 2, 2]:
        main = [r for r, v in cnt.items() if v == 4][0]
        return {"type": "four_two_pairs", "main": main, "length": 1}
    return None


def beats(play, last):
    """True if classified `play` beats classified `last` play."""
    if play["type"] == "rocket":
        return True
    if last["type"] == "rocket":
        return False
    if play["type"] == "bomb" and last["type"] != "bomb":
        return True
    if play["type"] != last["type"] or play["length"] != last["length"]:
        return False
    return play["main"] > last["main"]


# ---------- move enumeration ----------

def _groups(hand):
    """rank -> sorted card list."""
    g = {}
    for c in hand:
        g.setdefault(rank_of(c), []).append(c)
    return g


def _seq_windows(ranks, length):
    """All consecutive windows of `length` inside sorted `ranks` (ranks <= A only)."""
    rs = sorted(r for r in ranks if r <= MAX_SEQ_RANK)
    out = []
    for i in range(len(rs) - length + 1):
        w = rs[i:i + length]
        if _is_consecutive(w):
            out.append(w)
    return out


def _take(groups, rank, n):
    return groups[rank][:n]


def _smallest_kicker_cards(groups, exclude, n, size):
    """n kickers of `size` cards each (size=1 singles, size=2 pairs), from the
    smallest ranks not in `exclude`. Returns a card list or None."""
    kickers = []
    for r in sorted(groups):
        if r in exclude or len(groups[r]) < size:
            continue
        kickers.extend(groups[r][:size])
        if len(kickers) >= n * size:
            return kickers[:n * size]
    return None


def all_lead_plays(hand):
    """Every reasonable play a hand can LEAD with (bounded kicker enumeration —
    kickers always come from the smallest available ranks)."""
    groups = _groups(hand)
    plays = []

    for r in groups:
        plays.append(groups[r][:1])                      # single
    for r in groups:
        if len(groups[r]) >= 2:
            plays.append(groups[r][:2])                  # pair
    triples = [r for r in groups if len(groups[r]) >= 3]
    for r in triples:
        plays.append(groups[r][:3])                      # triple
        k1 = _smallest_kicker_cards(groups, {r}, 1, 1)
        if k1:
            plays.append(groups[r][:3] + k1)             # 三带一
        k2 = _smallest_kicker_cards(groups, {r}, 1, 2)
        if k2:
            plays.append(groups[r][:3] + k2)             # 三带二

    seq_ranks = [r for r in groups if r <= MAX_SEQ_RANK]
    for L in range(5, 13):
        for w in _seq_windows(seq_ranks, L):
            plays.append([groups[r][0] for r in w])      # straight
    for L in range(3, 9):
        for w in _seq_windows([r for r in seq_ranks if len(groups[r]) >= 2], L):
            plays.append([c for r in w for c in groups[r][:2]])  # 连对
    for k in range(2, 7):
        for w in _seq_windows(triples, k):
            core = [c for r in w for c in groups[r][:3]]
            plays.append(core)                           # 飞机
            k1 = _smallest_kicker_cards(groups, set(w), k, 1)
            if k1:
                plays.append(core + k1)                  # 飞机带单
            k2 = _smallest_kicker_cards(groups, set(w), k, 2)
            if k2:
                plays.append(core + k2)                  # 飞机带对

    quads = [r for r in groups if len(groups[r]) == 4]
    for r in quads:
        plays.append(groups[r])                          # bomb
        k1 = _smallest_kicker_cards(groups, {r}, 2, 1)
        if k1:
            plays.append(groups[r] + k1)                 # 四带二
        k2 = _smallest_kicker_cards(groups, {r}, 2, 2)
        if k2 and len(k2) == 4:
            plays.append(groups[r] + k2)                 # 四带二对
    if set(groups) >= {16, 17}:
        plays.append(["js", "jb"])                       # rocket

    return _dedupe(plays)


def follow_plays(hand, last):
    """Plays in `hand` that beat `last` (a classify() dict). Bombs/rocket always
    allowed; caller adds the 'pass' option separately."""
    groups = _groups(hand)
    plays = []
    t, m, L = last["type"], last["main"], last["length"]

    def add(cards):
        p = classify(cards)
        if p and beats(p, last):
            plays.append(cards)

    if t == "single":
        for r in groups:
            if r > m:
                add(groups[r][:1])
    elif t == "pair":
        for r in groups:
            if r > m and len(groups[r]) >= 2:
                add(groups[r][:2])
    elif t in ("triple", "triple1", "triple2"):
        for r in groups:
            if r > m and len(groups[r]) >= 3:
                core = groups[r][:3]
                if t == "triple":
                    add(core)
                elif t == "triple1":
                    k = _smallest_kicker_cards(groups, {r}, 1, 1)
                    if k:
                        add(core + k)
                else:
                    k = _smallest_kicker_cards(groups, {r}, 1, 2)
                    if k:
                        add(core + k)
    elif t == "straight":
        for w in _seq_windows(list(groups), L):
            if w[-1] > m:
                add([groups[r][0] for r in w])
    elif t == "pair_seq":
        for w in _seq_windows([r for r in groups if len(groups[r]) >= 2], L):
            if w[-1] > m:
                add([c for r in w for c in groups[r][:2]])
    elif t in ("airplane", "airplane1", "airplane2"):
        triples = [r for r in groups if len(groups[r]) >= 3]
        for w in _seq_windows(triples, L):
            if w[-1] <= m:
                continue
            core = [c for r in w for c in groups[r][:3]]
            if t == "airplane":
                add(core)
            else:
                size = 1 if t == "airplane1" else 2
                k = _smallest_kicker_cards(groups, set(w), L, size)
                if k and len(k) == L * size:
                    add(core + k)
    elif t in ("four_two", "four_two_pairs"):
        for r in groups:
            if r > m and len(groups[r]) == 4:
                if t == "four_two":
                    k = _smallest_kicker_cards(groups, {r}, 2, 1)
                    if k:
                        add(groups[r] + k)
                else:
                    k = _smallest_kicker_cards(groups, {r}, 2, 2)
                    if k and len(k) == 4:
                        add(groups[r] + k)

    # bombs beat anything (except a bigger bomb / rocket)
    for r in groups:
        if len(groups[r]) == 4:
            add(groups[r])
    if set(groups) >= {16, 17}:
        add(["js", "jb"])
    return _dedupe(plays)


def _dedupe(plays):
    seen, out = set(), []
    for p in plays:
        key = tuple(sort_cards(p))
        if key not in seen:
            seen.add(key)
            out.append(sort_cards(p))
    return out


def play_label(cards, last=None):
    """Human-readable play description for UI/model: 单A / 对K / 顺子3-7 / 王炸…"""
    p = classify(cards)
    if p is None:
        return " ".join(card_label(c) for c in sort_cards(cards))
    t, m = p["type"], p["main"]
    base = TYPE_CN.get(t, t)
    if t in ("straight",):
        lo = min(rank_of(c) for c in cards)
        return f"{base} {RANK_LABEL[lo]}-{RANK_LABEL[m]}({len(cards)}张)"
    if t == "pair_seq":
        lo = min(rank_of(c) for c in cards)
        return f"{base} {RANK_LABEL[lo]}-{RANK_LABEL[m]}({p['length']}对)"
    if t.startswith("airplane"):
        lo = m - p["length"] + 1
        return f"{base} {RANK_LABEL[lo]}-{RANK_LABEL[m]}"
    if t in ("triple1", "triple2", "four_two", "four_two_pairs"):
        return f"{base} {RANK_LABEL[m]}"
    if t == "rocket":
        return "王炸!"
    return f"{base}{RANK_LABEL[m]}"

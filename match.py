"""Head-to-head match: Jev vs Laya, alternating the first move.

Both backends play through the exact same pipeline — same board state,
same questions, same move pruning, same tactical_override safety net —
so the only variable is which model picks the move. "player" moves first,
so each round swaps which backend occupies that side.

Usage:
    python3 match.py [games]      # default 4 games
"""

import json
import random
import sys
import time

from dotenv import load_dotenv

import game as G
from app import build_move_choice_criteria, prune_moves
from jev_client import JevClient
from laya_client import LayaClient

load_dotenv()

MAX_TURNS = 400  # total half-moves before calling it a draw


def all_moves(game, side):
    return [(fp, tp) for fp in G.movable_pieces(game, side)
            for tp in G.possible_moves(game, side, fp)]


def decide(game, side, client, backend):
    """One half-move decision via the given backend. Returns
    (from_pos, to_pos, info) or raises."""
    override = G.tactical_override(game, side)
    if override:
        return override[0], override[1], {"override": override[2]}

    moves = all_moves(game, side)
    if not moves:
        return None, None, {"error": "no-legal-moves"}
    moves = prune_moves(game, moves)

    state = G.build_jev_state(game, side)
    questions = G.build_jev_questions()
    questions["primary_move"]["criteria"] = build_move_choice_criteria(
        game, moves, owner=side)

    resp = client.system_one(state=state, questions=questions)
    pm = resp.get("answers", {}).get("primary_move", {})
    try:
        idx = int(str(pm.get("choice", "m0")).lstrip("m"))
    except ValueError:
        idx = 0
    idx = max(0, min(idx, len(moves) - 1))
    fp, tp = moves[idx]
    info = {
        "confidence": pm.get("confidence"),
        "purpose": resp.get("answers", {}).get("move_purpose", {}).get("choice"),
    }
    return fp, tp, info


def play_game(first_backend, clients, rng):
    """Play one game. `first_backend` occupies "player" (moves first).
    Returns a result dict."""
    side_map = {
        "player": first_backend,
        "ai": "laya" if first_backend == "jev" else "jev",
    }
    game = G.new_game_for_setup()

    # Both sides get a randomly perturbed preset so neither memorizes.
    pl = G.perturb_layout("player", G._preset_abs_layout(
        "player", rng.randrange(len(G.PRESET_FORMATIONS))))
    game["setup"]["player"] = {f"{r},{c}": t for (r, c), t in pl.items()}
    G.setup_start(game)

    side = "player"
    while not game["winner"] and game["turn"] < MAX_TURNS:
        backend = side_map[side]
        t0 = time.time()
        try:
            fp, tp, info = decide(game, side, clients[backend], backend)
        except Exception as e:
            return {"winner": "opponent-error", "loser_backend": backend,
                    "error": repr(e), "turns": game["turn"],
                    "first": first_backend}
        if fp is None:
            return {"winner": side_map["player" if side == "ai" else "player"],
                    "reason": "no-legal-moves", "turns": game["turn"],
                    "first": first_backend}
        res = G.execute_move(game, side, fp, tp)
        ev = res.get("event", {})
        tag = ""
        if ev.get("combat"):
            tag = f" ⚔{ev['outcome']}"
        if ev.get("flag_deduced"):
            tag += " ⚑deduced"
        if info.get("override"):
            tag += f" [override:{info['override']}]"
        print(f"  t{game['turn']:>3} {backend:>4}({side}): "
              f"{tuple(fp)}→{tuple(tp)}{tag} ({time.time()-t0:.1f}s)",
              flush=True)
        side = "ai" if side == "player" else "player"

    if game["winner"]:
        return {"winner": side_map[game["winner"]], "reason": "win",
                "turns": game["turn"], "first": first_backend}
    return {"winner": "draw", "reason": "turn-cap", "turns": game["turn"],
            "first": first_backend}


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    rng = random.Random()

    print("loading backends...", flush=True)
    clients = {}
    try:
        clients["jev"] = JevClient()
        print("  jev: ok", flush=True)
    except Exception as e:
        print(f"  jev: DISABLED ({e})", flush=True)
    try:
        clients["laya"] = LayaClient()
        print("  laya: ok", flush=True)
    except Exception as e:
        print(f"  laya: DISABLED ({e})", flush=True)
    if len(clients) < 2:
        print("need both backends to run a match", file=sys.stderr)
        sys.exit(1)

    results = []
    for r in range(n):
        first = "jev" if r % 2 == 0 else "laya"   # alternate the first move
        print(f"\n=== game {r+1}/{n} — {first} moves first ===", flush=True)
        t0 = time.time()
        res = play_game(first, clients, rng)
        res["game"] = r + 1
        res["elapsed_s"] = round(time.time() - t0, 1)
        results.append(res)
        print(f"  -> winner: {res['winner']} ({res['reason']}, "
              f"{res['turns']} half-moves, {res['elapsed_s']}s)", flush=True)

    jev_wins = sum(1 for x in results if x["winner"] == "jev")
    laya_wins = sum(1 for x in results if x["winner"] == "laya")
    draws = n - jev_wins - laya_wins
    print("\n=========== MATCH RESULT ===========")
    print(f"  Jev  : {jev_wins} win(s)")
    print(f"  Laya : {laya_wins} win(s)")
    print(f"  Draws: {draws}")
    for x in results:
        print(f"    game {x['game']}: first={x['first']:>4} "
              f"winner={x['winner']:<9} turns={x['turns']}")
    with open("match_results.json", "w") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print("  saved to match_results.json")


if __name__ == "__main__":
    main()

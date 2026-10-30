"""Self-play evolutionary tuning for the Chinese Checkers heuristic.

This is the "self-evolving agent" piece: it never touches source code, only
the numeric coefficients in weights.py's WEIGHTS dict (see that file for why
the search space is deliberately restricted to smooth magnitudes, not
geometric thresholds). Everything here runs on the pure local heuristic —
no Jev/Laya API calls — so a full evolutionary run costs nothing but CPU and
can run unattended on a schedule.

Pipeline per generation:
  1. Mutate the current champion into a handful of candidates (gaussian
     noise per-coefficient, clamped to weights.BOUNDS).
  2. Each candidate plays N self-play games against the champion (colors
     alternated so neither side gets a fixed first-move edge), with a small
     epsilon-greedy exploration chance so repeated matchups aren't bit-for-
     bit identical games.
  3. The best candidate is promoted to champion ONLY if it clears a win-rate
     bar with basic statistical confidence (not just "won more than lost"),
     and also still beats a FROZEN baseline (the original hand-tuned
     weights) above a floor — this is the regression gate: it stops the
     search from drifting into something that only looks good against
     recent ancestors but has quietly gotten worse overall.
  4. Every generation (promoted or not) is logged to evolution_history/ for
     a full audit trail, and a run-level report.json/.md summarizes what
     happened — meant to be read unattended ("periodic report" mode) rather
     than requiring a human to approve each step.

Usage:
    python3 self_evolve.py --generations 20 --candidates 6 --games 24
"""

import argparse
import copy
import json
import math
import os
import random
import time

import ai_engine as ae
import rules
import weights as wmod

HISTORY_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "evolution_history")
BASELINE_WEIGHTS = dict(wmod.DEFAULT_WEIGHTS)   # frozen regression reference

MAX_MOVES_PER_GAME = 300   # draw if nobody finishes — avoids runaway loops
EPSILON = 0.08             # exploration: chance to play 2nd-best move instead
PROMOTE_WIN_RATE = 0.56    # candidate must beat champion at least this often
PROMOTE_MIN_GAMES_FOR_CONFIDENCE = 20
REGRESSION_FLOOR = 0.50    # champion must still beat the frozen baseline this often
REGRESSION_CHECK_EVERY = 5  # generations between regression audits


def _top_k_moves(game, player, w, k=3):
    """Like move_fallback but returns the top-k so epsilon-exploration has
    real alternatives instead of random legal noise (which would mostly
    just be bad moves and wouldn't test the heuristic's actual judgment)."""
    legal = game.legal_moves(player)
    if not legal:
        return []
    zone = rules.START_ZONE[player]
    scored = sorted(legal, key=lambda p: ae.two_ply_progress(game, player, p, w)[0]
                    + (w["fallback_leave_zone_bonus"] if p[0] in zone else 0)
                    + ae.opening_score(game, player, p, w)
                    + ae.formation_score(game, player, p, w), reverse=True)
    return scored[:k]


def play_game(weights_red, weights_blue, rng, epsilon=EPSILON):
    """One game, red using weights_red, blue using weights_blue. Returns
    'red' / 'blue' / None (draw by move cap)."""
    game = rules.Game()
    w_by_player = {"red": weights_red, "blue": weights_blue}
    for _ in range(MAX_MOVES_PER_GAME):
        player = rules.PLAYERS[game.turn]
        w = w_by_player[player]
        options = _top_k_moves(game, player, w, k=3)
        if not options:
            break
        move = options[0] if (len(options) == 1 or rng.random() > epsilon) \
            else options[rng.randrange(1, len(options))]
        game.apply_move(player, move)
        if game.winner:
            return game.winner
    return None  # draw / move cap


def evaluate(candidate_w, champion_w, n_games, rng):
    """Win rate of candidate vs champion over n_games, alternating colors."""
    wins = losses = draws = 0
    for i in range(n_games):
        if i % 2 == 0:
            result = play_game(candidate_w, champion_w, rng)
            cand_won = result == "red"
        else:
            result = play_game(champion_w, candidate_w, rng)
            cand_won = result == "blue"
        if result is None:
            draws += 1
        elif cand_won:
            wins += 1
        else:
            losses += 1
    decisive = wins + losses
    win_rate = wins / decisive if decisive else 0.5
    return {"wins": wins, "losses": losses, "draws": draws,
            "win_rate": round(win_rate, 3), "n_games": n_games}


def mutate(base_w, rng, scale=0.15):
    out = dict(base_w)
    for k, v in base_w.items():
        lo, hi = wmod.BOUNDS[k]
        step = (hi - lo) * scale * rng.gauss(0, 1) * 0.3
        out[k] = max(lo, min(hi, v + step))
    return out


def _binomial_confident(wins, n, bar):
    """Very rough one-sided confidence check (normal approx to binomial)
    that the true win rate is above `bar` — enough to gate against noise
    from a handful of unlucky games, not a rigorous significance test."""
    if n == 0:
        return False
    p_hat = wins / n
    se = math.sqrt(bar * (1 - bar) / n)
    z = (p_hat - bar) / se if se > 0 else 0
    return z > 1.0   # ~84% one-sided confidence; deliberately not super strict


def run(generations, n_candidates, n_games, seed=None, log=print):
    rng = random.Random(seed)
    os.makedirs(HISTORY_DIR, exist_ok=True)
    champion = wmod.load()
    gen_start = 0
    try:
        with open(wmod.WEIGHTS_FILE, encoding="utf-8") as f:
            gen_start = json.load(f).get("meta", {}).get("generation", 0)
    except (OSError, json.JSONDecodeError):
        pass

    report = {"started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
              "generations": [], "promotions": 0, "rollbacks": 0}
    for gen in range(gen_start + 1, gen_start + generations + 1):
        t0 = time.time()
        candidates = [mutate(champion, rng) for _ in range(n_candidates)]
        results = []
        for i, cand in enumerate(candidates):
            r = evaluate(cand, champion, n_games, rng)
            results.append((cand, r))
            log(f"  gen{gen} candidate{i}: {r['wins']}W-{r['losses']}L-{r['draws']}D "
                f"win_rate={r['win_rate']}")

        best_cand, best_r = max(results, key=lambda t: t[1]["win_rate"])
        promoted = False
        reason = "below promotion bar"
        if (best_r["win_rate"] >= PROMOTE_WIN_RATE
                and best_r["n_games"] >= PROMOTE_MIN_GAMES_FOR_CONFIDENCE
                and _binomial_confident(best_r["wins"], best_r["wins"] + best_r["losses"], 0.5)):
            promoted = True
            reason = "cleared win-rate + confidence bar"

        gen_record = {
            "generation": gen,
            "best_candidate_vs_champion": best_r,
            "promoted": promoted,
            "reason": reason,
            "elapsed_s": round(time.time() - t0, 1),
        }

        if promoted:
            prev_champion = champion
            champion = best_cand
            report["promotions"] += 1

            # Regression gate: periodically confirm the new lineage hasn't
            # quietly drifted worse against the ORIGINAL frozen baseline,
            # even though it keeps beating its immediate predecessor.
            if gen % REGRESSION_CHECK_EVERY == 0:
                base_r = evaluate(champion, BASELINE_WEIGHTS, 30, rng)
                gen_record["regression_check_vs_baseline"] = base_r
                if base_r["win_rate"] < REGRESSION_FLOOR:
                    champion = prev_champion
                    gen_record["rolled_back"] = True
                    report["rollbacks"] += 1
                    log(f"  gen{gen}: REGRESSION vs baseline "
                        f"(win_rate={base_r['win_rate']}) — rolled back")

        report["generations"].append(gen_record)
        with open(os.path.join(HISTORY_DIR, f"gen{gen:04d}.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"weights": champion, "record": gen_record}, f,
                      indent=2, ensure_ascii=False)

        if promoted and not gen_record.get("rolled_back"):
            wmod.save_champion(champion, meta={
                "generation": gen, "promoted_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                "win_rate_vs_prev": best_r["win_rate"]})
            log(f"gen{gen}: PROMOTED (win_rate={best_r['win_rate']} "
                f"over {best_r['n_games']} games)")
        else:
            log(f"gen{gen}: no promotion ({reason}, "
                f"best win_rate={best_r['win_rate']})")

    report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["final_champion_generation"] = gen_start + generations
    report_path = os.path.join(HISTORY_DIR, f"report_{int(time.time())}.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    _write_markdown_summary(report, report_path.replace(".json", ".md"))
    return report


def _write_markdown_summary(report, path):
    lines = [f"# 跳棋启发式自进化报告",
            f"开始: {report['started_at']} · 结束: {report['finished_at']}",
            f"晋升次数: {report['promotions']} · 回滚次数: {report['rollbacks']}",
            "", "| 代数 | 最佳候选胜率 | 晋升 | 备注 |", "|---|---|---|---|"]
    for g in report["generations"]:
        note = g["reason"]
        if g.get("rolled_back"):
            note += " ⚠已回滚"
        lines.append(f"| {g['generation']} | {g['best_candidate_vs_champion']['win_rate']} "
                    f"| {'✅' if g['promoted'] and not g.get('rolled_back') else ''} | {note} |")
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--generations", type=int, default=10)
    p.add_argument("--candidates", type=int, default=6)
    p.add_argument("--games", type=int, default=24)
    p.add_argument("--seed", type=int, default=None)
    args = p.parse_args()
    run(args.generations, args.candidates, args.games, seed=args.seed)

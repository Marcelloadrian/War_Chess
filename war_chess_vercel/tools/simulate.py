"""Headless balance simulator.   python tools/simulate.py [games_per_cell=6]
Part 1: difficulty check (balanced vs balanced).  Part 2: persona matrix (medium) to spot
dominant strategies: a persona that beats everything (or loses to everything) needs tuning."""
import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from collections import Counter
import ai, combat as C, mapgen
from engine import Difficulty, Side

N = int(sys.argv[1]) if len(sys.argv) > 1 else 6
MAX_ROUNDS = 100


def play(diff, seed, pa="balanced", pb="balanced"):
    s = mapgen.generate(32, 20, diff, seed); C.install(s)
    s.persona = {Side.PLAYER: pa, Side.AI: pb}
    while not s.winner and s.round < MAX_ROUNDS:
        ai.play_turn(s, s.active)
    ev = Counter(e["type"] for e in s.events)
    return s, ev


print("== Difficulty (balanced vs balanced) ==")
for diff in Difficulty:
    w, rounds = Counter(), []
    for seed in range(N * 3):
        s, _ = play(diff, seed); w[s.winner.value if s.winner else "draw"] += 1; rounds.append(s.round)
    n = N * 3
    print(f"{diff.value:7} player {w['player']/n:4.0%}  ai {w['ai']/n:4.0%}  draw {w['draw']/n:4.0%}  avg rounds {sum(rounds)/n:.0f}")

print("\n== Persona matrix (medium): row persona's win rate as PLAYER vs column as AI ==")
P = list(ai.PERSONAS); tot = Counter(); games = Counter(); stats = {p: Counter() for p in P}
print(" " * 10 + "".join(f"{c:>10}" for c in P))
for r in P:
    row = []
    for c in P:
        wins = 0
        for seed in range(N):
            s, ev = play(Difficulty.MEDIUM, seed, r, c)
            wins += s.winner is Side.PLAYER
            tot[r] += s.winner is Side.PLAYER; games[r] += 1
            tot[c] += s.winner is Side.AI; games[c] += 1
            for k in ("gacha_roll", "interrogated", "mine_detonated", "unit_frozen", "promoted", "unit_loaded", "mine_planted", "force_clash"):
                stats[r][k] += ev[k]
        row.append(f"{wins/N:>10.0%}")
    print(f"{r:>10}" + "".join(row))
print("\nOverall win rate:", {p: f"{tot[p]/games[p]:.0%}" for p in P})
print("Events across all games:", {p: dict(stats[p]) for p in P})

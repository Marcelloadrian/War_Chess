"""scenarios.py - preset missions (also work as the tutorial) + random skirmish."""
from __future__ import annotations

import mapgen
from engine import Difficulty, GameState, Res, Side

PERSONA_CYCLE = ["balanced", "banker", "brute"]

SCENARIOS: dict[str, dict] = {
    "skirmish": {"name": "Skirmish", "blurb": "Random map. Capture or destroy the enemy HQ."},
    "supply_run": {
        "name": "1. Supply Run", "difficulty": "easy", "seed": 11,
        "blurb": "Tutorial: hold 2 supply bases by round 14. Walk infantry onto a base, press Capture node.",
        "objective": {"type": "capture_bases", "count": 2, "rounds": 14}},
    "hold_line": {
        "name": "2. Hold the Line", "difficulty": "hard", "seed": 23, "persona": "brute",
        "blurb": "A brutal enemy commander. Survive until round 15. Mines and walls help.",
        "objective": {"type": "survive", "rounds": 15}},
    "lean_war": {
        "name": "3. Lean War", "difficulty": "medium", "seed": 5, "persona": "banker",
        "blurb": "You start with half the food. Cut the enemy's supply roads before yours starve.",
        "player_mult": {"food": 0.5}},
}


def build(scenario_id: str, difficulty: str, seed: int, vets: tuple = ()) -> GameState:
    sc = SCENARIOS[scenario_id]                      # KeyError -> 400 in the server
    diff = Difficulty(sc.get("difficulty") or difficulty)
    seed = sc.get("seed", seed) % 1_000_000
    s = mapgen.generate(50, 50, diff, seed)
    s.scenario, s.objective = scenario_id, sc.get("objective")
    s.persona[Side.AI] = sc.get("persona", PERSONA_CYCLE[seed % 3])
    for res, mult in sc.get("player_mult", {}).items():
        s.factions[Side.PLAYER].res[Res(res)] *= mult
        s.start_res[Side.PLAYER][Res(res)] *= mult
    # Campaign carry-over: surviving veterans' ranks from the previous mission.
    fighters = [u for u in s.units_of(Side.PLAYER) if u.type.value in ("infantry", "engineer")]
    for u, r in zip(fighters, vets):
        u.rank = max(1, min(5, int(r)))
    return s

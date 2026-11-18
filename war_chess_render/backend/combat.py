"""
combat.py - Force clashes, vulnerability auto-win, gacha subversion,
capture / surround, interrogation and Planted Bomb (mine) resolution.

Depends only on engine.py. All randomness uses state.rng.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import wraps
from enum import Enum
from typing import Optional

from engine import (
    TERRAIN, Building, BuildingType, Coord, Difficulty, GameState, Mine, Res, Side,
    Terrain, Unit, UnitClass, UnitType, INTERROGATION_COST, UNIT_SPECS,
)


@dataclass
class Outcome:
    ok: bool
    kind: str
    detail: dict = field(default_factory=dict)


def _cheb(a: Coord, b: Coord) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def actioned(fn):
    """Every battlefield action costs 1 of the side's MAX_ACTIONS per turn (player and AI alike)."""
    @wraps(fn)
    def wrapper(state, *args, **kwargs):
        side = state.active
        if state.actions_left(side) <= 0:
            return Outcome(False, "no_actions_left")
        out = fn(state, *args, **kwargs)
        if out.ok:
            state.use_action(side)
        return out
    return wrapper


# =========================================================================== #
# 1. FORCE NUMBER CLASH
# =========================================================================== #
def force_number(state: GameState, u: Unit, defending: bool) -> float:
    """force = base * rankMult * hpFactor * moralFactor * (1 + terrainDefense)"""
    base = u.spec.force
    rank_mult = 1.0 + 0.15 * (u.rank - 1)
    hp_factor = 0.5 + 0.5 * (u.hp / u.spec.hp)
    moral = state.factions[u.owner].moral
    moral_factor = 0.85 + 0.30 * (moral / 100.0)           # 0.85 .. 1.15
    terrain = 1.0 + (TERRAIN[state.terrain_at(u.pos)].defense if defending else 0.0)
    return base * rank_mult * hp_factor * moral_factor * terrain


def _validate_attack(state: GameState, a: Unit, d: Unit) -> Optional[str]:
    if state.winner:
        return "game_over"
    if a.owner is not state.active or a.owner is d.owner or d.carrier is not None:
        return "illegal_target"
    if not state.can_see(a.owner, d.pos):
        return "not_visible"              # fog of war: cannot engage what you cannot see
    if a.frozen:
        return "attacker_frozen"          # frozen units cannot initiate
    if _cheb(a.pos, d.pos) > a.spec.attack_range:
        return "out_of_range"
    return None


@actioned
def resolve_force_attack(state: GameState, attacker_id: int, defender_id: int,
                         capture_if_vulnerable: bool = False) -> Outcome:
    a, d = state.units[attacker_id], state.units[defender_id]
    err = _validate_attack(state, a, d)
    if err:
        return Outcome(False, err)

    # ---- Vulnerability Rule: bypasses force, stats, ranks entirely --------- #
    if state.is_vulnerable_to(d, a.owner):
        if capture_if_vulnerable and d.type is UnitType.INFANTRY:
            _take_prisoner(state, a.owner, d)
            state.emit("auto_win", attacker=a.id, defender=d.id, result="captured")
            return Outcome(True, "auto_win_captured", {"defender": d.id})
        state.kill_unit(d.id, "vulnerable_bankrupt")
        state.emit("auto_win", attacker=a.id, defender=d.id, result="destroyed")
        state.evaluate_bankruptcy(a.owner)
        return Outcome(True, "auto_win_destroyed", {"defender": d.id})

    # ---- Normal clash ------------------------------------------------------ #
    cost = {Res.BULLETS: a.spec.bullet_cost}
    if not state.spend(a.owner, cost):
        return Outcome(False, "no_bullets")
    fa = force_number(state, a, False) * state.rng.uniform(0.85, 1.15)
    fd = force_number(state, d, True) * state.rng.uniform(0.85, 1.15)
    ratio = fa / max(fd, 1e-6)

    if ratio >= 1.0:
        winner, loser, wdmg = a, d, int(35 / ratio)
        outcome = "attacker_wins"
    elif ratio >= 0.7:
        winner, loser, wdmg = None, None, 0
        outcome = "repelled"
        a.hp -= int(25 * (1 / ratio) * 0.5)
        d.hp -= 20
    else:
        winner, loser, wdmg = d, a, int(30 * (fd / fa) * 0.5)
        outcome = "attacker_loses"

    if winner:
        winner.hp -= wdmg
        state.kill_unit(loser.id, "force_clash")
        winner.kills += 1                                  # veterancy
        if winner.kills % 2 == 0 and winner.rank < 5:
            winner.rank += 1
            state.emit("promoted", unit=winner.id, rank=winner.rank)
    for u in (a, d):
        if u.id in state.units and u.hp <= 0:
            state.kill_unit(u.id, "attrition")
    state.emit("force_clash", attacker=a.id, defender=d.id, fa=round(fa, 1),
               fd=round(fd, 1), outcome=outcome)
    state.evaluate_bankruptcy(a.owner)
    return Outcome(True, outcome, {"fa": fa, "fd": fd})


@actioned
def drop_bomb(state: GameState, side: Side, target: Coord) -> Outcome:
    """Normal bomb: active-turn AoE (radius 1). Destroys walls; terrain cover
    (forest/urban) reduces unit damage. Hits friendlies too if they are there."""
    if side is not state.active or not state.spend(side, {Res.BOMBS: 1}):
        return Outcome(False, "cannot_bomb")
    hit = []
    for c in [target, *state.neighbors8(target)]:
        if not state.in_bounds(c):
            continue
        b = state.buildings.get(c)
        if b and b.type is BuildingType.WALL:
            del state.buildings[c]
            if state.terrain_at(c) is not Terrain.SEA:
                state.grid[c[1]][c[0]] = Terrain.URBAN          # rubble: cover, no tanks
            state.emit("wall_destroyed", pos=c)
        u = state.unit_at(c)
        if u:
            cover = TERRAIN[state.terrain_at(c)].ranged_cover
            dmg = int((70 if c == target else 35) * (1 - cover))
            u.hp -= dmg
            hit.append(u.id)
            if u.hp <= 0:
                state.kill_unit(u.id, "bomb")
    state.emit("bomb_dropped", pos=target, hit=hit)
    return Outcome(True, "bomb", {"hit": hit})


# =========================================================================== #
# 2. CAPTURE, SURROUND, PRISONERS
# =========================================================================== #
def _take_prisoner(state: GameState, captor: Side, u: Unit) -> None:
    state.units.pop(u.id, None)
    state.prisoners[u.id] = (u, captor)
    state.apply_moral(captor, "CAPTURE_ALIVE")
    state.emit("unit_captured", unit=u.id, captor=captor.value, pos=u.pos)


@actioned
def try_single_capture(state: GameState, attacker_id: int, target_id: int) -> Outcome:
    """Single-unit capture: needs a Force threshold (>=1.6x) and target wounded
    (<=60% hp) - capture is an alternative to killing, not a free action."""
    a, d = state.units[attacker_id], state.units[target_id]
    err = _validate_attack(state, a, d)
    if err:
        return Outcome(False, err)
    if state.is_vulnerable_to(d, a.owner):
        _take_prisoner(state, a.owner, d)
        return Outcome(True, "captured_vulnerable")
    ratio = force_number(state, a, False) / max(force_number(state, d, True), 1e-6)
    if ratio >= 1.6 and d.hp <= 0.6 * d.spec.hp and d.type is UnitType.INFANTRY:
        _take_prisoner(state, a.owner, d)
        return Outcome(True, "captured")
    return Outcome(False, "threshold_not_met", {"ratio": round(ratio, 2)})


def is_surrounded(state: GameState, u: Unit) -> bool:
    """All passable neighbours are enemy-occupied/walled; >=2 enemies adjacent."""
    enemies = 0
    for n in state.neighbors8(u.pos):
        if not state.in_bounds(n):
            continue                                  # map edge counts as closed
        occ = state.unit_at(n)
        b = state.buildings.get(n)
        if occ and occ.owner is not u.owner:
            enemies += 1
        elif (b and b.type is BuildingType.WALL) or u.spec.cls not in TERRAIN[state.terrain_at(n)].allowed:
            continue
        else:
            return False                              # an open escape tile exists
    return enemies >= 2


@actioned
def capture_surrounded(state: GameState, side: Side) -> Outcome:
    """Capture ALL enemy infantry currently surrounded. Other classes are
    destroyed (cannot be held as prisoners)."""
    caught = [u for u in state.units_of(side.other) if is_surrounded(state, u)]
    for u in caught:
        if u.type is UnitType.INFANTRY:
            _take_prisoner(state, side, u)
        else:
            state.kill_unit(u.id, "encircled")
    return Outcome(bool(caught), "surround", {"count": len(caught)})


# =========================================================================== #
# 3. INTERROGATION
# =========================================================================== #
def force_interrogate(state: GameState, side: Side, prisoner_id: int) -> Outcome:
    """Lethal, 0 money, big moral hit. Reveals ALL enemy mines to the captor."""
    entry = state.prisoners.get(prisoner_id)
    if not entry or entry[1] is not side:
        return Outcome(False, "no_such_prisoner")
    del state.prisoners[prisoner_id]
    revealed = _reveal_mines(state, side, fraction=1.0)
    state.apply_moral(side, "FORCE_INTERROGATION")
    state.emit("interrogated", mode="force", prisoner=prisoner_id, revealed=revealed)
    return Outcome(True, "force", {"revealed": revealed, "prisoner_died": True})


def resource_interrogate(state: GameState, side: Side, prisoner_id: int) -> Outcome:
    """Non-lethal; costs money+food scaled by difficulty; reveals ~50% of mines."""
    entry = state.prisoners.get(prisoner_id)
    if not entry or entry[1] is not side:
        return Outcome(False, "no_such_prisoner")
    c = INTERROGATION_COST[state.difficulty]
    if not state.spend(side, {Res.MONEY: c, Res.FOOD: c / 2}):
        return Outcome(False, "cannot_afford")
    revealed = _reveal_mines(state, side, fraction=0.5)
    state.apply_moral(side, "RESOURCE_INTERROGATION")
    state.emit("interrogated", mode="resource", prisoner=prisoner_id, revealed=revealed)
    return Outcome(True, "resource", {"revealed": revealed, "cost": c})


def release_prisoner(state: GameState, side: Side, prisoner_id: int) -> Outcome:
    entry = state.prisoners.pop(prisoner_id, None)
    if not entry or entry[1] is not side:
        return Outcome(False, "no_such_prisoner")
    state.apply_moral(side, "PRISONER_RELEASED")
    return Outcome(True, "released")


def _reveal_mines(state: GameState, side: Side, fraction: float) -> list[Coord]:
    pool = [m for m in state.mines.values() if m.owner is not side]
    state.rng.shuffle(pool)
    out = []
    for m in pool[: max(0, round(len(pool) * fraction))]:
        m.revealed_to.add(side)
        out.append(m.pos)
    return out


# =========================================================================== #
# 4. ECONOMIC SUBVERSION - GACHA
# =========================================================================== #
class Tier(str, Enum):
    DEFECTION = "SSR_DEFECTION"
    SURRENDER = "SR_SURRENDER"
    RESISTED = "R_RESISTED"
    BACKFIRE = "SSR_BACKFIRE"        # double agent


BASE_WEIGHTS = {Tier.DEFECTION: 8.0, Tier.SURRENDER: 24.0,
                Tier.RESISTED: 60.0, Tier.BACKFIRE: 8.0}
BRIBE_BASE_COST = 20      # per rank, times offer multiplier
DOUBLE_AGENT_ROUNDS = 3


def bribe_cost(target: Unit, offer_mult: float) -> float:
    return BRIBE_BASE_COST * target.rank * offer_mult


def gacha_odds(state: GameState, buyer: Side, target: Unit, offer_mult: float) -> dict:
    """Probability matrix. offer_mult in [1, 3].
        Success tiers x (1 + 0.5*(offer-1)) ; backfire x max(.3, 1-.25*(offer-1))
        target frozen/bankrupt : surrender x2.5, resisted x0.5
        morale edge            : edge = buyer moral - target side's moral (-100..+100)
                                 success x clamp(1 + edge/100, 0.4, 1.8); backfire x clamp(1 - edge/100, 0.4, 1.6)
                                 equal morale is neutral (x1) whatever the absolute level
        each rank above 1      : success x0.9
    """
    w = dict(BASE_WEIGHTS)
    boost = 1 + 0.5 * (offer_mult - 1)
    w[Tier.DEFECTION] *= boost
    w[Tier.SURRENDER] *= boost
    w[Tier.BACKFIRE] *= max(0.3, 1 - 0.25 * (offer_mult - 1))
    if target.frozen:
        w[Tier.SURRENDER] *= 2.5
        w[Tier.RESISTED] *= 0.5
    edge = state.factions[buyer].moral - state.factions[target.owner].moral    # who has the higher morale?
    desp = max(0.4, min(1.8, 1 + edge / 100))
    w[Tier.BACKFIRE] *= max(0.4, min(1.6, 1 - edge / 100))
    rank_pen = 0.9 ** (target.rank - 1)
    for t in (Tier.DEFECTION, Tier.SURRENDER):
        w[t] *= desp * rank_pen
    total = sum(w.values())
    return {t: v / total for t, v in w.items()}


@actioned
def gacha_bribe(state: GameState, buyer: Side, target_id: int,
                offer_mult: float = 1.0, roll: Optional[float] = None) -> Outcome:
    """`roll` lets tests inject a deterministic number in [0,1)."""
    t = state.units.get(target_id)
    if (not t or t.owner is buyer or buyer is not state.active or t.carrier is not None
            or not state.can_see(buyer, t.pos)):
        return Outcome(False, "illegal_target")
    offer_mult = max(1.0, min(3.0, offer_mult))
    cost = bribe_cost(t, offer_mult)
    if not state.spend(buyer, {Res.MONEY: cost}):
        return Outcome(False, "cannot_afford")

    odds = gacha_odds(state, buyer, t, offer_mult)
    r = state.rng.random() if roll is None else roll
    acc, tier = 0.0, Tier.RESISTED
    for tr in (Tier.DEFECTION, Tier.SURRENDER, Tier.BACKFIRE, Tier.RESISTED):
        acc += odds[tr]
        if r < acc:
            tier = tr
            break

    if tier is Tier.DEFECTION:
        _flip(state, t, buyer)
        state.apply_moral(buyer, "SUBVERSION")
    elif tier is Tier.SURRENDER:
        if t.type is UnitType.INFANTRY:
            _take_prisoner(state, buyer, t)         # +CAPTURE_ALIVE
        else:
            _flip(state, t, buyer)
        state.apply_moral(buyer, "SUBVERSION")
    elif tier is Tier.BACKFIRE:
        # Looks like a defection; actually a double agent that leaks our mines
        # and flips back after DOUBLE_AGENT_ROUNDS rounds.
        _flip(state, t, buyer, double_agent=True)
        for m in state.mines.values():
            if m.owner is buyer:
                m.revealed_to.add(buyer.other)
        state.apply_moral(buyer, "BACKFIRE")
    state.emit("gacha_roll", buyer=buyer.value, target=t.id, tier=tier.value,
               cost=cost, odds={k.value: round(v, 3) for k, v in odds.items()})
    return Outcome(True, tier.value, {"cost": cost, "odds": odds})


def _flip(state: GameState, u: Unit, new_owner: Side, double_agent: bool = False) -> None:
    u.original_owner = u.owner
    u.owner = new_owner
    u.frozen = False
    u.hp = max(u.hp, u.spec.hp // 2)
    if double_agent:
        u.double_agent_until = state.round + DOUBLE_AGENT_ROUNDS
    state.evaluate_bankruptcy(new_owner)


def tick_double_agents(state: GameState) -> None:
    """Round hook: double agents return to their original side."""
    for u in state.units.values():
        if u.double_agent_until is not None and state.round >= u.double_agent_until:
            state.emit("double_agent_revealed", unit=u.id, pos=u.pos)
            u.owner, u.original_owner = u.original_owner, None
            u.double_agent_until = None


def install(state: GameState) -> None:
    if tick_double_agents not in state.round_hooks:
        state.round_hooks.append(tick_double_agents)


# =========================================================================== #
# 5. PLANTED BOMBS (remote mines) - INDISCRIMINATE FRIENDLY FIRE
# =========================================================================== #
@actioned
def plant_mine(state: GameState, unit_id: int) -> Outcome:
    """Unit spends its turn planting a hidden bomb on its own tile."""
    u = state.units[unit_id]
    if u.owner is not state.active or u.frozen or u.spec.cls is UnitClass.NAVAL:
        return Outcome(False, "cannot_plant")
    if u.pos in state.mines or state.terrain_at(u.pos) is Terrain.SEA:
        return Outcome(False, "tile_unsuitable")
    if not state.spend(u.owner, {Res.BOMBS: 1, Res.MATERIALS: 2}):
        return Outcome(False, "cannot_afford")
    state.mines[u.pos] = Mine(u.owner, u.pos, state.round)
    state.emit("mine_planted", pos=u.pos, owner=u.owner.value)   # FE hides if not own
    return Outcome(True, "planted", {"pos": u.pos})


@actioned
def detonate_mine(state: GameState, side: Side, pos: Coord) -> Outcome:
    """Owner-triggered. Kills EVERY unit on the tile - friend or foe.
    Friendly deaths cost extra moral on top of the normal unit-loss penalty."""
    m = state.mines.get(pos)
    if not m or m.owner is not side or side is not state.active:
        return Outcome(False, "no_such_mine")
    del state.mines[pos]
    victims = [u for u in list(state.units.values()) if u.pos == pos]
    friendly = enemy = 0
    for u in victims:
        if u.owner is side:
            friendly += 1
        else:
            enemy += 1
        state.kill_unit(u.id, "planted_bomb")          # normal UNIT_LOST for owner
    if friendly:
        state.apply_moral(side, "FRIENDLY_MINE_KILL", friendly)
    state.emit("mine_detonated", pos=pos, owner=side.value,
               friendly_killed=friendly, enemy_killed=enemy)
    return Outcome(True, "detonated", {"friendly": friendly, "enemy": enemy})


# =========================================================================== #
# 6. ENGINEER: mine counter-play
# =========================================================================== #
@actioned
def defuse_mine(state: GameState, unit_id: int, pos: Coord) -> Outcome:
    """Engineers probe an adjacent tile and defuse whatever mine is there (any owner).
    Probing an empty tile is free, so engineers can sweep suspicious chokepoints."""
    u = state.units[unit_id]
    if u.type is not UnitType.ENGINEER or u.owner is not state.active or u.frozen:
        return Outcome(False, "cannot_defuse")
    if _cheb(u.pos, pos) > 1:
        return Outcome(False, "out_of_range")
    m = state.mines.pop(pos, None)
    if not m:
        return Outcome(False, "nothing_found")
    state.emit("mine_defused", pos=pos, owner=u.owner.value, was=m.owner.value)
    return Outcome(True, "defused")


# =========================================================================== #
# 7. ENGAGEMENT PREVIEW - deliberately FUZZY intel (no RNG consumed)
# =========================================================================== #
def preview_engagement(state: GameState, attacker_id: int, defender_id: int) -> dict:
    """Estimates only. Without a scout near the target the force ratio is a wide band whose
    centre is off by a deterministic amount; a scout within 5 tiles halves the uncertainty."""
    a, d = state.units[attacker_id], state.units[defender_id]
    err = _validate_attack(state, a, d)
    if err:
        return {"ok": False, "reason": err}
    r = force_number(state, a, False) / max(force_number(state, d, True), 1e-6)
    scout = any(u.owner is a.owner and u.type is UnitType.SCOUT and u.carrier is None
                and _cheb(u.pos, d.pos) <= 5 for u in state.units.values())
    spread = 0.10 if scout else 0.25
    shift = (((state.round * 31 + attacker_id * 17 + defender_id * 13) % 100) / 100 - 0.5) * spread
    lo, hi = r * (1 + shift - spread), r * (1 + shift + spread)
    bribe = []
    for off in (1.0, 2.0, 3.0):
        o = gacha_odds(state, a.owner, d, off)
        bb = o[Tier.BACKFIRE]
        bribe.append({"offer": off, "cost": round(bribe_cost(d, off)),
                      "success": int(round((o[Tier.DEFECTION] + o[Tier.SURRENDER]) * 20)) * 5,
                      "backfire": "low" if bb < 0.05 else "medium" if bb < 0.09 else "high"})
    return {"ok": True, "vulnerable": state.is_vulnerable_to(d, a.owner),
            "ratio": [round(lo, 2), round(hi, 2)],
            "verdict": "favourable" if lo > 1.3 else "dangerous" if hi < 0.8 else "uncertain",
            "intel": "good (scout nearby)" if scout else "poor", "bullet_cost": a.spec.bullet_cost,
            "can_capture": d.type is UnitType.INFANTRY or d.frozen, "bribe": bribe,
            "morale": {"you": round(state.factions[a.owner].moral), "enemy": round(state.factions[d.owner].moral)}}

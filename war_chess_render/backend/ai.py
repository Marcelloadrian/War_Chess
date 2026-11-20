"""ai.py - Rule-abiding, fog-limited AI that can play EITHER side (so it can be simulated).
Per turn: prisoners -> mines -> economy -> (flank) -> each unit: engineer / capture / attack / plant / advance.
The AI only reacts to enemies it can actually see."""
from __future__ import annotations

import heapq

import combat as C
from engine import TERRAIN, BuildingType, Coord, GameState, Res, Side, Terrain, Unit, UnitType

PERSONAS = {
    "balanced": {"bribe": 0.12, "money_gate": 150, "offer": 1.5, "interrogate": "resource"},
    "banker":   {"bribe": 0.35, "money_gate": 90,  "offer": 2.0, "interrogate": "resource"},
    "brute":    {"bribe": 0.0,  "money_gate": 1e9, "offer": 1.0, "interrogate": "force"},
    "flanker":  {"bribe": 0.12, "money_gate": 150, "offer": 1.5, "interrogate": "resource", "flank": True},
}


MAX_MINES = 6


def find_path(state: GameState, u: Unit, goal: Coord) -> list[Coord]:
    """Dijkstra weighted by move cost. Goal may be occupied (path ends adjacent)."""
    dist, prev, pq = {u.pos: 0}, {}, [(0, u.pos)]
    while pq:
        d, cur = heapq.heappop(pq)
        if cur == goal:
            break
        if d > dist.get(cur, 1e9):
            continue
        for n in state.neighbors8(cur):
            if not state.in_bounds(n) or (n != goal and not state.can_enter(u, n)):
                continue
            if n == goal and not state.can_enter(u, n) and not state.unit_at(n):
                continue
            nd = d + TERRAIN[state.terrain_at(n)].move_cost
            if nd < dist.get(n, 1e9):
                dist[n], prev[n] = nd, cur
                heapq.heappush(pq, (nd, n))
    if goal not in prev:
        return []
    path, cur = [], goal
    while cur != u.pos:
        path.append(cur)
        cur = prev[cur]
    path.reverse()
    return path[:-1] if state.unit_at(goal) else path


def _dist(a: Coord, b: Coord) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def _visible_foes(state: GameState, me: Side) -> list[Unit]:
    return [t for t in state.units_of(me.other) if t.carrier is None and state.can_see(me, t.pos)]


def _prisoners(state: GameState, me: Side, P: dict) -> None:
    for pid, (_, captor) in list(state.prisoners.items()):
        if captor is not me:
            continue
        if P["interrogate"] == "force":
            C.force_interrogate(state, me, pid)
        else:
            C.resource_interrogate(state, me, pid)
            C.release_prisoner(state, me, pid)


def _mines(state: GameState, me: Side, foe: Side) -> None:
    """Spring a mine when only enemies stand on it, or sacrifice a doomed frozen unit."""
    for m in list(state.mines.values()):
        if m.owner is not me:
            continue
        on = [u for u in state.units.values() if u.pos == m.pos]
        mine_u = [u for u in on if u.owner is me]
        foes_on = [u for u in on if u.owner is foe]
        adj_foes = sum(1 for n in state.neighbors8(m.pos) if (x := state.unit_at(n)) and x.owner is foe)
        if (foes_on and not mine_u) or (mine_u and all(u.frozen for u in mine_u) and (foes_on or adj_foes >= 2)):
            C.detonate_mine(state, me, m.pos)


def _economy(state: GameState, me: Side, foe: Side) -> None:
    res = state.factions[me].res
    if res[Res.MATERIALS] < 70 or res[Res.MONEY] < 60:
        return
    if state.count_buildings(me, BuildingType.SUPPLY_BASE) > state.count_buildings(foe, BuildingType.SUPPLY_BASE):
        return
    for u in state.units_of(me):
        if u.frozen or u.carrier is not None or u.type is not UnitType.INFANTRY:
            continue
        for n in state.neighbors8(u.pos):
            if state.in_bounds(n) and state.terrain_at(n) is Terrain.PLAINS and state.build(u.id, BuildingType.SUPPLY_BASE, n):
                return


def _flee(state: GameState, u: Unit, foes: list) -> None:
    """The Commandant creeps one step away from the nearest visible enemy."""
    best = max((n for n in state.neighbors8(u.pos) if state.in_bounds(n) and state.can_enter(u, n)),
               key=lambda n: min((_dist(n, f.pos) for f in foes), default=0), default=None)
    if best and foes:
        state.move_unit(u.id, [best])


def _info_base(state: GameState, me: Side) -> None:
    if state.round < 6 or any(b.type is BuildingType.INFO_BASE and b.owner is me for b in state.buildings.values()):
        return
    hq, res = state.command_pos(me), state.factions[me].res
    if not hq or res[Res.MATERIALS] < 110 or res[Res.MONEY] < 90:
        return
    for u in state.units_of(me):
        if u.frozen or u.carrier is not None or _dist(u.pos, hq) > 6:
            continue
        for n in state.neighbors8(u.pos):
            if state.in_bounds(n) and state.terrain_at(n) is Terrain.PLAINS and state.build(u.id, BuildingType.INFO_BASE, n):
                return


def _heavy(state: GameState, me: Side, foe: Side) -> None:
    """Symmetric Heavy Artillery: sabotage an enemy one, build one if affordable, shell the densest visible cluster."""
    foes = _visible_foes(state, me)
    eha = next((f for f in foes if f.type is UnitType.HEAVY_ARTILLERY), None)
    if eha:
        for e in state.units_of(me):
            if e.type is UnitType.ENGINEER and _dist(e.pos, eha.pos) <= 1 and state.sabotage(e.id, eha.id):
                return
    ha = next((u for u in state.units_of(me) if u.type is UnitType.HEAVY_ARTILLERY), None)
    if ha is None:
        if state.round >= 8 and not any(u.type is UnitType.HEAVY_ARTILLERY for u in state.units.values()):
            for u in state.units_of(me):
                if u.type is UnitType.ENGINEER and not u.frozen and u.carrier is None:
                    for n in state.neighbors8(u.pos):
                        if state.in_bounds(n) and state.build_artillery(u.id, n):
                            return
        return
    mine = {u.pos for u in state.units_of(me)}
    best = max(((sum(1 for f in foes if 0 <= f.pos[0] - x <= 1 and 0 <= f.pos[1] - y <= 1), (x, y))
                for f in foes for x in (f.pos[0] - 1, f.pos[0]) for y in (f.pos[1] - 1, f.pos[1])
                if not any((x + i, y + j) in mine for i in (0, 1) for j in (0, 1))), default=(0, None))
    if best[0] >= 1 and state.actions_left(me) > 0:
        state.fire_artillery(ha.id, best[1])


def _flank(state: GameState, me: Side, foe_hq: Coord, my_hq: Coord, used: set) -> None:
    """Naval pincer: load 2 infantry, sail toward the enemy HQ coast, land them."""
    t = next((u for u in state.units_of(me) if u.type is UnitType.TRANSPORT and not u.frozen and u.carrier is None), None)
    if not t:
        return
    used.add(t.id)
    cargo = [u for u in state.units.values() if u.carrier == t.id]
    if len(cargo) < 2:
        if _dist(t.pos, my_hq) > 12:          # already landed a wave far from home: do not shuttle
            return
        crew = sorted((u for u in state.units_of(me) if u.type is UnitType.INFANTRY and u.carrier is None and not u.frozen),
                      key=lambda u: _dist(u.pos, t.pos))
        for u in crew[: 2 - len(cargo)]:
            used.add(u.id)
            if _dist(u.pos, t.pos) > 1:
                p = find_path(state, u, t.pos)
                if p:
                    state.move_unit(u.id, p)
            if _dist(u.pos, t.pos) <= 1:
                state.load_unit(u.id, t.id)
        return
    if _dist(t.pos, foe_hq) <= 10:
        for k in cargo:
            for n in state.neighbors8(t.pos):
                if state.unload_unit(k.id, n):
                    break
        if any(u.carrier != t.id for u in cargo):
            return
    sea = [(x, y) for y in range(state.h) for x in range(state.w) if state.grid[y][x] is Terrain.SEA]
    target = min(sea, key=lambda c: _dist(c, foe_hq))
    p = find_path(state, t, target)
    if p:
        state.move_unit(t.id, p)


def play_turn(state: GameState, me: Side) -> None:
    foe = me.other
    res = state.factions[me].res
    P = PERSONAS[state.persona.get(me, "balanced")]
    _prisoners(state, me, P)
    _mines(state, me, foe)
    _economy(state, me, foe)
    _info_base(state, me)
    _heavy(state, me, foe)
    state.update_discovery(me)
    my_hq = state.command_pos(me)
    real_hq = next((b.pos for b in state.buildings.values() if b.type is BuildingType.HQ and b.owner is foe), None)
    # The enemy HQ is hidden: the AI only knows it once its units have SEEN it, otherwise it searches unexplored ground.
    foe_hq = real_hq if (real_hq and me.value in state.meta["found"]) else (state.search_target(me) or real_hq)
    used: set = set()
    mine_room = sum(1 for m in state.mines.values() if m.owner is me) < MAX_MINES   # mines are scarce
    if P.get("flank") and foe_hq and my_hq:
        _flank(state, me, foe_hq, my_hq, used)
    # the lane the enemy must take to reach our HQ: the best place for mines
    lane: set = set()
    if my_hq and foe_hq:
        lane = set(find_path(state, Unit(-1, me, UnitType.INFANTRY, foe_hq), my_hq))
    vf = _visible_foes(state, me)          # with only 5 actions, units that can fight act first
    order = sorted(state.units_of(me), key=lambda u: (not any(_dist(t.pos, u.pos) <= u.spec.attack_range for t in vf),
                                                      _dist(u.pos, foe_hq) if foe_hq else 0))
    for u in order:
        if state.actions_left(me) <= 0:
            break
        if state.winner or u.id not in state.units or u.frozen or u.carrier is not None or u.id in used:
            continue
        if u.type is UnitType.HEAVY_ARTILLERY:
            continue
        if u.type is UnitType.COMMANDANT:
            _flee(state, u, _visible_foes(state, me))
            continue
        foes = _visible_foes(state, me)
        if u.type is UnitType.ENGINEER:
            m = next((m for m in state.mines.values() if m.owner is foe and me in m.revealed_to
                      and _dist(m.pos, u.pos) <= 1), None)
            if m and C.defuse_mine(state, u.id, m.pos).ok:
                continue
        if (u.type is UnitType.ENGINEER and mine_room and my_hq and lane and res[Res.BOMBS] >= 4 and res[Res.MATERIALS] >= 8):
            spots = [c for c in lane if 5 <= _dist(c, my_hq) <= 11 and c not in state.mines]
            if spots:                                       # sapper: seed mines along the enemy's approach lane
                if u.pos in spots:
                    if C.plant_mine(state, u.id).ok:
                        continue
                else:
                    spot = min(spots, key=lambda c: _dist(c, u.pos))
                    p = find_path(state, u, spot)
                    if p:
                        state.move_unit(u.id, p)
                        if u.pos == spot:
                            C.plant_mine(state, u.id)
                        continue
        b = state.buildings.get(u.pos)
        if b and b.owner is not me and u.type is UnitType.INFANTRY:
            state.capture_building(u.id, u.pos)
            continue
        targets = [t for t in foes if _dist(t.pos, u.pos) <= u.spec.attack_range]
        if targets:
            targets.sort(key=lambda t: (not t.frozen, t.hp))       # free wins first
            t = targets[0]
            if not t.frozen and res[Res.MONEY] > P["money_gate"] and state.rng.random() < P["bribe"]:
                C.gacha_bribe(state, me, t.id, P["offer"])
            else:
                C.resolve_force_attack(state, u.id, t.id)
            continue
        if (mine_room and u.pos in lane and my_hq and 4 <= _dist(u.pos, my_hq) <= 14 and u.pos not in state.mines
                and res[Res.BOMBS] >= 5 and res[Res.MATERIALS] >= 10 and state.rng.random() < 0.5):
            if C.plant_mine(state, u.id).ok:
                continue
        starving = res[Res.FOOD] < 12
        goals = [bb.pos for bb in state.buildings.values() if bb.owner is not me and bb.type is not BuildingType.WALL]
        if starving:
            goals = [bb.pos for bb in state.buildings.values()
                     if bb.type is BuildingType.SUPPLY_BASE and bb.owner is not foe] or goals
        if foe_hq:
            goals.append(foe_hq)
        goals += [t.pos for t in foes]
        goals.sort(key=lambda g: _dist(g, u.pos))
        if foe_hq and (u.id % 2 == 0 or state.round >= 40) and not starving:
            goals.insert(0, foe_hq)          # commit to the HQ push instead of ping-ponging over bases
        for g in goals[:3]:
            p = find_path(state, u, g)
            if p:
                state.move_unit(u.id, p)
                break
    state.end_turn()


def play_ai_turn(state: GameState) -> None:
    play_turn(state, Side.AI)

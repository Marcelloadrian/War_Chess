"""
engine.py - Core game state, economy tick loop, terrain resource deduction,
bankruptcy freeze evaluation and the vulnerability (auto-win) predicate.

Design rules:
  * Pure-Python, deterministic: every random call goes through state.rng.
  * No I/O. Every state change appends an event to state.events so the frontend
    can animate it (see frontend/uiState.ts).
  * Grid is square, 8-directional (chess-king adjacency).
"""
from __future__ import annotations

import itertools
import random
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional

Coord = tuple[int, int]


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #
class Side(str, Enum):
    PLAYER = "player"
    AI = "ai"

    @property
    def other(self) -> "Side":
        return Side.AI if self is Side.PLAYER else Side.PLAYER


class Terrain(str, Enum):
    PLAINS = "plains"
    FOREST = "forest"
    URBAN = "urban"
    MUD = "mud"
    SEA = "sea"


class Res(str, Enum):
    MONEY = "money"
    FOOD = "food"
    BULLETS = "bullets"
    BOMBS = "bombs"
    MATERIALS = "materials"
    FUEL = "fuel"


class Difficulty(str, Enum):
    EASY = "easy"
    MEDIUM = "medium"
    HARD = "hard"


class UnitClass(str, Enum):
    FOOT = "foot"
    VEHICLE = "vehicle"
    NAVAL = "naval"
    AMPHIB = "amphib"


class Weather(str, Enum):
    CLEAR = "clear"
    RAIN = "rain"
    STORM = "storm"


class UnitType(str, Enum):
    ENGINEER = "engineer"
    SCOUT = "scout"
    INFANTRY = "infantry"
    ARMOR = "armor"
    DESTROYER = "destroyer"
    TRANSPORT = "transport"


class BuildingType(str, Enum):
    WALL = "wall"
    SUPPLY_BASE = "supply_base"
    BARRACKS = "barracks"
    HQ = "hq"


# --------------------------------------------------------------------------- #
# Static data tables (tune here, not in logic)
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TerrainSpec:
    move_cost: int
    defense: float                 # force multiplier bonus for the defender
    ranged_cover: float            # damage reduction vs bombs / ranged
    blocks_los: bool
    burn_res: Res
    burn_amt: float                # base resource burned per tile ENTERED
    allowed: frozenset[UnitClass]


LAND = frozenset({UnitClass.FOOT, UnitClass.VEHICLE, UnitClass.AMPHIB})
TERRAIN: dict[Terrain, TerrainSpec] = {
    Terrain.PLAINS: TerrainSpec(1, 0.00, 0.00, False, Res.FOOD, 0.5, LAND),
    Terrain.FOREST: TerrainSpec(2, 0.25, 0.50, True, Res.FOOD, 2.0,
                                frozenset({UnitClass.FOOT, UnitClass.VEHICLE})),
    Terrain.URBAN: TerrainSpec(2, 0.50, 0.35, True, Res.MATERIALS, 2.0,
                               frozenset({UnitClass.FOOT})),  # no heavy vehicles
    Terrain.MUD: TerrainSpec(3, 0.00, 0.00, False, Res.FUEL, 2.0, LAND),
    Terrain.SEA: TerrainSpec(1, 0.00, 0.00, False, Res.MONEY, 3.0,
                             frozenset({UnitClass.NAVAL, UnitClass.AMPHIB})),
}


# Weather multiplies the resource burn of terrain being entered (data-driven).
WEATHER_MULT: dict[tuple[Weather, Terrain], float] = {
    (Weather.RAIN, Terrain.PLAINS): 1.5, (Weather.RAIN, Terrain.MUD): 1.5, (Weather.RAIN, Terrain.FOREST): 1.25,
    (Weather.STORM, Terrain.SEA): 2.0, (Weather.STORM, Terrain.MUD): 1.5, (Weather.STORM, Terrain.PLAINS): 1.25,
}
SAVE_VERSION = 3
CARGO_CAP = 2
HEAL_PER_ROUND = 8
WEARINESS_START = 60          # after this round every burn/upkeep grows 3% per round
WEARINESS_RATE = 0.03
HQ_CONSCRIPT_BELOW = 4        # HQ drafts 1 infantry per payday while you field fewer than this
CONCEAL = {Terrain.FOREST, Terrain.URBAN}   # enemies there are only seen from <= 2 tiles


@dataclass(frozen=True)
class UnitSpec:
    cls: UnitClass
    force: float
    hp: int
    mp: int
    attack_range: int
    bullet_cost: int
    upkeep: dict                    # per-round upkeep {Res: amount}
    lifeline: tuple                 # resources whose depletion freezes the unit
    move_fuel: float                # extra FUEL per tile moved (vehicles/ships)
    vision: int = 3                 # fog-of-war sight radius (tiles)


UNIT_SPECS: dict[UnitType, UnitSpec] = {
    UnitType.INFANTRY: UnitSpec(UnitClass.FOOT, 10, 100, 3, 1, 2,
                                {Res.FOOD: 1.0}, (Res.FOOD,), 0.0),
    UnitType.ARMOR: UnitSpec(UnitClass.VEHICLE, 25, 160, 4, 2, 4,
                             {Res.FOOD: 1.0, Res.FUEL: 0.5}, (Res.FOOD, Res.FUEL), 0.5),
    UnitType.DESTROYER: UnitSpec(UnitClass.NAVAL, 30, 180, 4, 3, 4,
                                 {Res.FOOD: 1.0, Res.MONEY: 1.0}, (Res.MONEY, Res.FUEL), 1.0),
    UnitType.TRANSPORT: UnitSpec(UnitClass.AMPHIB, 4, 90, 4, 1, 1,
                                 {Res.FOOD: 0.5, Res.MONEY: 0.5}, (Res.MONEY,), 0.5),
    UnitType.ENGINEER: UnitSpec(UnitClass.FOOT, 6, 80, 3, 1, 1,
                                {Res.FOOD: 1.0}, (Res.FOOD,), 0.0),   # can defuse mines
    UnitType.SCOUT: UnitSpec(UnitClass.FOOT, 3, 60, 5, 1, 1,
                             {Res.FOOD: 1.0}, (Res.FOOD,), 0.0, vision=6),
}

BUILD_COST: dict[BuildingType, dict[Res, float]] = {
    BuildingType.WALL: {Res.MATERIALS: 10},
    BuildingType.SUPPLY_BASE: {Res.MATERIALS: 40, Res.MONEY: 30},
    BuildingType.BARRACKS: {Res.MATERIALS: 35, Res.MONEY: 20},
}
BUILD_TURNS = {BuildingType.WALL: 1, BuildingType.SUPPLY_BASE: 3, BuildingType.BARRACKS: 2}

BASE_START: dict[Res, float] = {
    Res.MONEY: 150, Res.FOOD: 60, Res.BULLETS: 60,
    Res.BOMBS: 15, Res.MATERIALS: 50, Res.FUEL: 40,
}
# (player multiplier, ai multiplier)
DIFFICULTY_START = {
    Difficulty.EASY: (1.40, 0.80),
    Difficulty.MEDIUM: (1.00, 1.00),
    Difficulty.HARD: (0.90, 1.40),
}
# Cost of non-lethal resource interrogation scales with difficulty.
INTERROGATION_COST = {Difficulty.EASY: 10, Difficulty.MEDIUM: 20, Difficulty.HARD: 35}

PAYDAY_INTERVAL = 5
PAYDAY_BASE: dict[Res, float] = {
    Res.MONEY: 40, Res.FOOD: 16, Res.BULLETS: 20,
    Res.BOMBS: 5, Res.MATERIALS: 20, Res.FUEL: 12,
}
SUPPLY_BASE_BONUS = 0.20          # +20% shipment per supply base (before moral cap)
ARMORY_BASE_CAP = 200             # shared cap on BULLETS + BOMBS
ARMORY_PER_BARRACKS = 40          # each barracks LOWERS the armory cap by this
ARMORY_MIN_CAP = 50
BARRACKS_RECRUIT_FOOD = 5

# --------------------------------------------------------------------------- #
# Moral scaling tables (piecewise-linear; moral is 0..100)
# --------------------------------------------------------------------------- #
INCOME_MULT_TABLE = [(0, 0.30), (25, 0.60), (50, 1.00), (75, 1.15), (100, 1.30)]
CONSUMPTION_MULT_TABLE = [(0, 1.50), (25, 1.25), (50, 1.00), (75, 0.95), (100, 0.90)]
# Max supply-base multiplier allowed at a given moral. Below ~30 moral, bases
# give NOTHING: this is the "Moral Multiplier Cap".
SUPPLY_CAP_TABLE = [(0, 1.00), (30, 1.00), (60, 1.60), (100, 2.50)]

MORAL_DELTAS = {
    "FORCE_INTERROGATION": -8.0,
    "RESOURCE_INTERROGATION": -0.5,
    "UNIT_LOST": -2.0,
    "FRIENDLY_MINE_KILL": -4.0,
    "BACKFIRE": -3.0,
    "CAPTURE_ALIVE": +3.0,
    "SUBVERSION": +4.0,
    "TERRITORY_CAPTURE": +5.0,
    "PRISONER_RELEASED": +1.5,
}


def lerp_table(table: list[tuple[float, float]], x: float) -> float:
    if x <= table[0][0]:
        return table[0][1]
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[-1][1]


# --------------------------------------------------------------------------- #
# Entities
# --------------------------------------------------------------------------- #
@dataclass
class Unit:
    id: int
    owner: Side
    type: UnitType
    pos: Coord
    rank: int = 1                   # 1..5
    hp: int = 0
    frozen: bool = False
    double_agent_until: Optional[int] = None   # round at which it flips back
    original_owner: Optional[Side] = None
    kills: int = 0                  # every 2nd kill = promotion (rank cap 5)
    carrier: Optional[int] = None   # transport id while embarked

    def __post_init__(self):
        if not self.hp:
            self.hp = UNIT_SPECS[self.type].hp

    @property
    def spec(self) -> UnitSpec:
        return UNIT_SPECS[self.type]


@dataclass
class Building:
    type: BuildingType
    owner: Optional[Side]           # None = neutral (capturable)
    pos: Coord
    build_turns_left: int = 0       # >0 while under construction

    @property
    def active(self) -> bool:
        return self.build_turns_left == 0


@dataclass
class Mine:
    owner: Side
    pos: Coord
    planted_round: int
    revealed_to: set = field(default_factory=set)   # sides that discovered it


@dataclass
class FactionState:
    side: Side
    res: dict
    moral: float = 50.0
    armory_cap: float = ARMORY_BASE_CAP

    def total_solvency(self) -> float:
        return sum(self.res.values())


# --------------------------------------------------------------------------- #
# Game state
# --------------------------------------------------------------------------- #
class GameState:
    def __init__(self, grid: list[list[Terrain]], difficulty: Difficulty, seed: int = 0):
        self.grid = grid
        self.h = len(grid)
        self.w = len(grid[0])
        self.difficulty = difficulty
        self.rng = random.Random(seed)
        self.round = 1
        self.active: Side = Side.PLAYER
        self.winner: Optional[Side] = None
        self.weather = Weather.CLEAR
        self.objective: Optional[dict] = None     # scenario objective
        self.scenario = "skirmish"
        self.persona = {Side.PLAYER: "balanced", Side.AI: "balanced"}
        self.units: dict[int, Unit] = {}
        self.prisoners: dict[int, tuple[Unit, Side]] = {}    # id -> (unit, captor)
        self.buildings: dict[Coord, Building] = {}
        self.mines: dict[Coord, Mine] = {}
        self.events: list[dict] = []
        self.round_hooks: list[Callable[["GameState"], None]] = []
        self._next_id = 1

        p_mult, a_mult = DIFFICULTY_START[difficulty]
        self.factions = {
            Side.PLAYER: FactionState(Side.PLAYER, {r: v * p_mult for r, v in BASE_START.items()}),
            Side.AI: FactionState(Side.AI, {r: v * a_mult for r, v in BASE_START.items()}),
        }
        self.start_res = {s: dict(f.res) for s, f in self.factions.items()}

    # ---- helpers ---------------------------------------------------------- #
    def emit(self, kind: str, **data) -> None:
        self.events.append({"type": kind, "round": self.round, **data})

    def drain_events(self) -> list[dict]:
        ev, self.events = self.events, []
        return ev

    def in_bounds(self, c: Coord) -> bool:
        return 0 <= c[0] < self.w and 0 <= c[1] < self.h

    def terrain_at(self, c: Coord) -> Terrain:
        return self.grid[c[1]][c[0]]

    @staticmethod
    def neighbors8(c: Coord):
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if dx or dy:
                    yield (c[0] + dx, c[1] + dy)

    def unit_at(self, c: Coord) -> Optional[Unit]:
        return next((u for u in self.units.values() if u.pos == c and u.carrier is None), None)

    def units_of(self, side: Side) -> list[Unit]:
        return [u for u in self.units.values() if u.owner is side]

    def spawn_unit(self, side: Side, utype: UnitType, pos: Coord, rank: int = 1) -> Unit:
        u = Unit(self._next_id, side, utype, pos, rank)
        self._next_id += 1
        self.units[u.id] = u
        self.emit("unit_spawned", unit=u.id, pos=pos, owner=side.value)
        return u

    def add_building(self, b: Building) -> None:
        self.buildings[b.pos] = b

    def count_buildings(self, side: Side, btype: BuildingType) -> int:
        return sum(1 for b in self.buildings.values()
                   if b.owner is side and b.type is btype and b.active)

    # ---- moral ------------------------------------------------------------ #
    def apply_moral(self, side: Side, key: str, n: int = 1) -> float:
        f = self.factions[side]
        f.moral = max(0.0, min(100.0, f.moral + MORAL_DELTAS[key] * n))
        self.emit("moral_changed", side=side.value, moral=f.moral, reason=key)
        return f.moral

    def income_mult(self, side: Side) -> float:
        return lerp_table(INCOME_MULT_TABLE, self.factions[side].moral)

    def consumption_mult(self, side: Side) -> float:
        weary = 1.0 + WEARINESS_RATE * max(0, self.round - WEARINESS_START)   # war weariness
        return lerp_table(CONSUMPTION_MULT_TABLE, self.factions[side].moral) * weary

    def supply_cap(self, side: Side) -> float:
        return lerp_table(SUPPLY_CAP_TABLE, self.factions[side].moral)

    # ---- resources -------------------------------------------------------- #
    def can_afford(self, side: Side, cost: dict) -> bool:
        res = self.factions[side].res
        return all(res[r] + 1e-9 >= amt for r, amt in cost.items())

    def spend(self, side: Side, cost: dict) -> bool:
        if not self.can_afford(side, cost):
            return False
        for r, amt in cost.items():
            self.factions[side].res[r] -= amt
        return True

    def drain(self, side: Side, cost: dict) -> dict:
        """Take as much as available (used by upkeep). Returns shortfalls."""
        short = {}
        res = self.factions[side].res
        for r, amt in cost.items():
            paid = min(res[r], amt)
            res[r] -= paid
            if paid < amt - 1e-9:
                short[r] = amt - paid
        return short

    def armory_room(self, side: Side) -> float:
        f = self.factions[side]
        return max(0.0, f.armory_cap - f.res[Res.BULLETS] - f.res[Res.BOMBS])

    def credit(self, side: Side, gains: dict) -> dict:
        """Add resources, clamping BULLETS+BOMBS to the armory capacity."""
        applied = {}
        f = self.factions[side]
        for r, amt in gains.items():
            if r in (Res.BULLETS, Res.BOMBS):
                amt = min(amt, self.armory_room(side))
                if amt < gains[r]:
                    self.emit("armory_overflow", side=side.value, lost=gains[r] - amt)
            f.res[r] += amt
            applied[r] = amt
        return applied

    def recompute_armory(self, side: Side) -> None:
        """Barracks raise recruitment but LOWER the armory pool (trade-off)."""
        n = self.count_buildings(side, BuildingType.BARRACKS)
        self.factions[side].armory_cap = max(
            ARMORY_MIN_CAP, ARMORY_BASE_CAP - ARMORY_PER_BARRACKS * n)

    # ---- movement & terrain burn ----------------------------------------- #
    def can_enter(self, unit: Unit, c: Coord) -> bool:
        if not self.in_bounds(c) or self.unit_at(c):
            return False
        if unit.spec.cls not in TERRAIN[self.terrain_at(c)].allowed:
            return False
        b = self.buildings.get(c)
        if b and b.type is BuildingType.WALL:
            return False
        return True

    def step_cost(self, unit: Unit, c: Coord) -> tuple[int, dict]:
        """(movement points, resource burn) for entering tile c."""
        ts = TERRAIN[self.terrain_at(c)]
        cm = self.consumption_mult(unit.owner)
        burn = {ts.burn_res: ts.burn_amt * cm * WEATHER_MULT.get((self.weather, self.terrain_at(c)), 1.0)}
        if unit.spec.move_fuel:
            burn[Res.FUEL] = burn.get(Res.FUEL, 0.0) + unit.spec.move_fuel * cm
        return ts.move_cost, burn

    def move_unit(self, unit_id: int, path: list[Coord]) -> dict:
        """Walk `path` (excluding the start tile). Stops at the first illegal
        or unaffordable step. Never moves frozen units."""
        u = self.units[unit_id]
        if self.winner or u.owner is not self.active:
            return {"moved": 0, "stopped": "not_your_turn"}
        if u.frozen:
            return {"moved": 0, "stopped": "frozen"}
        if u.carrier is not None:
            return {"moved": 0, "stopped": "embarked"}
        mp_left, moved, stopped = u.spec.mp, 0, None
        for c in path:
            if max(abs(c[0] - u.pos[0]), abs(c[1] - u.pos[1])) != 1:
                stopped = "not_adjacent"; break
            if not self.can_enter(u, c):
                stopped = "blocked"; break
            mp, burn = self.step_cost(u, c)
            if mp > mp_left:
                stopped = "out_of_mp"; break
            if not self.spend(u.owner, burn):
                stopped = "cannot_afford"; break
            mp_left -= mp
            self.emit("unit_moved", unit=u.id, src=u.pos, dst=c, burn=_ser(burn))
            u.pos = c
            for k in self.units.values():          # cargo travels with its transport
                if k.carrier == u.id:
                    k.pos = c
            moved += 1
        self.evaluate_bankruptcy(u.owner)
        return {"moved": moved, "stopped": stopped}

    # ---- bankruptcy & vulnerability -------------------------------------- #
    def evaluate_bankruptcy(self, side: Side) -> list[int]:
        """Recompute `frozen` for every unit of `side`; returns changed ids."""
        res = self.factions[side].res
        changed = []
        for u in self.units_of(side):
            should = any(res[r] <= 1e-9 for r in u.spec.lifeline)
            if should != u.frozen:
                u.frozen = should
                changed.append(u.id)
                self.emit("unit_frozen" if should else "unit_thawed",
                          unit=u.id, pos=u.pos)
        return changed

    # ---- naval cargo -------------------------------------------------------- #
    def load_unit(self, unit_id: int, transport_id: int) -> bool:
        u, t = self.units.get(unit_id), self.units.get(transport_id)
        if not u or not t or t.type is not UnitType.TRANSPORT or u.owner is not t.owner:
            return False
        if u.owner is not self.active or u.carrier is not None or u.frozen or t.frozen:
            return False
        if u.spec.cls is not UnitClass.FOOT or max(abs(u.pos[0] - t.pos[0]), abs(u.pos[1] - t.pos[1])) > 1:
            return False
        if sum(1 for k in self.units.values() if k.carrier == t.id) >= CARGO_CAP:
            return False
        u.carrier, u.pos = t.id, t.pos
        self.emit("unit_loaded", unit=u.id, carrier=t.id)
        return True

    def unload_unit(self, unit_id: int, c: Coord) -> bool:
        u = self.units.get(unit_id)
        t = self.units.get(u.carrier) if u and u.carrier is not None else None
        if not t or t.owner is not self.active or t.frozen or not self.in_bounds(c):
            return False
        if max(abs(c[0] - t.pos[0]), abs(c[1] - t.pos[1])) != 1 or not self.can_enter(u, c):
            return False
        u.carrier, u.pos = None, c
        self.emit("unit_unloaded", unit=u.id, pos=c)
        return True

    # ---- fog of war ---------------------------------------------------------- #
    def _sight_sources(self, side: Side) -> list[tuple[Coord, int]]:
        src = [(u.pos, u.spec.vision) for u in self.units.values() if u.owner is side and u.carrier is None]
        src += [(b.pos, 4 if b.type is BuildingType.HQ else 3) for b in self.buildings.values()
                if b.owner is side and b.active and b.type is not BuildingType.WALL]
        return src

    def visible_tiles(self, side: Side) -> set[Coord]:
        vis: set[Coord] = set()
        for (px, py), r in self._sight_sources(side):
            for x in range(px - r, px + r + 1):
                for y in range(py - r, py + r + 1):
                    if self.in_bounds((x, y)):
                        vis.add((x, y))
        return vis

    def can_see(self, side: Side, c: Coord) -> bool:
        """Forest/urban conceal units from anyone more than 2 tiles away."""
        hide = self.terrain_at(c) in CONCEAL
        for (px, py), r in self._sight_sources(side):
            d = max(abs(px - c[0]), abs(py - c[1]))
            if d <= r and (not hide or d <= 2):
                return True
        return False

    def heal_units(self) -> None:
        """Wounded units recover slowly next to their own HQ/bases/barracks (if fed)."""
        for u in self.units.values():
            if u.carrier is not None or u.hp >= u.spec.hp or self.factions[u.owner].res[Res.FOOD] <= 0:
                continue
            if any(b.owner is u.owner and b.active and b.type is not BuildingType.WALL
                   and max(abs(b.pos[0] - u.pos[0]), abs(b.pos[1] - u.pos[1])) <= 1
                   for b in self.buildings.values()):
                u.hp = min(u.spec.hp, u.hp + HEAL_PER_ROUND)

    def is_solvent(self, side: Side) -> bool:
        """'Enemy with greater than zero resources' - the attacker side check."""
        return self.factions[side].total_solvency() > 1e-9

    def is_vulnerable_to(self, target: Unit, attacker_side: Side) -> bool:
        return target.frozen and self.is_solvent(attacker_side)

    # ---- unit removal ----------------------------------------------------- #
    def kill_unit(self, unit_id: int, cause: str, moral_hit: bool = True) -> None:
        u = self.units.pop(unit_id, None)
        if not u:
            return
        for k in [x for x in self.units.values() if x.carrier == u.id]:
            self.kill_unit(k.id, "lost_with_transport")
        self.emit("unit_killed", unit=u.id, pos=u.pos, cause=cause, owner=u.owner.value)
        if moral_hit:
            self.apply_moral(u.owner, "UNIT_LOST")

    # ---- construction & capture ------------------------------------------ #
    def build(self, unit_id: int, btype: BuildingType, c: Coord) -> bool:
        u = self.units[unit_id]
        if u.owner is not self.active or u.frozen:
            return False
        if max(abs(c[0] - u.pos[0]), abs(c[1] - u.pos[1])) > 1:
            return False
        if (not self.in_bounds(c) or c in self.buildings or self.unit_at(c)
                or self.terrain_at(c) is Terrain.SEA):
            return False
        if not self.spend(u.owner, BUILD_COST[btype]):
            return False
        self.buildings[c] = Building(btype, u.owner, c, BUILD_TURNS[btype])
        self.emit("build_started", pos=c, btype=btype.value, turns=BUILD_TURNS[btype])
        return True

    def capture_building(self, unit_id: int, c: Coord) -> bool:
        """Only infantry capture territory (redemption mechanic)."""
        u = self.units[unit_id]
        b = self.buildings.get(c)
        if not b or u.type is not UnitType.INFANTRY or b.owner is u.owner or u.frozen:
            return False
        if u.pos != c:       # must stand on the node
            return False
        prev = b.owner
        b.owner = u.owner
        self.apply_moral(u.owner, "TERRITORY_CAPTURE")
        self.emit("building_captured", pos=c, btype=b.type.value, owner=u.owner.value)
        for s in (u.owner, prev):
            if s:
                self.recompute_armory(s)
        self.check_victory()
        return True

    # ---- round loop ------------------------------------------------------- #
    def end_turn(self) -> None:
        if self.winner:
            return
        if self.active is Side.PLAYER:
            self.active = Side.AI
            self.emit("turn_changed", active=self.active.value)
            return
        self.active = Side.PLAYER
        self.begin_round()

    def begin_round(self) -> None:
        self.round += 1
        if self.round % 3 == 0:
            self.weather = self.rng.choices(list(Weather), weights=[55, 30, 15])[0]
            self.emit("weather_changed", weather=self.weather.value)
        for b in self.buildings.values():
            if b.build_turns_left > 0:
                b.build_turns_left -= 1
                if b.build_turns_left == 0:
                    self.emit("build_finished", pos=b.pos, btype=b.type.value)
        for side in Side:
            self.recompute_armory(side)
            self.pay_upkeep(side)
        self.heal_units()
        if self.round % PAYDAY_INTERVAL == 0:
            for side in Side:
                self.payday(side)
        for side in Side:
            self.evaluate_bankruptcy(side)
        for hook in self.round_hooks:
            hook(self)
        self.check_victory()
        self.emit("turn_changed", active=self.active.value)

    def pay_upkeep(self, side: Side) -> None:
        cm = self.consumption_mult(side)
        need: dict = {}
        for u in self.units_of(side):
            for r, amt in u.spec.upkeep.items():
                need[r] = need.get(r, 0.0) + amt * cm
        short = self.drain(side, need)
        if short:
            self.emit("upkeep_shortfall", side=side.value, short=_ser(short))

    def payday(self, side: Side) -> dict:
        """final = base * min(1 + 0.2*bases, moral_cap) * moral_income_mult"""
        owned = self.count_buildings(side, BuildingType.SUPPLY_BASE)
        bases = len(self.connected_supply_bases(side))   # only bases linked to HQ pay out
        raw_supply = 1.0 + SUPPLY_BASE_BONUS * bases
        cap = self.supply_cap(side)
        supply = min(raw_supply, cap)
        mm = self.income_mult(side)
        shipment = {r: amt * supply * mm for r, amt in PAYDAY_BASE.items()}
        # Barracks recruits (cost food); trade-off vs armory handled above.
        self.credit(side, shipment)
        self.emit("payday", side=side.value, shipment=_ser(shipment),
                  supply_mult=supply, supply_uncapped=raw_supply,
                  moral_mult=mm, suppressed=raw_supply > cap + 1e-9,
                  disconnected=owned - bases)
        self._barracks_recruit(side)
        self._hq_conscript(side)
        return shipment

    def _barracks_recruit(self, side: Side) -> None:
        for b in list(self.buildings.values()):
            if b.owner is not side or b.type is not BuildingType.BARRACKS or not b.active:
                continue
            if not self.can_afford(side, {Res.FOOD: BARRACKS_RECRUIT_FOOD}):
                continue
            spot = next((n for n in self.neighbors8(b.pos)
                         if self.in_bounds(n) and not self.unit_at(n)
                         and self.terrain_at(n) in (Terrain.PLAINS, Terrain.FOREST)
                         and n not in self.buildings), None)
            if spot:
                self.spend(side, {Res.FOOD: BARRACKS_RECRUIT_FOOD})
                self.spawn_unit(side, UnitType.INFANTRY, spot)

    def connected_supply_bases(self, side: Side) -> list[Coord]:
        """Supply bases reachable from own HQ over land. Enemy units and the tiles
        next to them (zone of control), plus enemy buildings, cut the road."""
        hq = next((b.pos for b in self.buildings.values()
                   if b.type is BuildingType.HQ and b.owner is side), None)
        if not hq:
            return []
        mine = {u.pos for u in self.units.values() if u.owner is side}
        zoc = set()
        for u in self.units.values():
            if u.owner is not side:
                zoc.add(u.pos)
                zoc.update(self.neighbors8(u.pos))
        seen, q = {hq}, deque([hq])
        while q:
            c = q.popleft()
            for n in self.neighbors8(c):
                if n in seen or not self.in_bounds(n) or self.terrain_at(n) is Terrain.SEA:
                    continue
                b = self.buildings.get(n)
                if (n in zoc and n not in mine) or (b and b.owner is side.other):
                    continue
                seen.add(n)
                q.append(n)
        return [b.pos for b in self.buildings.values() if b.owner is side
                and b.type is BuildingType.SUPPLY_BASE and b.active and b.pos in seen]

    def _hq_conscript(self, side: Side) -> None:
        """Emergency draft: a side that is down to a handful of infantry gets one per payday."""
        if sum(1 for u in self.units_of(side) if u.type is UnitType.INFANTRY) >= HQ_CONSCRIPT_BELOW:
            return
        hq = next((b.pos for b in self.buildings.values() if b.type is BuildingType.HQ and b.owner is side), None)
        if not hq or not self.can_afford(side, {Res.FOOD: BARRACKS_RECRUIT_FOOD}):
            return
        spot = next((n for n in self.neighbors8(hq) if self.in_bounds(n) and not self.unit_at(n)
                     and self.terrain_at(n) in (Terrain.PLAINS, Terrain.FOREST) and n not in self.buildings), None)
        if spot:
            self.spend(side, {Res.FOOD: BARRACKS_RECRUIT_FOOD})
            self.spawn_unit(side, UnitType.INFANTRY, spot)
            self.emit("conscripted", side=side.value, pos=spot)

    def check_victory(self) -> Optional[Side]:
        for side in Side:
            has_hq = any(b.type is BuildingType.HQ and b.owner is side
                         for b in self.buildings.values())
            if not has_hq and self.winner is None:
                self.winner = side.other
                self.emit("game_over", winner=self.winner.value)
        o = self.objective
        if o and self.winner is None:
            if o["type"] == "survive" and self.round >= o["rounds"]:
                self.winner = Side.PLAYER
            elif o["type"] == "capture_bases":
                if self.count_buildings(Side.PLAYER, BuildingType.SUPPLY_BASE) >= o["count"]:
                    self.winner = Side.PLAYER
                elif self.round > o["rounds"]:
                    self.winner = Side.AI
            if self.winner:
                self.emit("game_over", winner=self.winner.value)
        return self.winner

    # ---- frontend snapshot ------------------------------------------------ #
    def snapshot(self, viewer: Side = Side.PLAYER) -> dict:
        f = self.factions[viewer]
        return {
            "round": self.round,
            "active": self.active.value,
            "winner": self.winner.value if self.winner else None,
            "difficulty": self.difficulty.value,
            "paydayIn": PAYDAY_INTERVAL - (self.round % PAYDAY_INTERVAL),
            "grid": [[t.value for t in row] for row in self.grid],
            "units": [{"id": u.id, "owner": u.owner.value, "type": u.type.value,
                       "pos": u.pos, "rank": u.rank, "hp": u.hp, "frozen": u.frozen, "kills": u.kills, "carrier": u.carrier}
                      for u in self.units.values()
                      if u.owner is viewer or (u.carrier is None and self.can_see(viewer, u.pos))],
            "buildings": [{"type": b.type.value, "owner": b.owner.value if b.owner else None,
                           "pos": b.pos, "turnsLeft": b.build_turns_left}
                          for b in self.buildings.values()],
            # Fog of war for mines: only your own + ones you discovered.
            "mines": [{"pos": m.pos, "owner": m.owner.value}
                      for m in self.mines.values()
                      if m.owner is viewer or viewer in m.revealed_to],
            "me": {"res": _ser(f.res), "moral": f.moral, "armoryCap": f.armory_cap,
                   "incomeMult": self.income_mult(viewer),
                   "consumptionMult": self.consumption_mult(viewer),
                   "supplyCap": self.supply_cap(viewer)},
            "enemyMoral": self.factions[viewer.other].moral,
            "visible": [list(c) for c in self.visible_tiles(viewer)],
            "weather": self.weather.value, "objective": self.objective, "scenario": self.scenario,
            "aiPersona": self.persona[viewer.other],
            "supplyConnected": len(self.connected_supply_bases(viewer)),
            "supplyTotal": self.count_buildings(viewer, BuildingType.SUPPLY_BASE),
        }


def _ser(d: dict) -> dict:
    return {(k.value if isinstance(k, Enum) else k): round(v, 2) for k, v in d.items()}


# --------------------------------------------------------------------------- #
# (De)serialisation - lets the game run on stateless serverless hosts (Vercel):
# the client stores the state and sends it back with every action.
# --------------------------------------------------------------------------- #
def _udump(u: Unit) -> dict:
    return {"id": u.id, "owner": u.owner.value, "type": u.type.value, "pos": list(u.pos),
            "rank": u.rank, "hp": u.hp, "frozen": u.frozen, "k": u.kills, "c": u.carrier, "da": u.double_agent_until,
            "oo": u.original_owner.value if u.original_owner else None}


def _uload(d: dict) -> Unit:
    u = Unit(d["id"], Side(d["owner"]), UnitType(d["type"]), tuple(d["pos"]), d["rank"],
                d["hp"], d["frozen"], d["da"], Side(d["oo"]) if d["oo"] else None)
    u.kills = d.get("k", 0)
    u.carrier = d.get("c")
    return u


def dump_state(s: GameState) -> dict:
    v, internal, gauss = s.rng.getstate()
    return {
        "v": SAVE_VERSION, "weather": s.weather.value, "objective": s.objective, "scenario": s.scenario,
        "persona": {k.value: v for k, v in s.persona.items()},
        "grid": [[t.value for t in r] for r in s.grid], "difficulty": s.difficulty.value,
        "round": s.round, "active": s.active.value, "winner": s.winner.value if s.winner else None,
        "next_id": s._next_id, "rng": [v, list(internal), gauss],
        "units": [_udump(u) for u in s.units.values()],
        "prisoners": [[_udump(u), c.value] for u, c in s.prisoners.values()],
        "buildings": [{"t": b.type.value, "o": b.owner.value if b.owner else None,
                       "p": list(b.pos), "n": b.build_turns_left} for b in s.buildings.values()],
        "mines": [{"o": m.owner.value, "p": list(m.pos), "r": m.planted_round,
                   "v": [x.value for x in m.revealed_to]} for m in s.mines.values()],
        "factions": {k.value: {"res": {r.value: x for r, x in f.res.items()},
                               "moral": f.moral, "cap": f.armory_cap} for k, f in s.factions.items()},
        "start_res": {k.value: {r.value: x for r, x in d.items()} for k, d in s.start_res.items()},
    }


def load_state(d: dict) -> GameState:
    if d.get("v") != SAVE_VERSION:
        raise ValueError("outdated save version")
    grid = [[Terrain(x) for x in r] for r in d["grid"]]
    s = GameState(grid, Difficulty(d["difficulty"]))
    s.round, s.active = d["round"], Side(d["active"])
    s.winner = Side(d["winner"]) if d["winner"] else None
    s._next_id = d["next_id"]
    s.weather, s.objective, s.scenario = Weather(d["weather"]), d["objective"], d["scenario"]
    s.persona = {Side(k): v for k, v in d["persona"].items()}
    v, internal, gauss = d["rng"]
    s.rng.setstate((v, tuple(internal), gauss))
    s.units = {u["id"]: _uload(u) for u in d["units"]}
    s.prisoners = {u["id"]: (_uload(u), Side(c)) for u, c in d["prisoners"]}
    s.buildings = {tuple(b["p"]): Building(BuildingType(b["t"]), Side(b["o"]) if b["o"] else None,
                                           tuple(b["p"]), b["n"]) for b in d["buildings"]}
    s.mines = {tuple(m["p"]): Mine(Side(m["o"]), tuple(m["p"]), m["r"], {Side(x) for x in m["v"]})
               for m in d["mines"]}
    for k, f in d["factions"].items():
        fs = s.factions[Side(k)]
        fs.res = {Res(r): x for r, x in f["res"].items()}
        fs.moral, fs.armory_cap = f["moral"], f["cap"]
    s.start_res = {Side(k): {Res(r): x for r, x in dd.items()} for k, dd in d["start_res"].items()}
    return s

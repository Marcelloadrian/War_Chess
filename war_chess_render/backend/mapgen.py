"""
mapgen.py - Procedural, fair, coastal map generation.

Pipeline
  1. Value-noise elevation + moisture fields (seeded).
  2. Coast gradient: sea bleeds in from the north and south edges (naval
     lanes / pincer routes), shaped by elevation noise.
  3. Biome assignment (sea / mud / forest / plains) from elevation+moisture.
  4. Cellular-automata smoothing (majority vote) to get coherent, realistic blobs.
  5. Urban clusters seeded, then grown.
  6. 180-degree rotational symmetry: the left half is generated, the right is
     its rotation -> perfectly fair lanes for both sides.
  7. Place HQs (west/east), Supply Bases, carve a guaranteed land road
     (A* free) between HQs, verify connectivity with BFS.
"""
from __future__ import annotations

import math
import random
from collections import deque

from engine import (
    Building, BuildingType, Coord, Difficulty, GameState, Side, Terrain, UnitType,
)


def _value_noise(w: int, h: int, rng: random.Random, cell: int) -> list[list[float]]:
    gw, gh = w // cell + 2, h // cell + 2
    lattice = [[rng.random() for _ in range(gw)] for _ in range(gh)]

    def smooth(t: float) -> float:
        return t * t * (3 - 2 * t)

    out = [[0.0] * w for _ in range(h)]
    for y in range(h):
        for x in range(w):
            gx, gy = x / cell, y / cell
            x0, y0 = int(gx), int(gy)
            tx, ty = smooth(gx - x0), smooth(gy - y0)
            a = lattice[y0][x0] * (1 - tx) + lattice[y0][x0 + 1] * tx
            b = lattice[y0 + 1][x0] * (1 - tx) + lattice[y0 + 1][x0 + 1] * tx
            out[y][x] = a * (1 - ty) + b * ty
    return out


def _smooth_ca(grid: list[list[Terrain]], passes: int, keep: set[Terrain]) -> None:
    h, w = len(grid), len(grid[0])
    for _ in range(passes):
        nxt = [row[:] for row in grid]
        for y in range(h):
            for x in range(w):
                if grid[y][x] in keep:
                    continue
                counts: dict[Terrain, int] = {}
                for dy in (-1, 0, 1):
                    for dx in (-1, 0, 1):
                        nx, ny = x + dx, y + dy
                        if 0 <= nx < w and 0 <= ny < h:
                            t = grid[ny][nx]
                            counts[t] = counts.get(t, 0) + 1
                best = max(counts, key=counts.get)
                if counts[best] >= 5:
                    nxt[y][x] = best
        grid[:] = nxt


def _land_path_exists(grid, a: Coord, b: Coord) -> bool:
    h, w = len(grid), len(grid[0])
    seen, q = {a}, deque([a])
    while q:
        x, y = q.popleft()
        if (x, y) == b:
            return True
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                n = (x + dx, y + dy)
                if ((dx or dy) and 0 <= n[0] < w and 0 <= n[1] < h and n not in seen
                        and grid[n[1]][n[0]] not in (Terrain.SEA, Terrain.URBAN)):
                    seen.add(n)
                    q.append(n)
    return False


def _carve_road(grid, a: Coord, b: Coord) -> None:
    """Straight-ish plains corridor guaranteeing a main lane of advance."""
    x, y = a
    while (x, y) != b:
        if grid[y][x] in (Terrain.SEA, Terrain.URBAN, Terrain.MUD, Terrain.FOREST):
            grid[y][x] = Terrain.PLAINS
        if x != b[0]:
            x += 1 if b[0] > x else -1
        elif y != b[1]:
            y += 1 if b[1] > y else -1
    grid[y][x] = Terrain.PLAINS


def generate(width: int = 50, height: int = 50, difficulty: Difficulty = Difficulty.MEDIUM,
             seed: int = 0) -> GameState:
    rng = random.Random(seed)
    elev = _value_noise(width, height, rng, 5)
    moist = _value_noise(width, height, rng, 4)

    grid = [[Terrain.PLAINS] * width for _ in range(height)]
    coast_depth = max(2, height // 6)
    for y in range(height):
        for x in range(width):
            edge = min(y, height - 1 - y)                    # distance to N/S edge
            coast = coast_depth * (0.55 + 0.9 * elev[y][x])  # wobbly shoreline
            e, m = elev[y][x], moist[y][x]
            if edge < coast - 0.5:
                grid[y][x] = Terrain.SEA
            elif edge < coast + 1.2 and m > 0.5:
                grid[y][x] = Terrain.MUD                    # tidal flats / swamp
            elif m > 0.62 and e > 0.35:
                grid[y][x] = Terrain.FOREST
            elif m > 0.72:
                grid[y][x] = Terrain.MUD

    # Urban clusters (seeded in the left half, grown by random walk)
    for _ in range(max(2, width * height // 160)):
        cx, cy = rng.randrange(4, width // 2), rng.randrange(coast_depth + 2, height - coast_depth - 2)
        for _ in range(rng.randint(4, 9)):
            if grid[cy][cx] is not Terrain.SEA:
                grid[cy][cx] = Terrain.URBAN
            cx = max(0, min(width - 1, cx + rng.choice((-1, 0, 1))))
            cy = max(0, min(height - 1, cy + rng.choice((-1, 0, 1))))

    _smooth_ca(grid, 2, keep={Terrain.URBAN})

    # Enforce 180-degree rotational symmetry (left half is the source of truth)
    for y in range(height):
        for x in range(width // 2):
            grid[height - 1 - y][width - 1 - x] = grid[y][x]

    hq_p: Coord = (2, height // 2)
    hq_a: Coord = (width - 3, height // 2 - (1 if height % 2 == 0 else 0))
    # clear a safe pad around each HQ
    for hq in (hq_p, hq_a):
        for dx in range(-2, 3):
            for dy in range(-2, 3):
                x, y = hq[0] + dx, hq[1] + dy
                if 0 <= x < width and 0 <= y < height:
                    grid[y][x] = Terrain.PLAINS
    if not _land_path_exists(grid, hq_p, hq_a):
        _carve_road(grid, hq_p, hq_a)
        # keep symmetry: mirror the carved road
        for y in range(height):
            for x in range(width // 2):
                if grid[y][x] is Terrain.PLAINS:
                    grid[height - 1 - y][width - 1 - x] = Terrain.PLAINS

    state = GameState(grid, difficulty, seed)
    state.add_building(Building(BuildingType.HQ, Side.PLAYER, hq_p))
    state.add_building(Building(BuildingType.HQ, Side.AI, hq_a))

    # Neutral supply bases: symmetric pairs, kept off sea tiles
    placed = 0
    attempts = 0
    while placed < 3 and attempts < 200:
        attempts += 1
        x = rng.randrange(6, width // 2 + 2)
        y = rng.randrange(coast_depth + 1, height - coast_depth - 1)
        mirror = (width - 1 - x, height - 1 - y)
        if (grid[y][x] in (Terrain.PLAINS, Terrain.FOREST)
                and (x, y) not in state.buildings and mirror not in state.buildings
                and mirror != (x, y)):
            state.add_building(Building(BuildingType.SUPPLY_BASE, None, (x, y)))
            state.add_building(Building(BuildingType.SUPPLY_BASE, None, mirror))
            placed += 1

    # Starting forces (mirrored)
    for i in range(4):
        for side, hq, sgn in ((Side.PLAYER, hq_p, 1), (Side.AI, hq_a, -1)):
            pos = (hq[0] + sgn * (1 + i % 2), hq[1] - 2 + i)
            if state.in_bounds(pos) and not state.unit_at(pos):
                state.spawn_unit(side, UnitType.INFANTRY, pos)
    for side, hq, sgn in ((Side.PLAYER, hq_p, 1), (Side.AI, hq_a, -1)):
        pos = (hq[0] + sgn, hq[1] + 2)
        if state.in_bounds(pos) and not state.unit_at(pos):
            state.spawn_unit(side, UnitType.ENGINEER, pos)
    for side, hq, sgn in ((Side.PLAYER, hq_p, 1), (Side.AI, hq_a, -1)):
        pos = (hq[0] + 2 * sgn, hq[1] + 2)
        if state.in_bounds(pos) and not state.unit_at(pos):
            state.spawn_unit(side, UnitType.SCOUT, pos)
    for side, hq, lo, hi in ((Side.PLAYER, hq_p, 0, width // 2), (Side.AI, hq_a, width // 2, width)):
        sea = [(x, y) for y in range(height) for x in range(lo, hi) if grid[y][x] is Terrain.SEA]
        if sea:
            state.spawn_unit(side, UnitType.TRANSPORT,
                             min(sea, key=lambda c: max(abs(c[0] - hq[0]), abs(c[1] - hq[1]))))
    state.drain_events()
    return state

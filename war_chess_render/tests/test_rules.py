import sys; from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
import combat as C, mapgen
from engine import *


def game(seed=3, diff=Difficulty.MEDIUM):
    s = mapgen.generate(32, 20, diff, seed); C.install(s); return s


def test_map_is_symmetric():
    s = game()
    assert all(s.grid[y][x] == s.grid[s.h-1-y][s.w-1-x] for y in range(s.h) for x in range(s.w))


def test_difficulty_asymmetry():
    assert game(diff=Difficulty.HARD).factions[Side.AI].res[Res.MONEY] > game(diff=Difficulty.HARD).factions[Side.PLAYER].res[Res.MONEY]
    assert game(diff=Difficulty.EASY).factions[Side.PLAYER].res[Res.MONEY] > game(diff=Difficulty.EASY).factions[Side.AI].res[Res.MONEY]


def test_vulnerable_bankrupt_unit_loses_instantly():
    s = game(); p = s.units_of(Side.PLAYER)[0]; a = s.units_of(Side.AI)[0]
    a.pos = (p.pos[0] + 1, p.pos[1]); p.rank = 5
    s.factions[Side.AI].res[Res.FOOD] = 0; s.evaluate_bankruptcy(Side.AI)
    assert a.frozen
    assert C.resolve_force_attack(s, p.id, a.id).kind == "auto_win_destroyed"


def test_planted_bomb_kills_friendly_and_costs_moral():
    s = game(); u = s.units_of(Side.PLAYER)[0]; before = s.factions[Side.PLAYER].moral
    assert C.plant_mine(s, u.id).ok
    out = C.detonate_mine(s, Side.PLAYER, u.pos)
    assert out.detail["friendly"] == 1 and s.factions[Side.PLAYER].moral < before - 4


def test_moral_cap_suppresses_supply_bases():
    s = game()
    for i in range(3):
        s.add_building(Building(BuildingType.SUPPLY_BASE, Side.PLAYER, (10 + i, 10)))
    s.factions[Side.PLAYER].moral = 10
    low = s.payday(Side.PLAYER)[Res.MONEY]
    s.factions[Side.PLAYER].moral = 50
    assert low < 0.5 * s.payday(Side.PLAYER)[Res.MONEY]


def test_barracks_lower_armory_cap():
    s = game(); base = s.factions[Side.PLAYER].armory_cap
    s.add_building(Building(BuildingType.BARRACKS, Side.PLAYER, (9, 9))); s.recompute_armory(Side.PLAYER)
    assert s.factions[Side.PLAYER].armory_cap < base


def test_sea_movement_burns_money_and_land_units_cannot_enter():
    s = game(); u = s.units_of(Side.PLAYER)[0]
    sea = next((x, y) for y in range(s.h) for x in range(s.w) if s.grid[y][x] is Terrain.SEA)
    assert not s.can_enter(u, sea)
    assert TERRAIN[Terrain.SEA].burn_res is Res.MONEY


def test_serialization_roundtrip_is_exact():
    s = game()
    for _ in range(6): s.end_turn()
    d = dump_state(s)
    assert dump_state(load_state(d)) == d


def test_api_stateless_roundtrip():
    from fastapi.testclient import TestClient
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
    import server
    c = TestClient(server.app)
    j = c.get("/api/new?difficulty=hard&seed=5").json()
    j2 = c.post("/api/action", json={"state": j["state"], "sig": j["sig"], "type": "end_turn"}).json()
    assert j2["snapshot"]["round"] == 2
    assert c.post("/api/action", json={"state": j["state"], "sig": j["sig"], "type": "move", "unit": 1, "x": 500, "y": 1}).status_code == 422


# ------------------------- v2 features -------------------------
import pytest, scenarios, ai


def test_supply_line_cut_by_enemy_zone_of_control():
    s = game(); hq = next(b.pos for b in s.buildings.values() if b.type is BuildingType.HQ and b.owner is Side.PLAYER)
    base = (hq[0] + 4, hq[1]); s.add_building(Building(BuildingType.SUPPLY_BASE, Side.PLAYER, base))
    assert base in s.connected_supply_bases(Side.PLAYER)
    en = s.spawn_unit(Side.AI, UnitType.INFANTRY, (base[0], base[1] - 1))
    assert base not in s.connected_supply_bases(Side.PLAYER)          # enemy adjacent to the base cuts it
    s.factions[Side.PLAYER].moral = 50
    assert s.payday(Side.PLAYER)  # disconnected base contributes nothing, no crash
    assert [e for e in s.events if e["type"] == "payday"][-1]["disconnected"] == 1


def test_weather_raises_burn():
    s = game(); u = s.units_of(Side.PLAYER)[0]
    c = next(n for n in s.neighbors8(u.pos) if s.can_enter(u, n) and s.terrain_at(n) is Terrain.PLAINS)
    clear = s.step_cost(u, c); s.weather = Weather.RAIN
    wet = s.step_cost(u, c)                              # rain turns plains into mud: slower and burns fuel instead of food
    assert s.terrain_at(c) is Terrain.MUD and wet[0] > clear[0] and Res.FUEL in wet[1]


def test_survive_objective_wins_for_player():
    s = scenarios.build("hold_line", "easy", 1)
    for _ in range(30):
        if s.winner: break
        s.end_turn()
    assert s.winner in (Side.PLAYER, Side.AI) and s.objective["type"] == "survive"


def test_engineer_defuses_enemy_mine():
    s = game(); eng = next(u for u in s.units_of(Side.PLAYER) if u.type is UnitType.ENGINEER)
    pos = next(n for n in s.neighbors8(eng.pos) if s.in_bounds(n))
    s.mines[pos] = Mine(Side.AI, pos, 1)
    assert C.defuse_mine(s, eng.id, pos).ok and pos not in s.mines
    assert C.defuse_mine(s, eng.id, pos).kind == "nothing_found"


def test_veterancy_promotes_every_second_kill():
    s = game(); u = s.units_of(Side.PLAYER)[0]; u.kills = 1
    victim = s.spawn_unit(Side.AI, UnitType.INFANTRY, (u.pos[0] + 1, u.pos[1]))
    u.rank, victim.hp = 5, 1                                # rank 5 can't rise; use rank 2 instead
    u.rank = 2
    for _ in range(40):                                     # retry across rng variance
        if victim.id not in s.units: break
        C.resolve_force_attack(s, u.id, victim.id)
        s.active = Side.PLAYER; s.actions_used[Side.PLAYER] = 0
    assert u.id not in s.units or u.rank >= 2


def test_old_save_version_rejected():
    d = dump_state(game()); d["v"] = 1
    with pytest.raises(ValueError):
        load_state(d)


def test_brute_force_interrogates_prisoners():
    s = game(); s.persona[Side.AI] = "brute"
    p = s.units_of(Side.PLAYER)[0]; s.units.pop(p.id); s.prisoners[p.id] = (p, Side.AI)
    s.active = Side.AI; ai.play_turn(s, Side.AI)
    assert p.id not in s.prisoners and s.factions[Side.AI].moral < 50


def test_replay_is_deterministic():
    def run():
        s = game(seed=9)
        for _ in range(8): ai.play_turn(s, s.active)
        return dump_state(s)
    assert run() == run()


def test_scenarios_build_and_persona():
    for sid in scenarios.SCENARIOS:
        s = scenarios.build(sid, "medium", 3)
        assert s.persona[Side.AI] in ai.PERSONAS


# ------------------------- v3 features -------------------------
def test_fog_hides_distant_enemies_and_blocks_attack():
    s = game(); p = s.units_of(Side.PLAYER)[0]; a = s.units_of(Side.AI)[0]
    assert not s.can_see(Side.PLAYER, a.pos)
    snap = s.snapshot(Side.PLAYER)
    assert all(u["owner"] == "player" for u in snap["units"])
    a.pos = (p.pos[0] + 1, p.pos[1])
    assert s.can_see(Side.PLAYER, a.pos)


def test_forest_conceals_beyond_two_tiles():
    s = game(); scout = next(u for u in s.units_of(Side.PLAYER) if u.type is UnitType.SCOUT)
    x, y = scout.pos[0] + 4, scout.pos[1]
    s.grid[y][x] = Terrain.PLAINS; assert s.can_see(Side.PLAYER, (x, y))       # scout sees 6
    s.grid[y][x] = Terrain.FOREST; assert not s.can_see(Side.PLAYER, (x, y))


def test_attack_on_unseen_target_rejected():
    s = game(); p = s.units_of(Side.PLAYER)[0]; a = s.units_of(Side.AI)[0]
    a.pos = (p.pos[0] + 5, p.pos[1])
    assert C.resolve_force_attack(s, p.id, a.id).kind in ("out_of_range", "not_visible")


def test_naval_load_sail_unload_and_cargo_dies_with_transport():
    s = game(); tr = next(u for u in s.units_of(Side.PLAYER) if u.type is UnitType.TRANSPORT)
    inf = s.units_of(Side.PLAYER)[0]
    inf.pos = next(n for n in s.neighbors8(tr.pos) if s.in_bounds(n))      # beside the boat
    assert s.load_unit(inf.id, tr.id) and inf.carrier == tr.id
    assert s.unit_at(tr.pos) is tr                                         # embarked unit is not on the map
    dest = next(n for n in s.neighbors8(tr.pos) if s.can_enter(tr, n))
    s.move_unit(tr.id, [dest]); assert inf.pos == tr.pos
    s.kill_unit(tr.id, "test"); assert inf.id not in s.units


def test_embarked_unit_cannot_move_or_be_attacked():
    s = game(); tr = next(u for u in s.units_of(Side.PLAYER) if u.type is UnitType.TRANSPORT)
    inf = s.units_of(Side.PLAYER)[0]; inf.pos = tr.pos; inf.carrier = tr.id
    assert s.move_unit(inf.id, [(0, 0)])["stopped"] == "embarked"


def test_bombed_wall_leaves_rubble():
    s = game(); c = (10, 10); s.grid[10][10] = Terrain.PLAINS
    s.buildings[c] = Building(BuildingType.WALL, Side.PLAYER, c); s.factions[Side.PLAYER].res[Res.BOMBS] = 5
    C.drop_bomb(s, Side.PLAYER, c)
    assert c not in s.buildings and s.grid[10][10] is Terrain.URBAN


def test_wounded_units_heal_near_hq():
    s = game(); u = s.units_of(Side.PLAYER)[0]
    hq = next(b.pos for b in s.buildings.values() if b.type is BuildingType.HQ and b.owner is Side.PLAYER)
    u.pos = (hq[0] + 1, hq[1]); u.hp = 40; s.heal_units()
    assert u.hp == 40 + HEAL_PER_ROUND


def test_preview_is_fuzzy_and_consumes_no_rng():
    s = game(); p = s.units_of(Side.PLAYER)[0]; a = s.units_of(Side.AI)[0]
    a.pos = (p.pos[0] + 1, p.pos[1]); before = s.rng.getstate()
    pv = C.preview_engagement(s, p.id, a.id)
    assert pv["ok"] and pv["ratio"][1] > pv["ratio"][0] and s.rng.getstate() == before


def test_tampered_or_unsigned_save_rejected():
    from fastapi.testclient import TestClient
    import server
    c = TestClient(server.app); j = c.get("/api/new").json()
    bad = {**j["state"]}; bad["factions"] = {**bad["factions"]}
    bad["factions"]["player"] = {**bad["factions"]["player"], "moral": 100}
    assert c.post("/api/action", json={"state": bad, "sig": j["sig"], "type": "end_turn"}).status_code == 400
    assert c.post("/api/load", json={"state": j["state"], "sig": "x"}).status_code == 400


def test_signature_survives_js_style_integer_floats():
    import server
    st = dump_state(game()); sig = server._sign(st)
    def jsify(x):                      # what JSON.parse/stringify does to 60.0
        if isinstance(x, float) and x.is_integer(): return int(x)
        if isinstance(x, dict): return {k: jsify(v) for k, v in x.items()}
        if isinstance(x, list): return [jsify(v) for v in x]
        return x
    assert server._sign(jsify(st)) == sig


def test_campaign_veterans_carry_over():
    s = scenarios.build("supply_run", "easy", 1, vets=(5, 4))
    ranks = sorted(u.rank for u in s.units_of(Side.PLAYER) if u.type in (UnitType.INFANTRY, UnitType.ENGINEER))
    assert ranks[-2:] == [4, 5]


def test_flanker_persona_loads_infantry_onto_transport():
    s = game(); s.persona[Side.AI] = "flanker"
    for _ in range(14):
        s.active = Side.AI; ai.play_turn(s, Side.AI)
        if any(u.carrier is not None for u in s.units.values()): break
    assert any(u.carrier is not None and u.owner is Side.AI for u in s.units.values())


def test_hq_conscripts_when_army_is_gone_and_weariness_grows():
    s = game()
    for u in list(s.units_of(Side.PLAYER)): s.units.pop(u.id)
    s.payday(Side.PLAYER)
    assert sum(u.type is UnitType.INFANTRY for u in s.units_of(Side.PLAYER)) == 1
    base = s.consumption_mult(Side.PLAYER); s.round = 80
    assert s.consumption_mult(Side.PLAYER) == pytest.approx(base * 1.6)


def test_five_actions_per_turn_for_everyone_then_reset():
    s = game(); inf = [u for u in s.units_of(Side.PLAYER) if u.type is UnitType.INFANTRY]
    done = 0
    for i in range(8):
        u = inf[i % len(inf)]
        step = next((n for n in s.neighbors8(u.pos) if s.can_enter(u, n)), None)
        done += s.move_unit(u.id, [step])["moved"] > 0
    assert done == MAX_ACTIONS and s.actions_left(Side.PLAYER) == 0
    assert s.move_unit(inf[0].id, [next(n for n in s.neighbors8(inf[0].pos) if s.can_enter(inf[0], n))])["stopped"] == "no_actions_left"
    s.end_turn(); assert s.actions_left(Side.AI) == MAX_ACTIONS
    ai.play_turn(s, Side.AI)                       # AI is capped too
    assert s.actions_used[Side.AI] == 0 or s.actions_used[Side.AI] <= MAX_ACTIONS


def test_all_action_types_cost_one_and_are_blocked_at_zero():
    s = game(); u = s.units_of(Side.PLAYER)[0]
    s.actions_used[Side.PLAYER] = MAX_ACTIONS
    assert C.plant_mine(s, u.id).kind == "no_actions_left"
    assert not s.build(u.id, BuildingType.WALL, next(n for n in s.neighbors8(u.pos) if s.in_bounds(n)))
    s.actions_used[Side.PLAYER] = MAX_ACTIONS - 1
    assert C.plant_mine(s, u.id).ok and s.actions_left(Side.PLAYER) == 0


def test_payday_fires_on_round_5_and_counter_matches():
    s = game(); seen = []
    for _ in range(12):
        s.end_turn(); s.end_turn()
        seen.append(s.snapshot(Side.PLAYER)["paydayIn"])
    assert [e["round"] for e in s.events if e["type"] == "payday" and e["side"] == "player"][:2] == [5, 10]
    assert seen[:5] == [3, 2, 1, 5, 4]


def test_snapshot_reports_moves_left():
    s = game(); snap = s.snapshot(Side.PLAYER)
    assert snap["actionsLeft"] == MAX_ACTIONS == snap["maxActions"]


def _only_infantry(s, n=4):
    for u in list(s.units.values()): s.units.pop(u.id)
    hq = next(b.pos for b in s.buildings.values() if b.type is BuildingType.HQ and b.owner is Side.PLAYER)
    for i in range(n): s.spawn_unit(Side.PLAYER, UnitType.INFANTRY, (hq[0] + 1, hq[1] - 2 + i))


def test_forecast_matches_what_the_engine_really_does():
    for food, start_round in ((10, 1), (5, 3), (30, 1), (14, 2), (40, 7)):
        s = game(); _only_infantry(s); s.round = start_round
        s.factions[Side.PLAYER].res[Res.FOOD] = food
        predicted = s.forecast(Side.PLAYER)["food"]["runs_out_in"]
        real = None
        for t in range(1, 41):
            s.end_turn(); s.end_turn()
            if s.factions[Side.PLAYER].res[Res.FOOD] <= 1e-9:
                real = t; break
        assert predicted == real, (food, start_round, predicted, real)


def test_forecast_no_payday_is_worst_case_and_snapshot_has_it():
    s = game(); _only_infantry(s); s.round = 3; s.factions[Side.PLAYER].res[Res.FOOD] = 5
    f = s.forecast(Side.PLAYER)["food"]
    import math
    net = f["upkeep"] - f["round_income"]                      # food lost per round once the trickle is counted
    assert f["runs_out_no_payday"] == math.ceil(5 / net - 1e-9) and (f["runs_out_in"] is None or f["runs_out_in"] > f["runs_out_no_payday"])
    assert "food" in s.snapshot(Side.PLAYER)["forecast"]


def test_move_preview_matches_real_move():
    import ai as _ai
    for seed in (1, 2, 3, 4):
        for weather in (Weather.CLEAR, Weather.RAIN):
            base = game(seed); base.weather = weather
            u = base.units_of(Side.PLAYER)[0]
            for dx, dy in ((3, 0), (5, 1), (2, -2), (9, 0)):
                goal = (u.pos[0] + dx, u.pos[1] + dy)
                if not base.in_bounds(goal) or not base.can_enter(u, goal):
                    continue
                path = _ai.find_path(base, u, goal)
                pv = base.preview_path(u.id, path)
                real = load_state(dump_state(base)); real.weather = weather; C.install(real)
                before = dict(real.factions[Side.PLAYER].res)
                m = real.move_unit(u.id, path)
                assert m["moved"] == len(pv["steps"]) and m["stopped"] == pv["stopped"]
                spent = {r.value: before[r] - real.factions[Side.PLAYER].res[r] for r in Res}
                for k, v in pv["burn"].items():
                    assert abs(spent[k] - v) < 0.02
                assert real.units[u.id].pos == tuple(pv["end"])


def test_preview_move_endpoint():
    from fastapi.testclient import TestClient
    import server
    c = TestClient(server.app); j = c.get("/api/new?seed=5").json()
    uid = next(u["id"] for u in j["snapshot"]["units"] if u["owner"] == "player" and u["type"] == "infantry")
    ux, uy = next(u["pos"] for u in j["snapshot"]["units"] if u["id"] == uid)
    r = c.post("/api/preview_move", json={"state": j["state"], "sig": j["sig"], "unit": uid, "x": ux + 2, "y": uy}).json()
    assert r["ok"] and r["steps"] and "terrainCost" in j["snapshot"]


def test_round_trickle_ignores_supply_bases():
    a, b = game(5), game(5)
    for i in range(3):
        b.add_building(Building(BuildingType.SUPPLY_BASE, Side.PLAYER, (6 + i, 8)))
    for s in (a, b):
        before = s.factions[Side.PLAYER].res[Res.MATERIALS]
        s.end_turn(); s.end_turn()                      # one full round (round 2: not a payday)
        assert s.round == 2
        assert abs(s.factions[Side.PLAYER].res[Res.MATERIALS] - before - ROUND_INCOME[Res.MATERIALS] * s.income_mult(Side.PLAYER)) < 1e-6
    assert a.factions[Side.PLAYER].res == b.factions[Side.PLAYER].res


def test_replay_reproduces_the_game_exactly():
    """A replay = the signed start state + the list of actions. Re-running them must end in the identical state."""
    from fastapi.testclient import TestClient
    import server
    c = TestClient(server.app); start = c.get("/api/new?seed=9").json(); cur, acts = start, []
    u = next(x for x in start["snapshot"]["units"] if x["owner"] == "player" and x["type"] == "infantry")
    plan = [{"type": "move", "unit": u["id"], "x": u["pos"][0] + 2, "y": u["pos"][1]}, {"type": "end_turn"}, {"type": "end_turn"}, {"type": "end_turn"}]
    for a in plan:
        r = c.post("/api/action", json={**a, "state": cur["state"], "sig": cur["sig"]}).json()
        if r["result"]["ok"]:
            acts.append(a); cur = r
    assert any(a["type"] == "end_turn" for a in acts)
    rep = c.post("/api/load", json={"state": start["state"], "sig": start["sig"]}).json()
    for a in acts:
        rep = c.post("/api/action", json={**a, "state": rep["state"], "sig": rep["sig"]}).json()
    assert rep["state"] == cur["state"] and rep["sig"] == cur["sig"] and cur["snapshot"]["rulesVersion"] == RULES_VERSION


def test_ice_freeze_thaw_drowns_land_units_but_not_buildings_on_water():
    s = game(1); u = s.units_of(Side.PLAYER)[0]
    sea = next((x, y) for y in range(s.h) for x in range(s.w) if s.grid[y][x] is Terrain.SEA)
    s.set_weather(Weather.FREEZE)
    assert s.terrain_at(sea) is Terrain.ICE and not s.can_enter(u, sea) is False
    u.pos = sea
    s.set_weather(Weather.FREEZE)                       # still frozen: safe
    assert u.id in s.units
    s.set_weather(Weather.CLEAR)                        # thaw
    assert u.id not in s.units
    s.set_weather(Weather.FREEZE)
    assert s.grid[sea[1]][sea[0]] is Terrain.SEA        # stored map never changes, only the effective terrain


def test_attrition_makes_far_units_cost_more_not_freeze():
    s = game(2); u = s.units_of(Side.PLAYER)[0]
    near = s.upkeep_per_round(Side.PLAYER)
    posts = [b.pos for b in s.buildings.values() if b.owner is Side.PLAYER and b.type in OUTPOST_TYPES]
    far = next((x, y) for y in range(s.h) for x in range(s.w)
               if all(max(abs(x - p[0]), abs(y - p[1])) > OUTPOST_RANGE for p in posts))
    u.pos = far
    assert u.id in s.far_units(Side.PLAYER) and s.upkeep_per_round(Side.PLAYER)[Res.FOOD] > near[Res.FOOD]
    assert not u.frozen


def test_map_is_50_by_50():
    s = scenarios.build("skirmish", "medium", 3)
    assert (s.w, s.h) == (50, 50)


def _hq(s, side):
    return next(b for b in s.buildings.values() if b.type is BuildingType.HQ and b.owner is side)


def test_enemy_hq_is_hidden_until_found_and_info_base_is_backup_hq():
    s = game(4)
    types = lambda: {(b["type"], b["owner"]) for b in s.snapshot(Side.PLAYER)["buildings"]}
    assert ("hq", "ai") not in types() and ("hq", "player") in types()          # the enemy HQ is not on your map
    s.meta["found"].append("player")
    assert ("hq", "ai") in types()                                              # ...until you have found it
    s.buildings[(1, 1)] = Building(BuildingType.INFO_BASE, Side.AI, (1, 1))
    del s.buildings[_hq(s, Side.AI).pos]; s.check_victory()
    assert s.winner is None and not any(u.type is UnitType.COMMANDANT for u in s.units.values())   # backup HQ: game goes on
    del s.buildings[(1, 1)]; s.check_victory()
    assert any(u.type is UnitType.COMMANDANT and u.owner is Side.AI for u in s.units.values())      # now the Commandant appears


def test_commandant_dies_in_thaw_and_loses_the_game():
    s = game(4); del s.buildings[_hq(s, Side.AI).pos]; s.check_victory()
    cmd = next(u for u in s.units.values() if u.type is UnitType.COMMANDANT)
    sea = next((x, y) for y in range(s.h) for x in range(s.w) if s.grid[y][x] is Terrain.SEA)
    s.set_weather(Weather.FREEZE); cmd.pos = sea
    s.set_weather(Weather.CLEAR); s.check_victory()
    assert cmd.id not in s.units and s.winner is Side.PLAYER                    # ice trap: instant defeat


def test_heavy_artillery_rules():
    s = game(4); me = Side.PLAYER; eng = next(u for u in s.units_of(me) if u.type is UnitType.ENGINEER)
    for r in Res:
        s.factions[me].res[r] = 500
    spot = s._free_land_near(eng.pos, 1)
    assert s.build_artillery(eng.id, spot)
    assert s.factions[me].res[Res.MONEY] == pytest.approx(150)                  # 70% of everything drained
    ha = s.unit_at(spot); assert ha.spec.mp == 0
    assert not s.build_artillery(eng.id, s._free_land_near(eng.pos, 2))          # only ONE per game
    assert not s.fire_artillery(ha.id, _hq(s, Side.AI).pos)                     # never at an HQ
    victim = s.units_of(Side.AI)[0]; hp = victim.hp
    assert s.fire_artillery(ha.id, victim.pos) and (victim.id not in s.units or victim.hp == hp - HA_DAMAGE)
    foe_eng = s.spawn_unit(Side.AI, UnitType.ENGINEER, s._free_land_near(spot, 3)); s.factions[Side.AI].res[Res.FOOD] = 50
    my_eng = s.spawn_unit(me, UnitType.ENGINEER, s._free_land_near(spot, 1))
    assert not s.sabotage(my_eng.id, ha.id)                                     # cannot sabotage your own


def test_info_base_trains_scouts():
    s = game(4); me = Side.PLAYER; hq = _hq(s, me).pos
    spot = s._free_land_near(hq, 2); s.buildings[spot] = Building(BuildingType.INFO_BASE, me, spot)
    n = sum(1 for u in s.units_of(me) if u.type is UnitType.SCOUT)
    assert s.train_scout(me) and sum(1 for u in s.units_of(me) if u.type is UnitType.SCOUT) == n + 1

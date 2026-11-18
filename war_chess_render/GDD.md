# WAR CHESS — Technical Game Design Document

*Single-player PvE tactical strategy: deterministic chess-like grid movement + war-sim logistics.*

---

## 0. Pillars

1. **Logistics is the real enemy.** Every tile you enter costs something; running dry is lethal.
2. **Morality is an economy.** Brutality is cheap now, expensive later; mercy is slow but compounding.
3. **Two doctrines, one board.** Force (bullets) vs. Subversion (money) are both viable and both risky.
4. **Deterministic core, gambled edges.** Movement and costs are exact. Only force variance and gacha rolls use the seeded RNG.
5. **The AI plays by the same rules.** Difficulty = starting resources only.

---

## 1. Rule Set (resolved & tuned)

### 1.1 Grid
Square grid, 8-directional (chess-king adjacency), single occupancy per tile. Square was chosen over hex for simpler mine/wall/surround logic and chess familiarity. The `neighbors8` abstraction is the only adjacency source, so a hex swap is a one-file change.

### 1.2 Terrain (data in `engine.TERRAIN`)

| Terrain | Glyph | Move cost | Defense | Ranged cover | LOS | Burns (per tile entered) | Allowed |
|---|---|---|---|---|---|---|---|
| Plains/Road | `.` | 1 | +0% | 0% | open | 0.5 Food | Land, Amphib |
| Forest | `/` hatch | 2 | +25% | 50% | blocks | 2 Food | Foot, Vehicle |
| Urban/Ruins | 🧱 | 2 | +50% | 35% | blocks | 2 Materials | Foot only |
| Mud/Swamp | 🌊 | 3 | 0% | 0% | open | 2 Fuel | Land, Amphib |
| Sea | ⚓ | 1 | 0% | 0% | open | 3 Money | Naval, Amphib |

All burn values are multiplied by the **moral consumption multiplier**. Vehicles and ships additionally burn `move_fuel` Fuel per tile.

### 1.3 Resources
Money · Food · Bullets · Bombs · Materials · Fuel, plus the **Armory Pool** (a shared cap on Bullets + Bombs). Each Barracks lowers the cap by 40 (base 200, floor 50).

### 1.4 Units (v1 roster)

| Unit | Class | Force | HP | MP | Range | Upkeep/round | Frozen when zero |
|---|---|---|---|---|---|---|---|
| Infantry | Foot | 10 | 100 | 3 | 1 | 1 Food | Food |
| Armor | Vehicle | 25 | 160 | 4 | 2 | 1 Food, 0.5 Fuel | Food, Fuel |
| Destroyer | Naval | 30 | 180 | 4 | 3 | 1 Food, 1 Money | Money, Fuel |
| Transport | Amphib | 4 | 90 | 4 | 1 | 0.5 Food, 0.5 Money | Money |

"Lifeline" resources are per unit type, so a fuel-starved army loses its tanks but its infantry still walk. This makes bankruptcy *partial and legible* rather than a single global off-switch.

### 1.5 Buildings

| Building | Cost | Build time | Effect |
|---|---|---|---|
| Wall | 10 Materials | 1 | Blocks movement; destroyed by bombs |
| Supply Base | 40 Mat, 30 Money | 3 | +20% payday each (capped by morale) |
| Barracks | 35 Mat, 20 Money | 2 | Free infantry each payday (5 Food); −40 armory cap |
| HQ | — | — | Lose it → lose the game. Capturable by infantry |

### 1.6 Planted Bombs
Unit spends its action planting on its own tile (1 Bomb + 2 Materials). Invisible to the enemy unless revealed by interrogation or a Backfire. **Owner-triggered detonation kills every unit on that tile, friend or foe.** Friendly deaths cost an additional −4 moral each. Strategic use: bait tiles, chokepoints, "sacrifice" plays (a frozen unit that is about to be captured can be detonated under its captors).

### 1.7 Turn structure
`Player turn → AI turn → Round ends`. On round start: construction ticks → upkeep → **payday if `round % 5 == 0`** → bankruptcy re-evaluation → round hooks (double agents flip back) → victory check.

---

## 2. Economy & Morale Math

### 2.1 Difficulty start multipliers (× base stock)

| Mode | Player | AI |
|---|---|---|
| Easy | 1.40 | 0.80 |
| Medium | 1.00 | 1.00 |
| Hard | 0.90 | 1.40 |

Base stock: Money 150, Food 60, Bullets 60, Bombs 15, Materials 50, Fuel 40 (tightened after headless simulation showed the first values never caused bankruptcy).

### 2.2 Payday
```
final[res] = PAYDAY_BASE[res] × min(1 + 0.20·bases, supplyCap(moral)) × incomeMult(moral)
```
`PAYDAY_BASE`: Money 40, Food 16, Bullets 20, Bombs 5, Materials 20, Fuel 12.

### 2.3 Moral scaling (0–100, start 50, piecewise-linear)

| Moral | Income × | Consumption × | Supply-base cap |
|---|---|---|---|
| 0 | 0.30 | 1.50 | ×1.00 |
| 25 | 0.60 | 1.25 | ×1.00 |
| 30 | 0.68 | 1.20 | ×1.00 |
| 50 | 1.00 | 1.00 | ×1.40 |
| 60 | 1.06 | 0.98 | ×1.60 |
| 75 | 1.15 | 0.95 | ×1.94 |
| 100 | 1.30 | 0.90 | ×2.50 |

At moral ≤ 30, **supply bases contribute nothing**, and income is also only 30–68% of normal. A 5-base empire at moral 10 earns about 0.42× the base shipment, well under half of what a 0-base empire earns at moral 50. That is the "shit morale still suppresses you" rule, made concrete.

### 2.4 Moral events

| Event | Δ moral |
|---|---|
| Force interrogation (lethal) | −8 |
| Resource interrogation | −0.5 |
| Own unit lost | −2 |
| Own mine kills own unit (extra) | −4 each |
| Gacha Backfire | −3 |
| Capture enemy alive | +3 |
| Successful subversion | +4 |
| Infantry captures building | +5 |
| Release prisoner | +1.5 |

**Loop check:** one force interrogation (−8) costs about 2.7 live captures to undo. Interrogating every prisoner is a death spiral on purpose. Keep it as the desperate tool.

### 2.5 Bankruptcy & Vulnerability
- A unit is `frozen` the instant any of its lifeline resources is ≤ 0. It can't move or initiate attacks. It can be thawed by income.
- **Vulnerability:** if `target.frozen` and the attacker faction has `total resources > 0`, the attacker wins instantly. This skips force, rank, terrain and bullet cost. Attacker may choose destroy or (infantry) capture.
- Because a bankrupt AI is just as exposed as a bankrupt player, **starving the enemy is a legitimate win path**: cut supply bases, push naval blockades (sea movement burns Money).

---

## 3. Combat & Engagement

### 3.1 Force Number
```
force = base × (1 + 0.15·(rank−1)) × (0.5 + 0.5·hp/maxHP) × (0.85 + 0.30·moral/100) × (1 + defenderTerrainDef)
clash: each side × U(0.85, 1.15)
ratio ≥ 1.0  → attacker wins (loser destroyed, winner damaged 35/ratio)
0.7–1.0     → repelled (both damaged)
< 0.7       → attacker destroyed
```
Costs bullets per attack. Bombs are a separate AoE (radius 1, 70/35 dmg, reduced by cover, destroy walls, hit friendlies).

### 3.2 Gacha subversion
Cost = `20 × rank × offerMultiplier (1–3)` Money.

| Tier | Base weight | Result |
|---|---|---|
| SSR Defection | 4% | Unit joins you; +4 moral |
| SR Surrender | 16% | Infantry become prisoners (+3, +4); others defect |
| R Resisted | 70% | Money lost |
| SSR Backfire | 10% | Fake defector (**double agent**), leaks all your mines, flips back after 3 rounds; −3 moral |

Modifiers: higher offer ↑success ↓backfire · frozen target ×2.5 surrender · your moral < 40 halves success · enemy moral < 50 raises success · each target rank above 1 → ×0.9.
Raw-odds baseline: ~20% success per roll, so bribery is a **mid-game luxury and a finisher for starved units**, not a spam button.

### 3.3 Capture
- **Single:** infantry only, force ratio ≥ 1.6 and target ≤ 60% HP (or target frozen).
- **Surround:** all passable neighbours enemy-held/walled/edge, ≥ 2 adjacent enemies. Captures all surrounded infantry in one action; non-infantry are destroyed.

### 3.4 Interrogation
| Mode | Cost | Result | Moral |
|---|---|---|---|
| Force | 0 | Prisoner dies; **all** enemy mines revealed | −8 |
| Resource | 10/20/35 Money (+½ Food) by difficulty | Prisoner lives; ~50% of mines revealed | −0.5 |
| Release | 0 | Prisoner freed | +1.5 |

---

## 4. Frontend Architecture

### 4.1 Rendering
- **Stack:** PixiJS (WebGL, tile batching) or Canvas2D for the MVP. Map ≤ 40×28, so even Canvas2D is fine.
- **Layers (bottom → top):** `terrain` (baked once to a RenderTexture, redrawn only on destruction) → `terrainDetail` (hatch/brick/wave sprite patterns, tiled) → `mines` (own + revealed only) → `buildings` → `units` → `overlays` (move range, target ring, frozen chains) → `fx`.
- **Terrain visual debuffs:** a small corner icon shows the burn resource (🌾 / 🧱 / ⛽ / 💰) and a move-cost number. Hovering a path shows the **total resource bill before commit**.
- **Planted bombs:** the server never sends enemy mines in the snapshot, so the client *cannot* leak them. Own mines: pulsing red dot + "💣 armed". Revealed enemy mines: hollow red diamond.
- **Construction:** `turnsLeft > 0` buildings draw as scaffolding with a radial progress ring (`constructing` map in store).
- **Frozen units:** desaturated sprite, ice-blue ring, chain icon, 2s pulse. Add a tooltip: "Vulnerable to any solvent enemy".

### 4.2 HUD layout
```
┌ TOP BAR ────────────────────────────────────────────────────────────┐
│ ROUND 12 [HARD]  💰412 🌾88 🔫54 💣9 🧱31 ⛽40   ARMORY 63/160   PAYDAY ▸▸▸▢▢ (2)│
├ LEFT ─────────┬ BOARD ───────────────────────────┬ RIGHT ────────────┤
│ MORAL METER   │                                  │ UNIT INSPECTOR     │
│ ▮▮▮▮▯▯ 38     │                                  │ type/rank/hp       │
│ Income ×0.78  │                                  │ force preview      │
│ Burn ×1.18    │                                  │ lifeline status    │
│ Supply cap ×1 │                                  │ [Move][Attack]     │
│ WARNINGS      │                                  │ [Build/Plant]      │
├───────────────┴──────────────────────────────────┴───────────────────┤
│ EVENT LOG / TOASTS                                         [END TURN]│
└──────────────────────────────────────────────────────────────────────┘
```
Moral meter shows both **income × and burn ×** live, and a red notch at 30 labelled "supply bonus lost".

### 4.3 Modals
| Modal | Contents |
|---|---|
| Engagement | Force preview (±15% band), bullet cost, odds table for Bribe + offer slider (1–3×) with live cost, Capture eligibility. **If target is frozen and you're solvent, replace Force with an "INSTANT WIN" banner.** |
| Interrogation | Prisoner card, two big buttons with moral deltas shown, "Release" option |
| Build/Plant | Cards with cost, affordability, trade-off note (Barracks lowers armory, Plant Bomb warns about friendly fire) |
| Gacha result | Reveal animation by tier colour; Backfire is shown as a normal defection for ~1s, then **flips**, which is a deliberately cruel but fun twist |

### 4.4 State machine
`AWAITING_INPUT ↔ UNIT_SELECTED ↔ TARGETING → MODAL_OPEN → ANIMATING → ENEMY_TURN → ANIMATING → AWAITING_INPUT | GAME_OVER`. Input is locked during `ANIMATING` and `ENEMY_TURN`. See `frontend/uiState.ts`.

### 4.5 Client/server contract
Every action returns `{snapshot, events[]}`. The client never computes outcomes, only previews (force, cost, odds), which it asks the server for via `preview_*` endpoints so numbers never drift.

---

## 5. Backend Architecture

```
backend/
  engine.py   state, data tables, economy tick, bankruptcy, build/capture, snapshot
  combat.py   force, bombs, capture/surround, interrogation, gacha, mines
  mapgen.py   noise+CA map generation, symmetry, HQs, supply bases
  ai.py       (next milestone, spec below)
```

### 5.1 Map generation (`mapgen.py`)
1. Two seeded value-noise fields (elevation, moisture).
2. Sea bleeds in from the **north and south edges** with noise-wobbled depth, giving two naval lanes with a land belt between them.
3. Biome rules: low edge + wet → mud (tidal flats); wet & high → forest; very wet → mud; default plains.
4. 2 passes of cellular-automata majority smoothing so biomes form contiguous blobs.
5. Urban clusters via short random walks, protected from smoothing.
6. **180° rotational symmetry:** left half is truth, right half is its rotation. This guarantees identical lane geometry for both sides; fairness comes from the structure, not tuning.
7. HQs at west/east with a cleared 5×5 pad; BFS verifies a land path, otherwise carve and re-mirror a road. Neutral supply bases are placed in mirrored pairs.

The naval pincer is real: transports can sail either coast and land on plains behind the enemy line, burning 3 Money per sea tile, so *flanking is a money decision*.

### 5.2 AI decision loop (`ai.py` spec)
Utility-scored, same rules as the player. Per AI turn:
```
1. SURVIVAL: for each own frozen unit → if enemy adjacent and enemy solvent: mark "doomed".
            Doomed + own mine under it → detonate (the sacrifice play).
2. ECONOMY : compute runway = resource / burn_rate per resource (rounds until zero).
            If any runway < 3: reroute units to supply bases / cheaper terrain, prioritise
            building Supply Base, avoid Mud/Sea, stop bribing.
3. OPPORTUNISM: scan player frozen units → free auto-win targets (score 100).
4. OBJECTIVES (score each candidate action):
     HQ push        = 40 · (1 − enemyHQDefense) · (supply runway ok)
     Capture node   = 25 + 10·supply_value        (infantry only)
     Naval pincer   = 30 if transport available and Money > 3 × seaTilesNeeded × burn
     Attack         = 20 · min(forceRatio, 2) − bulletCost
     Bribe          = EV(odds) · unitValue − cost   (only if Money > reserve)
     Build          = priority by shortfall (Materials → Wall near HQ; Supply Base if bases < player)
     Plant bomb     = 15 on chokepoints along player's shortest path to HQ
5. MORAL POLICY: only force-interrogate if moral > 60 (it can afford the dip) or desperate (HQ under threat).
6. Execute top-scoring non-conflicting actions per unit; difficulty only changes starting stock.
```
Hard AI uses the same scorer; it just starts richer. No hidden bonuses, no omniscient mines (it sees only mines in `snapshot('ai')`).

---

## 6. Balance Flags & Design Risks (worth playtesting early)

| Risk | Why it matters | Suggested mitigation |
|---|---|---|
| **Frozen death-spiral** | Bankruptcy → freezing → instant losses → morale drop → lower income → more bankruptcy | Morale floor on income at 0.30; thaw happens immediately on payday; consider a 1-round "grace freeze" before vulnerability applies |
| **Single global Food pool** | One bad payday can freeze the whole infantry line | Already mitigated by per-unit lifelines; consider a ration "reserve" building |
| **Bribe spam** | Money is also naval upkeep, so naval play and bribery compete | Rank scaling + 70% fail rate; consider a per-turn bribe cap |
| **Mine abuse** | Detonation under a captor stack is huge | Costs a Bomb + Materials, cannot plant on sea, the Backfire tier leaks your mines |
| **Vulnerability trivialises ranks** | Rank 5 tank loses to a zero-rank grunt once starved | That is the design intent; surface it clearly in the UI (warnings + tooltips) |
| **Armory vs Barracks** | If armory cap < payday bullets, shipments overflow | `armory_overflow` event and HUD "full" state |

---

## 7. Roadmap
1. **M1 (done in this repo):** engine, combat, mapgen, UI state layer, smoke-tested.
2. **M2:** `ai.py` per §5.2, HTTP/WebSocket wrapper (FastAPI), preview endpoints.
3. **M3:** Pixi renderer + HUD components bound to `UiStore`.
4. **M4:** Balance pass via headless AI-vs-AI sims (the engine is I/O-free and seeded, so thousands of games run in seconds).
5. **M5:** Save/load (state is plain dataclasses), campaign scenarios, audio.

## 8. Open Questions for You
1. Should **frozen units be able to defend with a Force clash if the attacker is *also* nearly broke**? (Currently: any attacker with >0 total resources auto-wins. A higher threshold would reduce brutality.)
2. Should the **AI be allowed to bribe your units** with the same gacha (including Backfire against *itself*)? I assumed yes, since the rules are identical.
3. Do you want **fog of war** beyond mines? The snapshot function is already viewer-scoped, so it is a small extension.


---

## 9. v2 Additions

| Feature | Rule |
|---|---|
| **Supply lines** | Payday counts only supply bases connected to your HQ over land. Enemy units and the 8 tiles around them (zone of control), plus enemy buildings, cut the road. Cut roads show as "CUT OFF" in the payday log and the 🔗 HUD counter. |
| **Weather** | Re-rolled every 3 rounds (55% clear / 30% rain / 15% storm). Rain: plains and mud burn ×1.5, forest ×1.25. Storm: sea ×2, mud ×1.5, plains ×1.25. |
| **Engineer** | New unit (Force 6, HP 80). Defuses mines on an adjacent tile, any owner. Probing an empty tile is free. Each side starts with one. |
| **Veterancy** | Every 2nd kill in a Force clash promotes a unit (rank cap 5, +15% force per rank). |
| **AI commanders** | *Balanced*, *Banker* (35% bribe chance, spends from 90 Money), *Brute* (never bribes, force-interrogates every prisoner). Shown in the HUD. |
| **Scenarios** | Supply Run (tutorial, capture 2 bases by round 14), Hold the Line (survive to round 15 vs a Brute), Lean War (half food). Random Skirmish remains. |
| **Codex** | In-game Field Manual (📖) covering terrain, weather, supply, bankruptcy, doctrines, mines, morale. |

### Simulation results (headless, `tools/simulate.py`)
- **Difficulty works:** balanced AI as player wins ~83% on Easy, ~50% Medium, ~17% Hard (12 games each).
- **No dominant persona** in a small persona matrix (overall win rate balanced 58%, banker 42%, brute 38%), but sample sizes are small. Run with a larger N before trusting this.
- **Open issues the simulation exposed:** AI mines are never detonated (the AI plants them but its trigger rule rarely fires), and unit freezes are very frequent (~150 across 36 games), so bankruptcy may be too punishing. Consider the grace period in section 6.


---

## 10. v3 Additions ("no mercy" pass)

**Design stance:** this is a war simulator. There is no undo, no bankruptcy grace period and no emergency bailout. Frozen units die, mistakes stick, and the economy and morale loops stay harsh on purpose.

| Feature | Rule |
|---|---|
| **Fog of war** | You see tiles within sight of your units (3; scout 6; HQ 4; bases 3). Enemies in Forest/Urban are hidden unless within 2 tiles. You cannot attack, bribe or capture what you cannot see. The server never sends hidden units. The AI is fog-limited too. |
| **Scout** | Force 3, HP 60, MP 5, vision 6. Also sharpens combat estimates near a target. |
| **Naval loading** | Transport carries 2 foot units (infantry, engineer, scout). Board when adjacent, unload onto an adjacent enterable tile. Cargo is off the map, cannot act, and dies with the transport. |
| **Fuzzy intel** | The engagement screen shows a force-ratio *band* (±25%, or ±10% with a scout within 5 tiles, centre deterministically offset), bribe cost, ~success % (5% steps) and backfire risk. It never consumes RNG. A bankrupt target is shown exactly. |
| **Lasting consequences** | Wounded units heal +8 HP/round beside own HQ/bases/barracks if fed. Bombed walls leave rubble (Urban terrain). Surviving veterans' ranks carry into the next campaign mission. |
| **AI mines** | Sapper engineers seed up to 6 mines along the enemy's approach lane. The AI detonates when only enemies stand on one, or sacrifices a doomed frozen unit. |
| **Flanker AI** | Loads 2 infantry, sails to the enemy HQ coast, lands them, and does not shuttle back. |
| **Signed saves** | HMAC-SHA256 over the canonical state. Hand-edited saves are rejected. Set `WARCHESS_SECRET` in Vercel. Note: RNG state is inside the save, so reloading cannot reroll an outcome, but a player can still keep an *older* copy of their own save. True anti-undo needs server-side state (a database). |

### Simulation (6 games per cell, medium unless noted)
- Difficulty trend is monotonic: balanced AI as player wins 44% Easy / 39% Medium / 28% Hard (draws 28-39%).
- No commander dominates; Banker (40%) and Flanker (38%) lead, Balanced (23%) trails. Sample is small (6/cell), so run `python tools/simulate.py 20` before tuning.
- Known issue: ~30-40% of sim games hit the 100-round cap (fog slows contact). Consider a stronger AI "search" behaviour or a round limit objective.


---

## 11. v4 Additions
| Change | Why (from simulation) |
|---|---|
| **HQ conscription** | Mutual annihilation left both sides with no infantry to capture an HQ, so games froze. The HQ now drafts 1 infantry per payday (5 Food) while you field fewer than 4. |
| **War weariness** | After round 60 all upkeep and terrain burn grow 3% per round. Long stalemates now starve both sides. |
| **HQ commitment (AI)** | Infantry chased the nearest base and flipped bases back and forth (35-62 captures per game). Half the infantry (and all after round 40) now go straight for the enemy HQ. |

Result (6 games per cell): Easy draws fell from 39% to 11% (player 72% / AI 17%). Medium and Hard still show ~44% draws, so stalemates are reduced, not solved. Next lever: smarter search when fog hides the enemy, or an HQ-siege objective.

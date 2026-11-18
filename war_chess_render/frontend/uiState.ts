/**
 * uiState.ts - Framework-agnostic HUD state, modal controller, turn-phase
 * state machine, warning triggers and animation queue.
 *
 * Works with React (useSyncExternalStore), Svelte stores or plain DOM.
 * The backend is authoritative: the UI only ever consumes `Snapshot` + `GameEvent[]`
 * (see engine.py -> snapshot() / drain_events()).
 */

// ------------------------------------------------------------------ types ---
export type Side = "player" | "ai";
export type ResKey = "money" | "food" | "bullets" | "bombs" | "materials" | "fuel";
export type Coord = [number, number];

export interface UnitDTO { id: number; owner: Side; type: string; pos: Coord; rank: number; hp: number; frozen: boolean }
export interface BuildingDTO { type: "wall" | "supply_base" | "barracks" | "hq"; owner: Side | null; pos: Coord; turnsLeft: number }
export interface Snapshot {
  round: number; active: Side; winner: Side | null; difficulty: "easy" | "medium" | "hard";
  paydayIn: number; grid: string[][]; units: UnitDTO[]; buildings: BuildingDTO[];
  mines: { pos: Coord; owner: Side }[];             // already fog-filtered by the server
  me: { res: Record<ResKey, number>; moral: number; armoryCap: number; incomeMult: number; consumptionMult: number; supplyCap: number };
  enemyMoral: number;
}
export interface GameEvent { type: string; round: number; [k: string]: any }

// ---------------------------------------------------------- phase machine ---
export type Phase =
  | "AWAITING_INPUT" | "UNIT_SELECTED" | "TARGETING" | "MODAL_OPEN"
  | "ANIMATING" | "ENEMY_TURN" | "GAME_OVER";

const TRANSITIONS: Record<Phase, Phase[]> = {
  AWAITING_INPUT: ["UNIT_SELECTED", "MODAL_OPEN", "ANIMATING", "ENEMY_TURN", "GAME_OVER"],
  UNIT_SELECTED: ["AWAITING_INPUT", "TARGETING", "MODAL_OPEN", "ANIMATING"],
  TARGETING: ["UNIT_SELECTED", "AWAITING_INPUT", "MODAL_OPEN", "ANIMATING"],
  MODAL_OPEN: ["AWAITING_INPUT", "UNIT_SELECTED", "ANIMATING"],
  ANIMATING: ["AWAITING_INPUT", "ENEMY_TURN", "GAME_OVER"],
  ENEMY_TURN: ["ANIMATING", "AWAITING_INPUT", "GAME_OVER"],
  GAME_OVER: [],
};

// ----------------------------------------------------------------- modals ---
export type ModalState =
  | { kind: "ENGAGEMENT"; attackerId: number; defenderId: number;
      autoWin: boolean;                 // defender frozen && we are solvent
      options: ("FORCE" | "BRIBE" | "CAPTURE")[]; offerMult: number }
  | { kind: "INTERROGATION"; prisonerId: number; moralPreview: { force: number; resource: number } }
  | { kind: "BUILD_PLANT"; unitId: number; tile: Coord; entries: BuildEntry[] }
  | { kind: "GACHA_RESULT"; tier: string; targetId: number }
  | { kind: "GAME_OVER"; winner: Side };

export interface BuildEntry { id: string; label: string; cost: Partial<Record<ResKey, number>>; affordable: boolean; note?: string }

// ---------------------------------------------------------------- warnings ---
export type Severity = "info" | "warn" | "critical";
export interface Warning { id: string; severity: Severity; text: string; unitIds?: number[] }

const RES_LOW_FRACTION = 0.15;          // of starting stock -> "warn"
const MORAL_CAP_THRESHOLD = 30;         // below this, Supply Bases give nothing

// ------------------------------------------------------------- animations ---
export interface Animation { kind: string; durationMs: number; payload: any }
const ANIM_DURATIONS: Record<string, number> = {
  unit_moved: 250, unit_killed: 400, unit_frozen: 600, auto_win: 900,
  build_started: 500, build_finished: 700, mine_detonated: 800,
  gacha_roll: 1200, payday: 1000, force_clash: 600,
};

// ------------------------------------------------------------------ store ---
export interface UiState {
  phase: Phase;
  snap: Snapshot | null;
  startRes: Record<ResKey, number> | null;
  selectedUnitId: number | null;
  reachable: Coord[];
  targets: number[];
  modal: ModalState | null;
  frozenUnitIds: Set<number>;         // drives the ice/chain overlay + greyscale sprite
  constructing: Map<string, number>;  // "x,y" -> turnsLeft (progress bar on tile)
  warnings: Warning[];
  animQueue: Animation[];
  toasts: { id: number; text: string; severity: Severity }[];
}

type Listener = (s: Readonly<UiState>) => void;

export class UiStore {
  private s: UiState = {
    phase: "AWAITING_INPUT", snap: null, startRes: null, selectedUnitId: null,
    reachable: [], targets: [], modal: null, frozenUnitIds: new Set(),
    constructing: new Map(), warnings: [], animQueue: [], toasts: [],
  };
  private listeners = new Set<Listener>();
  private toastId = 0;
  private draining = false;
  private resumePhase: Phase = "AWAITING_INPUT";

  get state(): Readonly<UiState> { return this.s; }
  subscribe(l: Listener): () => void { this.listeners.add(l); return () => this.listeners.delete(l); }
  private emit() { this.listeners.forEach((l) => l(this.s)); }

  // ---- phase ------------------------------------------------------------ //
  private go(next: Phase): boolean {
    if (this.s.phase === next) return true;
    if (!TRANSITIONS[this.s.phase].includes(next)) {
      console.warn(`Illegal phase transition ${this.s.phase} -> ${next}`);
      return false;
    }
    this.s = { ...this.s, phase: next };
    return true;
  }

  // ---- server sync ------------------------------------------------------ //
  /** Single entry point after every backend call. */
  applyServerUpdate(snap: Snapshot, events: GameEvent[]): void {
    const first = this.s.startRes === null;
    this.s = {
      ...this.s,
      snap,
      startRes: first ? { ...snap.me.res } : this.s.startRes,
      frozenUnitIds: new Set(snap.units.filter((u) => u.frozen && u.owner === "player").map((u) => u.id)),
      constructing: new Map(snap.buildings.filter((b) => b.turnsLeft > 0)
        .map((b) => [`${b.pos[0]},${b.pos[1]}`, b.turnsLeft] as [string, number])),
    };
    this.s.warnings = deriveWarnings(this.s);
    this.enqueueAnimations(events);
    this.handleSideEffects(events, snap);
    this.emit();
    void this.drainAnimations();
  }

  private handleSideEffects(events: GameEvent[], snap: Snapshot) {
    for (const e of events) {
      if (e.type === "unit_frozen" && this.s.snap?.units.find((u) => u.id === e.unit)?.owner === "player")
        this.toast("A unit is out of supplies and frozen in place — it is defenceless.", "critical");
      if (e.type === "payday" && e.side === "player" && e.suppressed)
        this.toast(`Payday suppressed by low morale: supply bases capped at ×${e.supply_mult.toFixed(2)}.`, "warn");
      if (e.type === "armory_overflow" && e.side === "player")
        this.toast("Armory full — ammunition shipment partly lost.", "info");
      if (e.type === "gacha_roll")
        this.openModal({ kind: "GACHA_RESULT", tier: e.tier, targetId: e.target });
    }
    if (snap.winner) { this.s.modal = { kind: "GAME_OVER", winner: snap.winner }; this.s.phase = "GAME_OVER"; }
    else if (snap.active === "ai" && this.s.phase !== "ANIMATING") this.s.phase = "ENEMY_TURN";
    else if (snap.active === "player" && this.s.phase === "ENEMY_TURN") this.s.phase = "AWAITING_INPUT";
  }

  // ---- animation queue (input is locked while draining) ----------------- //
  private enqueueAnimations(events: GameEvent[]) {
    const anims = events.filter((e) => e.type in ANIM_DURATIONS)
      // never reveal an enemy-planted mine through an animation
      .filter((e) => !(e.type === "mine_planted" && e.owner !== "player"))
      .map((e) => ({ kind: e.type, durationMs: ANIM_DURATIONS[e.type], payload: e }));
    this.s.animQueue = [...this.s.animQueue, ...anims];
  }

  private async drainAnimations() {
    if (this.draining || this.s.animQueue.length === 0) return;
    this.draining = true;
    this.resumePhase = this.s.phase === "ANIMATING" ? this.resumePhase : this.s.phase;
    if (this.s.phase !== "GAME_OVER") { this.s.phase = "ANIMATING"; this.emit(); }
    while (this.s.animQueue.length) {
      const [next, ...rest] = this.s.animQueue;
      this.s = { ...this.s, animQueue: rest };
      this.emit();                                          // renderer plays `next`
      this.currentAnimation = next;
      await new Promise((r) => setTimeout(r, next.durationMs));
    }
    this.currentAnimation = null;
    this.draining = false;
    if (this.s.phase !== "GAME_OVER") {
      const active = this.s.snap?.active;
      this.s.phase = this.s.modal ? "MODAL_OPEN" : active === "ai" ? "ENEMY_TURN" : "AWAITING_INPUT";
    }
    this.emit();
  }
  currentAnimation: Animation | null = null;

  // ---- selection / targeting -------------------------------------------- //
  selectUnit(id: number | null, reachable: Coord[] = [], targets: number[] = []) {
    if (this.s.phase !== "AWAITING_INPUT" && this.s.phase !== "UNIT_SELECTED") return;
    const u = this.s.snap?.units.find((x) => x.id === id);
    if (u && u.frozen) {                      // frozen units: inspectable but no move range
      reachable = [];
      this.toast("Frozen: no resources. Cannot move. Vulnerable to any solvent enemy.", "warn");
    }
    this.s = { ...this.s, selectedUnitId: id, reachable, targets };
    this.go(id === null ? "AWAITING_INPUT" : "UNIT_SELECTED");
    this.emit();
  }

  beginTargeting() { if (this.go("TARGETING")) this.emit(); }

  // ---- modal controller -------------------------------------------------- //
  openModal(m: ModalState) {
    this.s = { ...this.s, modal: m };
    if (this.s.phase !== "GAME_OVER" && this.s.phase !== "ANIMATING") this.go("MODAL_OPEN");
    this.emit();
  }

  closeModal() {
    this.s = { ...this.s, modal: null };
    if (this.s.phase === "MODAL_OPEN") this.go(this.s.selectedUnitId ? "UNIT_SELECTED" : "AWAITING_INPUT");
    this.emit();
  }

  /** Build the engagement prompt. If the target is frozen and we are solvent,
   *  the Force option is replaced by an instant-win banner (server still decides). */
  openEngagement(attackerId: number, defenderId: number) {
    const snap = this.s.snap!;
    const d = snap.units.find((u) => u.id === defenderId)!;
    const solvent = Object.values(snap.me.res).some((v) => v > 0);
    const autoWin = d.frozen && solvent;
    const options: ("FORCE" | "BRIBE" | "CAPTURE")[] = autoWin ? ["FORCE", "CAPTURE"] : ["FORCE", "BRIBE", "CAPTURE"];
    this.openModal({ kind: "ENGAGEMENT", attackerId, defenderId, autoWin, options, offerMult: 1 });
  }

  setOffer(mult: number) {
    const m = this.s.modal;
    if (m?.kind === "ENGAGEMENT") { this.s = { ...this.s, modal: { ...m, offerMult: Math.min(3, Math.max(1, mult)) } }; this.emit(); }
  }

  openInterrogation(prisonerId: number) {
    this.openModal({ kind: "INTERROGATION", prisonerId, moralPreview: { force: -8, resource: -0.5 } });
  }

  openBuildPlant(unitId: number, tile: Coord) {
    const res = this.s.snap!.me.res;
    const defs: [string, string, Partial<Record<ResKey, number>>, string?][] = [
      ["wall", "Wall", { materials: 10 }],
      ["supply_base", "Supply Base", { materials: 40, money: 30 }, "+20% payday (capped by morale)"],
      ["barracks", "Barracks", { materials: 35, money: 20 }, "More infantry, LOWERS armory cap"],
      ["plant_mine", "Plant Bomb", { bombs: 1, materials: 2 }, "Hidden. Kills friend AND foe when detonated"],
    ];
    const entries = defs.map(([id, label, cost, note]) => ({
      id, label, cost, note,
      affordable: Object.entries(cost).every(([k, v]) => res[k as ResKey] >= (v as number)),
    }));
    this.openModal({ kind: "BUILD_PLANT", unitId, tile, entries });
  }

  // ---- toasts ------------------------------------------------------------ //
  toast(text: string, severity: Severity = "info", ttlMs = 4000) {
    const id = ++this.toastId;
    this.s = { ...this.s, toasts: [...this.s.toasts, { id, text, severity }] };
    this.emit();
    setTimeout(() => { this.s = { ...this.s, toasts: this.s.toasts.filter((t) => t.id !== id) }; this.emit(); }, ttlMs);
  }
}

// -------------------------------------------------------- derived selectors ---
export function deriveWarnings(s: UiState): Warning[] {
  const out: Warning[] = [];
  if (!s.snap || !s.startRes) return out;
  const { me, paydayIn } = s.snap;
  (Object.keys(me.res) as ResKey[]).forEach((k) => {
    const v = me.res[k], start = s.startRes![k];
    if (v <= 0.001) out.push({ id: `res0-${k}`, severity: "critical", text: `${k.toUpperCase()} depleted — dependent units freeze.` });
    else if (v / start <= RES_LOW_FRACTION) out.push({ id: `reslow-${k}`, severity: "warn", text: `${k} running low (${v.toFixed(0)}).` });
  });
  if (me.moral < MORAL_CAP_THRESHOLD)
    out.push({ id: "moral-cap", severity: "critical",
      text: `Morale ${me.moral.toFixed(0)}: supply-base bonus capped at ×${me.supplyCap.toFixed(1)}, income ×${me.incomeMult.toFixed(2)}.` });
  else if (me.consumptionMult > 1.1)
    out.push({ id: "moral-burn", severity: "warn", text: `Low morale: units burn ×${me.consumptionMult.toFixed(2)} resources.` });
  if (s.frozenUnitIds.size)
    out.push({ id: "frozen", severity: "critical", unitIds: [...s.frozenUnitIds],
      text: `${s.frozenUnitIds.size} unit(s) frozen — any solvent enemy wins instantly against them.` });
  if (paydayIn === 1) out.push({ id: "payday", severity: "info", text: "Payday next round." });
  return out;
}

/** HUD view-model: everything the panels need, pre-formatted. */
export function selectHud(s: Readonly<UiState>) {
  const snap = s.snap;
  if (!snap) return null;
  const armoryUsed = snap.me.res.bullets + snap.me.res.bombs;
  return {
    resources: (Object.keys(snap.me.res) as ResKey[]).map((k) => ({
      key: k, value: Math.floor(snap.me.res[k]),
      status: snap.me.res[k] <= 0.001 ? "empty" : s.startRes && snap.me.res[k] / s.startRes[k] <= RES_LOW_FRACTION ? "low" : "ok",
    })),
    armory: { used: Math.floor(armoryUsed), cap: Math.floor(snap.me.armoryCap), full: armoryUsed >= snap.me.armoryCap },
    paydayCountdown: snap.paydayIn,
    moral: { value: snap.me.moral, enemy: snap.enemyMoral, incomeMult: snap.me.incomeMult,
             consumptionMult: snap.me.consumptionMult, supplyCap: snap.me.supplyCap,
             band: snap.me.moral < 25 ? "broken" : snap.me.moral < 50 ? "shaken" : snap.me.moral < 75 ? "steady" : "high" },
    difficulty: snap.difficulty,
    round: snap.round,
    canAct: s.phase === "AWAITING_INPUT" || s.phase === "UNIT_SELECTED" || s.phase === "TARGETING",
  };
}

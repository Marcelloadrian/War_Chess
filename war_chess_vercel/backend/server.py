"""server.py - STATELESS, SIGNED FastAPI app (Vercel-compatible).
The browser stores the serialised game state + an HMAC signature and sends both with every
action, so hand-edited saves are rejected. Set WARCHESS_SECRET in Vercel env vars (any long
random string); the default only suits local dev.
Endpoints: GET /api/new, POST /api/load, /api/action, /api/preview, /api/log."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import ai
import combat as C
import scenarios
from engine import BuildingType, Side, dump_state, load_state

app = FastAPI(title="War Chess")
ME = Side.PLAYER
SECRET = os.environ.get("WARCHESS_SECRET", "dev-only-secret-change-me").encode()


def _norm(x):
    """JS turns 60.0 into 60; normalise integral floats so signatures survive the round trip."""
    if isinstance(x, float) and x.is_integer():
        return int(x)
    if isinstance(x, dict):
        return {k: _norm(v) for k, v in x.items()}
    if isinstance(x, list):
        return [_norm(v) for v in x]
    return x


def _sign(state: dict) -> str:
    raw = json.dumps(_norm(state), sort_keys=True, separators=(",", ":")).encode()
    return hmac.new(SECRET, raw, hashlib.sha256).hexdigest()


def _view(s, result=None):
    snap = s.snapshot(ME)
    snap["prisoners"] = [{"id": u.id, "type": u.type.value, "rank": u.rank}
                         for u, captor in s.prisoners.values() if captor is ME]
    st = dump_state(s)
    out = {"state": st, "sig": _sign(st), "snapshot": snap, "events": s.drain_events(),
           "result": None if result is None else {"ok": result.ok, "kind": result.kind, "detail": result.detail}}
    return json.loads(json.dumps(out, default=str))


def _restore(d: dict, sig: str):
    if not hmac.compare_digest(_sign(d), sig or ""):
        raise HTTPException(400, "tampered or unsigned save")
    try:
        s = load_state(d)
    except Exception:
        raise HTTPException(400, "Corrupt or incompatible save state")
    C.install(s)
    return s


class Signed(BaseModel):
    state: dict
    sig: str


class Action(Signed):
    type: str
    unit: int | None = Field(None, ge=0, le=10_000)
    target: int | None = Field(None, ge=0, le=10_000)
    x: int | None = Field(None, ge=0, le=99)
    y: int | None = Field(None, ge=0, le=99)
    btype: str | None = None
    offer: float = Field(1.0, ge=1.0, le=3.0)
    capture: bool = False
    mode: str | None = Field(None, pattern="^(force|resource)$")


class Preview(Signed):
    unit: int = Field(ge=0, le=10_000)
    target: int = Field(ge=0, le=10_000)


class ClientLog(BaseModel):
    msg: str = Field(..., max_length=500)
    where: str = Field("", max_length=200)


@app.get("/api/new")
def new_game(difficulty: str = "medium", seed: int = 7, scenario: str = "skirmish", vets: str = ""):
    ranks = tuple(int(x) for x in vets.split(",") if x.strip().isdigit())[:8]
    try:
        s = scenarios.build(scenario, difficulty, seed, ranks)
    except (KeyError, ValueError):
        raise HTTPException(400, "unknown scenario or difficulty")
    C.install(s)
    return _view(s)


@app.post("/api/load")
def load(body: Signed):
    return _view(_restore(body.state, body.sig))


@app.post("/api/preview")
def preview(p: Preview):
    s = _restore(p.state, p.sig)
    if p.unit not in s.units or p.target not in s.units or s.units[p.unit].owner is not ME:
        return {"ok": False, "reason": "invalid units"}
    return json.loads(json.dumps(C.preview_engagement(s, p.unit, p.target), default=str))


@app.post("/api/action")
def action(a: Action):
    s = _restore(a.state, a.sig)
    if s.winner or s.active is not ME:
        return _view(s, C.Outcome(False, "not_your_turn"))
    t, pos, r = a.type, ((a.x, a.y) if a.x is not None and a.y is not None else None), None
    needs_pos = {"move", "bomb", "detonate", "build", "defuse", "unload"}
    try:
        if t in needs_pos and pos is None:
            raise ValueError("x,y required")
        if a.unit is not None and a.unit in s.units and s.units[a.unit].owner is not ME:
            raise ValueError("not your unit")
        if t == "move":
            u = s.units[a.unit]
            p = ai.find_path(s, u, pos)
            m = s.move_unit(u.id, p) if p else {"moved": 0, "stopped": "no_path"}
            r = C.Outcome(m["moved"] > 0, "move", m)
        elif t == "attack":
            r = C.resolve_force_attack(s, a.unit, a.target, a.capture)
        elif t == "capture_unit":
            r = C.try_single_capture(s, a.unit, a.target)
        elif t == "bribe":
            r = C.gacha_bribe(s, ME, a.target, a.offer)
        elif t == "bomb":
            r = C.drop_bomb(s, ME, pos)
        elif t == "plant":
            r = C.plant_mine(s, a.unit)
        elif t == "detonate":
            r = C.detonate_mine(s, ME, pos)
        elif t == "defuse":
            r = C.defuse_mine(s, a.unit, pos)
        elif t == "load":
            r = C.Outcome(s.load_unit(a.unit, a.target), "load")
        elif t == "unload":
            r = C.Outcome(s.unload_unit(a.unit, pos), "unload")
        elif t == "build":
            r = C.Outcome(s.build(a.unit, BuildingType(a.btype), pos), "build")
        elif t == "capture_node":
            r = C.Outcome(s.capture_building(a.unit, s.units[a.unit].pos), "capture_node")
        elif t == "surround":
            r = C.capture_surrounded(s, ME)
        elif t == "interrogate":
            r = (C.force_interrogate if a.mode == "force" else C.resource_interrogate)(s, ME, a.target)
        elif t == "release":
            r = C.release_prisoner(s, ME, a.target)
        elif t == "end_turn":
            s.end_turn()
            ai.play_ai_turn(s)
            r = C.Outcome(True, "end_turn")
        else:
            r = C.Outcome(False, "unknown_action")
    except (KeyError, ValueError, TypeError) as e:
        r = C.Outcome(False, "bad_request", {"error": str(e)})
    s.evaluate_bankruptcy(ME)
    s.check_victory()
    return _view(s, r)


@app.post("/api/log")
def client_log(body: ClientLog):
    print(f"[client-error] {body.msg} @ {body.where}")     # appears in Vercel function logs
    return {"ok": True}


# Local dev only: on Vercel, /public is served by the CDN before reaching this app.
_public = Path(__file__).resolve().parent.parent / "public"
if _public.exists():
    app.mount("/", StaticFiles(directory=_public, html=True), name="public")

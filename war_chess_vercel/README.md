# War Chess - VERCEL package
Stateless FastAPI as a Vercel Python function (`api/index.py`), static client in `public/`.

## Deploy (GitHub -> Vercel)
1. Push this folder to a GitHub repo (repo root = this folder). Add a .gitignore with `.venv/`, `__pycache__/`, `.vercel/`.
2. Vercel dashboard -> Add New -> Project -> import the repo. Framework: Other. Leave build/output empty.
3. Settings -> Environment Variables: add `WARCHESS_SECRET` = long random string (all environments).
4. Deploy. Pushes to `main` go live; other branches get preview URLs.
CLI alternative: `npm i -g vercel && vercel --prod`.

## First checks after deploy
- `https://YOUR-APP.vercel.app/api/new` returns JSON.
- Play a few turns: if every action says "tampered or unsigned save", the secret is missing/mismatched.
- Vercel function logs show `[client-error]` lines from browsers.

## Local run
    python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
    pip install -r requirements-dev.txt
    uvicorn api.index:app --reload --port 8000            # http://localhost:8000
## Test / balance
    pytest tests -q
    python tools/simulate.py 6      # difficulty + AI commander matrix (use 20 for reliable numbers)
## Game notes
Fog of war, scouts, naval pincers, fuzzy engagement intel, signed saves, no undo by design. See GDD.md.
Saves live in the browser (localStorage) and are HMAC-signed; set WARCHESS_SECRET in production.
Old saves are discarded automatically when the save format changes.

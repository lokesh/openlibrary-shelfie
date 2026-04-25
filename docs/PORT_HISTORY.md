# Port history: extracting shelfie from `internetarchive/openlibrary`

> **Historical document.** This is the original port plan from when shelfie was extracted out of `scripts/shelfie.py` in the OL repo. Preserved for context on early decisions ("port, don't refactor", network-name assumption, OLClient fidelity). It is **not** current direction — see `README.md` for that.

Target repo: `github.com/lokeshdhakar/openlibrary-shelfie`. Keep the change minimal — port, don't refactor.

## Repo layout

```
openlibrary-shelfie/
├── README.md
├── pyproject.toml          # deps: requests; dev: pytest
├── .python-version         # 3.12
├── .gitignore
├── Dockerfile              # python:3.12-slim, pip install -e .
├── docker-compose.yml      # sidecar joining openlibrary_default
├── shelfie/
│   ├── __init__.py
│   ├── __main__.py         # python -m shelfie → cli.main()
│   ├── cli.py              # current shelfie.py, minus _init_path & OpenLibrary
│   └── client.py           # new OLClient (plain requests)
├── seed_data/              # was scripts/dev_data/
│   ├── books.json
│   ├── list_names.json
│   └── subjects.json
└── tests/
    └── test_shelfie.py     # ported, no _init_path hack
```

## Phase 1 — Scaffolding

1. Create empty repo on GitHub.
2. `uv init`, add `requests` runtime dep, `pytest` dev dep, Python 3.12.
3. `.gitignore` (venv, `__pycache__`, `.pytest_cache`).
4. Stub `README.md`.

## Phase 2 — Sidecar runtime

1. **Verify OL's network name first** — run `docker network ls` while OL is up; expect `openlibrary_default`. If different, adjust everywhere below.
2. `Dockerfile`:
   ```dockerfile
   FROM python:3.12-slim
   WORKDIR /app
   COPY pyproject.toml ./
   RUN pip install -e .
   COPY . .
   ENTRYPOINT ["python", "-m", "shelfie"]
   ```
3. `docker-compose.yml`:
   ```yaml
   services:
     shelfie:
       build: .
       networks: [openlibrary_default]
       volumes: [".:/app"]
       stdin_open: true
       tty: true
   networks:
     openlibrary_default:
       external: true
   ```
   (`stdin_open` + `tty` are needed for the interactive menu.)

## Phase 3 — Port the source (the meaningful work)

1. Copy `scripts/shelfie.py` → `shelfie/cli.py`. Strip:
   - `import _init_path`
   - `from openlibrary.api import OLError, OpenLibrary`
2. Write `shelfie/client.py`. Read OL's `openlibrary/api.py` first so the new `OLClient` mirrors the on-the-wire contract exactly (cookie shape, login form encoding, `/api/import` payload format, `/query.json` response shape). Only 5 methods + 1 exception:
   - `class OLError(Exception)`
   - `OLClient.__init__(base_url)`
   - `.login(email, password)` — POST `/account/login`, store session cookie, expose `.cookie`
   - `.get(key)` — GET `<base>/<key>.json` → dict, raise `OLError` on 4xx/5xx
   - `.query(type, limit)` — GET `/query.json` → list of keys
   - `.import_data(json_str)` — POST `/api/import` (payload + headers from OL's existing client)
   - `._request(path, method, data=None, params=None, headers=None)` — generic, returns the `requests.Response`
3. In `cli.py`, swap import: `from .client import OLClient, OLError`. Keep all `connect()` defaults — sidecar means `web:8080`, `infobase:7000`, `solr:8983` still resolve.
4. Move `scripts/dev_data/` → `seed_data/`, update `DEV_DATA_DIR`.
5. Add `shelfie/__main__.py`:
   ```python
   from .cli import main
   if __name__ == "__main__":
       main()
   ```

## Phase 4 — Tests

1. Copy `scripts/tests/test_shelfie.py` → `tests/test_shelfie.py`.
2. Drop the `sys.modules["_init_path"] = MagicMock()` hack.
3. Rewrite imports: `from shelfie.cli import …`.
4. Rewrite all `patch("scripts.shelfie.X")` → `patch("shelfie.cli.X")`.
5. `pytest` clean.

## Phase 5 — Live smoke against running OL

With `docker compose up` running in the OL clone, from the shelfie clone run **in this order**:

1. `docker compose build`
2. `docker compose run --rm shelfie list-users` — read-only sanity check (network + auth-less reads work).
3. `docker compose run --rm shelfie add-books --count 3 --source seed` — exercises login + `/api/import`.
4. `docker compose run --rm shelfie smoke-test` — full regression battery.
5. `docker compose run --rm shelfie` (no args) — verify interactive menu renders.

If any step fails, the most likely culprit is an `OLClient` shape mismatch — diff the request against what `openlibrary.api.OpenLibrary` would have sent.

## Phase 6 — README

Sections: prereq (OL running), setup (`docker compose build`), running (`docker compose run --rm shelfie <cmd>`), command list, troubleshooting (network-name mismatch).

## Phase 7 — Ship

1. Push, tag `v0.1.0`.
2. Light announce to OL contributors.
3. **Don't touch the OL repo** — `scripts/shelfie.py` stays for now.

## Risks to watch

- **Network name** — single biggest assumption; verify Phase 2 step 1 before writing the compose file.
- **`OLClient` fidelity** — login cookie handling and `/api/import` payload are the two places where a quiet mismatch will silently break things. Read OL's `api.py` carefully.
- **Interactive TTY** — confirm `docker compose run --rm shelfie` (no args) gives a usable menu; may need `-it` if compose doesn't grant it via `tty: true` alone.

Estimated effort: ~half a day if `OLClient` goes smoothly, full day if the on-the-wire contract has surprises.

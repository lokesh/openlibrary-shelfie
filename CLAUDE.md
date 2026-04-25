# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Shelfie is a sidecar CLI that joins a running Open Library Docker stack and populates its dev DB with realistic data (real titles, authors, covers, subjects fetched from production openlibrary.org). It was originally `scripts/shelfie.py` in `internetarchive/openlibrary` and was extracted here so it can ship independently — see `docs/PORT_HISTORY.md` for the original port plan and the constraints that shaped the current shape (in particular, "port, don't refactor").

## Common commands

Local dev (no OL stack needed for unit tests — they mock network/infobase):

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest                                    # full test suite
.venv/bin/pytest tests/test_shelfie.py::TestIsLowQuality -v   # single class
.venv/bin/pytest -k merge_save                      # single test by name
```

Against a running OL stack (in the OL clone, `docker compose up` first):

```bash
docker compose build
docker compose run --rm shelfie                     # interactive menu
docker compose run --rm shelfie <subcommand> --help # per-command flags
docker compose run --rm shelfie smoke-test          # run after editing cli.py
```

The full subcommand list lives in `README.md` and in `build_parser()` in `shelfie/cli.py`.

## Architecture

Three services on the OL Docker network, each with a different role:

- **`web:8080`** — main OL web app. Authenticated writes (imports, lists, ratings) go through `OLClient` in `shelfie/client.py`, a deliberately minimal port of `openlibrary.api.OpenLibrary`. The wire contract (login form, cookie shape, `/api/import` payload, `/query.json` shape) must match OL exactly — if you change `client.py`, diff the request against what `openlibrary.api` would send.
- **`infobase:7000`** — low-level datastore, hit directly via `infobase_save()` for writes that bypass web-app save bugs (usergroup membership, `/save_many` for subjects/series).
- **`solr:8983`** — search index, queried for stats, coverage checks, and existing work keys.

Two interfaces, one codebase. Every menu action and every argparse subcommand maps to a `cmd_*` function in `cli.py`. Menu helpers are `_menu_*` wrappers that prompt for inputs then call the same `cmd_*`. When adding a feature, add the `cmd_*`, then wire both: a subparser in `build_parser()` + a dispatch arm in `main()`, and a menu entry in `MENU_OPTIONS` + the `_MENU_DISPATCH` table.

### The merge-save invariant (don't break this)

`infobase_save` / `/save_many` **replaces** the stored revision with exactly the JSON you POST — fields you omit are lost. For partial updates always go through `_merge_save()` in `cli.py`, which fetches the current doc, overlays the patch, and strips revision metadata. PR #12157 review caught a bug where direct saves wiped titles/authors; `cmd_smoke_test` has explicit regression checks for it (search for "bug #5").

### Data sourcing

Most commands fetch live from openlibrary.org and re-import into local. When production is unreachable they fall back to bundled JSON in `seed_data/`. For `add-books` the fallback is opt-in via `--source seed`. Quality filter (`_is_low_quality`) rejects study guides and self-published junk before import — keep filtering before insert, not after.

### Imports run in parallel

`/api/import` is one-book-per-call, so concurrency (`IMPORT_WORKERS = 8`, `ThreadPoolExecutor`) is the only speed lever. Per-record errors are surfaced inline (first 3 failures printed during the run).

## UI conventions

All styled output, prompts, spinners, and progress bars live in `shelfie/ui.py` (rich + questionary). `cli.py` should only import from `ui` for presentation — don't sprinkle `print()` / `console.print()` calls directly. `UserExit` propagates Ctrl-C / Esc out of prompts; the menu loop catches it.

## Defaults that matter

- Login: `openlibrary@example.com` / `admin123` (the dev DB bootstrap user)
- Targets: `web:8080`, `infobase:7000/openlibrary`, `solr:8983` (resolvable only inside the `openlibrary_webnet` Docker network)
- Network name in `docker-compose.yml` must match OL's actual network — verify with `docker network ls | grep openlibrary` (older setups use `openlibrary_default` or `openlibrary_dbnet`)

## After editing `cli.py`

Run `cmd_smoke_test` (`docker compose run --rm shelfie smoke-test`) against a fresh dev DB. It's a regression battery for the specific bugs PR #12157 review caught, not a generic test — each check is named with the bug number it guards.

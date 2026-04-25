# shelfie

Interactive dev tool that populates a local Open Library instance with realistic data so contributors can work on features without needing a production database dump.

Shelfie runs as a sidecar container that joins the OL Docker network and talks to the `web`, `infobase`, and `solr` services directly. Most commands fetch real data from `openlibrary.org` (search results, public lists, series metadata) and re-import it into your local instance, so the dev DB looks like a real library — real titles, authors, covers, subjects, and ISBNs. Bundled JSON seed files cover the case where production is unreachable.

## Prerequisites

- Docker / Docker Compose
- A running [Open Library](https://github.com/internetarchive/openlibrary) dev stack (`docker compose up` in your OL clone)

Shelfie joins OL's Docker network as `external`. The default network name is `openlibrary_webnet` — verify with `docker network ls | grep openlibrary` and adjust `docker-compose.yml` if your OL stack uses a different name.

## Setup

```bash
git clone https://github.com/lokeshdhakar/openlibrary-shelfie
cd openlibrary-shelfie
docker compose build
```

## Usage

### Interactive menu

```bash
docker compose run --rm shelfie
```

A menu walks you through every operation.

### Subcommands

```bash
# One-shot: a rich seeded DB in a single command
docker compose run --rm shelfie populate-all

# Targeted commands
docker compose run --rm shelfie add-books --count 100
docker compose run --rm shelfie add-books --count 10 --source seed   # offline
docker compose run --rm shelfie generate-lists --count 5
docker compose run --rm shelfie seed-reading-log --count 20 --username admin
docker compose run --rm shelfie seed-series --count 3
docker compose run --rm shelfie seed-ratings --count 30 --username admin
docker compose run --rm shelfie populate-subjects
docker compose run --rm shelfie populate-covers --limit 100

# Inspection
docker compose run --rm shelfie list-users
docker compose run --rm shelfie stats
docker compose run --rm shelfie manage-solr

# Admin
docker compose run --rm shelfie set-role --username openlibrary --role admin
docker compose run --rm shelfie reset

# Verification
docker compose run --rm shelfie smoke-test
```

Run `docker compose run --rm shelfie <command> --help` for per-command flags.

## Commands

| Command | What it does |
|---|---|
| `populate-all` | Run everything below in sequence — books, subjects, lists, ratings, reading log, series. |
| `add-books` | Import books into local OL. `--source production` (default) fetches from openlibrary.org; `--source seed` uses bundled JSON. |
| `generate-lists` | Create reading lists by re-importing real public lists from openlibrary.org. |
| `seed-reading-log` | Add books to a user's want/reading/read shelves. |
| `seed-series` | Create series (Harry Potter, Dune, etc.) with ordered work links. |
| `seed-ratings` | Post ratings as a user across existing works. |
| `populate-subjects` | Backfill missing `subjects` on works using title-keyword matching. |
| `populate-covers` | Look up covers on openlibrary.org for works missing them. |
| `set-role` | Add/remove a user from a usergroup (admin, librarians, etc.). |
| `list-users` | Table of users with emails, roles, and dev-default password hints. |
| `stats` | Cross-reference infobase and Solr counts; surface coverage and sync drift. |
| `manage-solr` | Check Solr index status; reindex specific work keys. |
| `reset` | Mark shelfie-imported books, lists, or series as deleted. Has a "nuclear" option that prints the docker volume teardown command. |
| `smoke-test` | Regression battery for bugs from PR #12157 review — run after editing `cli.py`. |

## Defaults

- Login: `openlibrary@example.com` / `admin123` (the dev DB bootstrap user)
- Targets: `http://web:8080`, `http://infobase:7000/openlibrary`, `http://solr:8983`

Override per-command with `--url`, `--email`, `--password`.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Tests cover pure helpers (record building, quality filtering, password lookup) and the merge-save logic that prevents partial saves from clobbering existing fields. They mock out network and infobase, so no running OL is required.

## Troubleshooting

**`network openlibrary_webnet declared as external, but could not be found`**
Your OL stack uses a different network name. Run `docker network ls | grep openlibrary` and update the network name in `docker-compose.yml`. Older or customized OL setups have used `openlibrary_default` or `openlibrary_dbnet`.

**`Could not reach openlibrary.org`**
Most commands fall back to bundled `seed_data/` when production is unreachable. For `add-books`, pass `--source seed` explicitly to skip the production fetch.

**`Logged in as ...` followed by `400 Bad Request` on imports**
Usually a transient infobase race (`version_thing_id_fkey`). Re-run; shelfie's per-record import logs which titles failed.

**Books imported but not appearing in search**
Solr re-indexes asynchronously via `solr-updater`. Wait a moment, or use `manage-solr` → "Reindex specific works" to nudge it.

## Background

Originally `scripts/shelfie.py` in the [openlibrary/openlibrary](https://github.com/internetarchive/openlibrary) repo. Extracted here so it can ship independently of the OL release cycle and run in any OL contributor's environment without needing a checkout of the main repo.

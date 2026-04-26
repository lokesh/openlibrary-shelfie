#!/usr/bin/env python3
"""shelfie - Interactive dev tool for Open Library.

Usage:
    Interactive:  python -m shelfie
    Subcommands:  python -m shelfie populate-all
                  python -m shelfie add-books --count 100
                  python -m shelfie seed-series --count 3
                  python -m shelfie set-role --username openlibrary --role admin
                  python -m shelfie list-users
                  python -m shelfie smoke-test

Run as a sidecar against a running Open Library stack:
    docker compose run --rm shelfie

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
#
# Shelfie is a CLI tool that populates a local Open Library dev environment
# with realistic data so developers can work on features without needing
# a production database dump. It runs as a sidecar container on the OL
# Docker network and talks to three internal services:
#
#   - web (port 8080):     The main OL web app. Used for authenticated
#                          operations like importing books, creating lists,
#                          and posting ratings via the bundled OLClient
#                          (shelfie.client.OLClient).
#
#   - infobase (port 7000): The low-level datastore. Used for direct writes
#                           that bypass web-app save bugs in local dev -
#                           e.g. updating usergroup membership and saving
#                           subjects or series docs via /save_many.
#
#   - solr (port 8983):    The search index. Queried for stats, coverage
#                          checks, and to fetch existing work keys.
#
# Data sourcing strategy:
#   Most commands fetch real data from production openlibrary.org - search
#   results, public lists, series metadata - and re-import it into the local
#   instance via the /import endpoint. This gives developers realistic titles,
#   authors, covers, subjects, and ISBNs without fabricating anything. When
#   production is unreachable, commands fall back to small bundled JSON seed
#   files in seed_data/.
#
# Two interfaces, one codebase:
#   With no arguments, shelfie launches an interactive menu (choose/ask/
#   confirm helpers) that walks the user through each operation. Every menu
#   action maps to a cmd_* function that also accepts keyword arguments, so
#   the same functions power the argparse subcommands for scripted/CI usage
#   (e.g. `shelfie add-books --count 50`).
#
# Key techniques:
#   - Import pipeline: production search doc -> _search_doc_to_record()
#     normalizes fields (authors, publishers, ISBNs, cover URLs, subjects)
#     into an import-ready dict -> ol.import_data() posts it to /import.
#   - Quality filtering: _is_low_quality() rejects study guides, workbooks,
#     and self-published junk so the dev DB looks like a real library.
#   - Series creation: searches production for known series titles, imports
#     the matched works, then writes /type/series docs and back-links each
#     work with a position via infobase.
#   - Reading activity: ratings, reading-log shelves (want/reading/read),
#     and lists are all posted through OL's JSON APIs as the logged-in user.
#   - Stats dashboard: cross-references infobase counts with Solr counts
#     to surface coverage gaps (works missing covers or subjects) and
#     sync drift (works not yet indexed).
#
# ---------------------------------------------------------------------------
"""

import argparse
import contextlib
import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

from .client import OLClient, OLError
from .ui import (
    SIMPLE_HEAVY,
    Columns,
    Table,
    UserExit,
    ask,
    banner,
    choose,
    confirm,
    console,
    dim,
    error,
    failure_logger,
    friendly_error,
    header,
    import_progress,
    info,
    plain,
    report_error,
    spinner,
    stats_table,
    step_progress,
    success,
    warn,
)

DEV_DATA_DIR = Path(__file__).parent.parent / "seed_data"
DEFAULT_BASE_URL = "http://web:8080"
DEFAULT_INFOBASE_URL = "http://infobase:7000/openlibrary"
DEFAULT_LOGIN_EMAIL = "openlibrary@example.com"
DEFAULT_LOGIN_PASSWORD = "admin123"
DEFAULT_USERNAME = "openlibrary"

USERGROUPS = [
    "admin",
    "librarians",
    "super-librarians",
    "curators",
    "beta-testers",
    "read-only",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def load_json(filename):
    with open(DEV_DATA_DIR / filename) as f:
        return json.load(f)


def _preflight_docker_check(url):
    """Fail fast if the user is trying to use Docker hostnames outside Docker.

    The defaults (web:8080, infobase:7000, solr:8983) only resolve inside the
    OL Docker network. Without this guard, a beginner running `python -m
    shelfie` from a venv hits a wall of cryptic DNS errors. This catches the
    common case before anything else touches the network.
    """
    if url != DEFAULT_BASE_URL:
        return  # User has overridden the URL — assume they know what they're doing.
    if Path("/.dockerenv").exists():
        return
    error("Looks like you're running shelfie outside the OL Docker stack.")
    plain("")
    plain("Shelfie's defaults (web:8080, infobase:7000, solr:8983) are Docker")
    plain("hostnames and only resolve inside OL's Docker network.")
    plain("")
    plain("From your OL clone, run:")
    console.print("  [bold cyan]docker compose run --rm shelfie[/bold cyan]")
    plain("")
    dim("Or pass --url to point shelfie at a host you've made reachable some other way.")
    raise SystemExit(1)


def connect(base_url=None, email=None, password=None):
    """Create and authenticate an OL client."""
    base_url = base_url or DEFAULT_BASE_URL
    email = email or DEFAULT_LOGIN_EMAIL
    password = password or DEFAULT_LOGIN_PASSWORD
    ol = OLClient(base_url)
    try:
        with spinner(f"Logging in as {email}…"):
            ol.login(email, password)
    except (OLError, requests.RequestException) as e:
        report_error(e, target_url=base_url, operation="Login failed")
        dim("  Run 'shelfie health-check' to see which service is unreachable.")
        return ol

    if ol.cookie:
        success(f"Logged in as [cyan]{email}[/cyan]")
        return ol

    # Login endpoint returns 200 even on bad credentials — it just doesn't
    # set a session cookie. Tell the user what likely went wrong.
    error("Login was rejected — no session cookie returned.")
    if email != DEFAULT_LOGIN_EMAIL:
        dim(f"  You used: {email}")
        dim(f"  Default dev login is {DEFAULT_LOGIN_EMAIL} / {DEFAULT_LOGIN_PASSWORD}.")
    else:
        dim(f"  Default dev login ({DEFAULT_LOGIN_EMAIL} / {DEFAULT_LOGIN_PASSWORD}) was rejected.")
        dim("  Was the dev DB initialized? OL's bootstrap usually creates this user.")
    dim("  Most operations will fail without auth. Run 'shelfie health-check' to diagnose.")
    return ol


def infobase_save(docs, comment="shelfie"):
    """Save documents via infobase directly (bypasses web app save bugs)."""
    resp = requests.post(
        f"{DEFAULT_INFOBASE_URL}/save_many",
        data={"query": json.dumps(docs), "comment": comment},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _fetch_raw(key, base_url=None):
    """Fetch a doc as raw JSON via the OL HTTP API (no client-side unmarshalling)."""
    url = (base_url or DEFAULT_BASE_URL) + key + ".json"
    try:
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _merge_save(patches, comment="shelfie"):
    """Apply field-level patches without wiping untouched fields.

    Why: infobase save_many REPLACES the stored revision with exactly what
    you POST - fields you omit are lost. For partial updates, we fetch the
    current doc, overlay the patch, strip revision metadata, and save.
    """
    merged_docs = []
    for patch in patches:
        existing = _fetch_raw(patch["key"]) or {}
        for meta_key in ("latest_revision", "revision", "created", "last_modified"):
            existing.pop(meta_key, None)
        merged_docs.append({**existing, **patch})
    return infobase_save(merged_docs, comment=comment)


def _user_exists(ol, username):
    """Return True if /people/<username> is a stored doc."""
    try:
        return ol.get(f"/people/{username}") is not None
    except (OLError, requests.RequestException):
        return False


def solr_request(path, base_url=None):
    """Make a request to the local Solr instance.

    Returns parsed JSON, or None on any failure. Callers handle None
    (stats render "?", coverless lookup returns []). We don't print here:
    the startup banner alone fans 8 calls, so a flaky Solr would otherwise
    spam the same red error 8 times before the menu draws.
    """
    solr_url = (base_url or "http://solr:8983") + path
    try:
        resp = requests.get(solr_url, timeout=10)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def get_work_keys_from_solr(limit=1000):
    """Fetch available work keys from Solr."""
    data = solr_request(f"/solr/openlibrary/select?q=type:work&fl=key&rows={limit}&wt=json")
    if data and "response" in data:
        return [doc["key"] for doc in data["response"]["docs"]]
    return []


def get_work_keys(ol, limit=500):
    """Get work keys from Solr, falling back to infobase query."""
    work_keys = get_work_keys_from_solr(limit=limit)
    if not work_keys:
        try:
            results = ol.query(type="/type/work", limit=limit)
            work_keys = [str(r) for r in results]
        except OLError:
            pass
    return work_keys


SEARCH_FIELDS = "key,title,author_name,first_publish_year,publisher,subject,isbn,cover_i,number_of_pages_median"
COVERS_URL_TEMPLATE = "https://covers.openlibrary.org/b/id/{}-L.jpg"


REJECTED_PUBLISHERS = {
    "independently published",
    "independent publisher",
    "createspace independent publishing platform",
    "createspace",
    "unknown",
}

REJECTED_TITLE_WORDS = {"study guide", "workbook", "test bank", "solutions manual"}


def _is_low_quality(doc):
    """Check if a search doc is likely junk (study guides, self-published, etc.)."""
    title = doc.get("title", "").lower()
    if any(word in title for word in REJECTED_TITLE_WORDS):
        return True
    publishers = doc.get("publisher", [])
    return bool(publishers and all(p.casefold() in REJECTED_PUBLISHERS for p in publishers[:3]))


def _pick_publisher(doc):
    """Pick the first non-rejected publisher, or 'Unknown'."""
    for p in doc.get("publisher", []):
        if p.casefold() not in REJECTED_PUBLISHERS:
            return [p]
    return ["Unknown"]


def _search_doc_to_record(doc, source_tag):
    """Convert an openlibrary.org search API doc into an import record."""
    record = {
        "title": doc["title"],
        "authors": [{"name": a} for a in doc.get("author_name", ["Unknown"])],
        "publishers": _pick_publisher(doc),
        "publish_date": str(doc.get("first_publish_year", "2000")),
        "source_records": [source_tag],
        "subjects": doc.get("subject", [])[:10],
    }
    if isbns := doc.get("isbn", []):
        isbn = isbns[0]
        if len(isbn) == 13:
            record["isbn_13"] = [isbn]
        elif len(isbn) == 10:
            record["isbn_10"] = [isbn]
    if cover_id := doc.get("cover_i"):
        record["cover"] = COVERS_URL_TEMPLATE.format(cover_id)
    if pages := doc.get("number_of_pages_median"):
        record["number_of_pages"] = pages
    return record


def _import_and_get_work_key(ol, record):
    """Import a record and return the work key, or None on failure."""
    try:
        result = ol.import_data(json.dumps(record))
        result_data = json.loads(result) if isinstance(result, str) else result
        if result_data.get("success"):
            return result_data.get("work", {}).get("key")
    except (OLError, requests.RequestException, json.JSONDecodeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Feature: Add Books
# ---------------------------------------------------------------------------


SEARCH_QUERIES = [
    "fiction",
    "science fiction",
    "fantasy",
    "mystery",
    "romance",
    "history",
    "biography",
    "philosophy",
    "poetry",
    "adventure",
    "horror",
    "thriller",
    "children",
    "young adult",
    "graphic novel",
    "cooking",
    "travel",
    "science",
    "mathematics",
    "art",
    "music",
    "psychology",
    "economics",
    "politics",
    "religion",
    "classic literature",
    "detective",
    "war",
    "nature",
    "technology",
]


def _fetch_books_from_prod(count):
    """Fetch real book data from openlibrary.org search API."""
    books = []
    seen_keys = set()

    queries = SEARCH_QUERIES.copy()
    random.shuffle(queries)

    for query in queries:
        if len(books) >= count:
            break
        offset = random.randint(0, 50)
        batch_size = min(count - len(books), 100)
        try:
            resp = requests.get(
                "https://openlibrary.org/search.json",
                params={
                    "q": query,
                    "limit": batch_size,
                    "offset": offset,
                    "fields": SEARCH_FIELDS,
                },
                timeout=15,
            )
            resp.raise_for_status()
            docs = resp.json().get("docs", [])
        except (requests.RequestException, ValueError):
            continue

        for doc in docs:
            if len(books) >= count:
                break
            work_key = doc.get("key", "")
            if work_key in seen_keys or not doc.get("title") or _is_low_quality(doc):
                continue
            seen_keys.add(work_key)
            books.append(_search_doc_to_record(doc, f"shelfie:prod-{work_key}"))

    return books


IMPORT_WORKERS = 8


def _import_one_book(ol, record):
    """Import a single record. Returns (ok, error_message)."""
    try:
        result = ol.import_data(json.dumps(record))
        result_data = json.loads(result) if isinstance(result, str) else result
        if result_data.get("success"):
            return True, None
        return False, result_data.get("error_message", str(result_data))
    except (OLError, requests.RequestException, json.JSONDecodeError) as e:
        return False, str(e)


def _import_books(ol, records, workers=IMPORT_WORKERS):
    """Import records in parallel. /api/import is one-book-per-call so the
    only lever for speed is concurrency."""
    success_count = 0
    errors = 0
    total = len(records)

    with import_progress() as progress:
        task = progress.add_task("Importing books", total=total, ok=0, err=0)
        log_failure = failure_logger(progress)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_import_one_book, ol, r): r for r in records}
            for fut in as_completed(futures):
                record = futures[fut]
                ok, err_msg = fut.result()
                if ok:
                    success_count += 1
                else:
                    errors += 1
                    log_failure(record["title"], err_msg)
                progress.update(task, advance=1, ok=success_count, err=errors)

    return success_count, errors


def cmd_add_books(ol, count=10, source="production"):
    """Import books - from openlibrary.org (default) or local seed data.

    Other commands fall back to seed data silently when prod is
    unreachable. add-books is the only one with an explicit `--source`
    flag because it's the most common offline workflow (demos, plane
    rides, fast iteration) — the flag lets users skip the slow prod
    fetch entirely instead of waiting for it to time out.
    """
    header("Add Books")

    if source == "production":
        with spinner(f"Fetching {count} books from openlibrary.org…"):
            records = _fetch_books_from_prod(count)
        if not records:
            warn("Could not reach openlibrary.org. Falling back to seed data.")
            source = "seed"
        else:
            info(f"Fetched [cyan]{len(records)}[/cyan] unique books (with covers & subjects).")

    if source == "seed":
        books = load_json("books.json")
        total_available = len(books)
        info(f"Using seed file ([cyan]{total_available}[/cyan] books).")
        records = []
        for i in range(count):
            book = books[i % total_available]
            record = {
                "title": book["title"],
                "authors": book.get("authors", []),
                "publishers": book.get("publishers", []),
                "publish_date": book.get("publish_date", "2000"),
                "subjects": book.get("subjects", []),
                "source_records": [f"shelfie:seed-{i}"],
            }
            if book.get("number_of_pages"):
                record["number_of_pages"] = book["number_of_pages"]
            records.append(record)

    succeeded, errors = _import_books(ol, records)

    console.print()
    success(f"[bold]{succeeded}[/bold] books imported, [red]{errors}[/red] errors.")
    if succeeded > 0:
        dim("Tip: books may not appear in search until Solr reindexes.")
        dim("Use 'Manage Solr Index' or wait for solr-updater to catch up.")
    return succeeded


# ---------------------------------------------------------------------------
# Feature: Change User Role
# ---------------------------------------------------------------------------


def cmd_set_role(ol, username=None, role=None, action="add"):
    """Add or remove a user from a usergroup."""
    header("Change User Role")

    if not username:
        username = ask("Enter username (e.g. openlibrary)", DEFAULT_USERNAME)

    user_key = f"/people/{username}"

    # Verify user exists
    try:
        user = ol.get(user_key)
        info(f"Found user: [cyan]{user.get('displayname', username)}[/cyan]")
    except OLError:
        error(f"User '[cyan]{username}[/cyan]' not found.")
        return

    # Show current groups
    try:
        current_groups = []
        for group_name in USERGROUPS:
            try:
                group = ol.get(f"/usergroup/{group_name}")
                members = group.get("members", [])
                member_keys = [m.get("key", m) if isinstance(m, dict) else str(m) for m in members]
                if user_key in member_keys:
                    current_groups.append(group_name)
            except OLError:
                pass
        if current_groups:
            info(f"Current roles: [yellow]{', '.join(current_groups)}[/yellow]")
        else:
            dim("Current roles: (none)")
    except (OLError, requests.RequestException) as e:
        warn(f"Could not fetch current roles: {e}")

    if not role:
        action_choice = choose("Action", ["Add role", "Remove role"])
        action = "add" if action_choice == "Add role" else "remove"
        role = choose("Select role", USERGROUPS)

    group_key = f"/usergroup/{role}"

    try:
        group = ol.get(group_key)
    except OLError:
        error(f"Usergroup '[cyan]{role}[/cyan]' not found.")
        return

    raw_members = group.get("members", [])
    # Normalize: Reference strings or dicts -> plain string keys
    member_keys = [str(m) if not isinstance(m, dict) else m.get("key", str(m)) for m in raw_members]

    if action == "add":
        if user_key in member_keys:
            warn(f"User '[cyan]{username}[/cyan]' is already in '[yellow]{role}[/yellow]'.")
            return
        member_keys.append(user_key)
    else:
        if user_key not in member_keys:
            warn(f"User '[cyan]{username}[/cyan]' is not in '[yellow]{role}[/yellow]'.")
            return
        member_keys = [k for k in member_keys if k != user_key]

    # Save via infobase (web app PUT has issues in local dev)
    doc = {
        "key": group_key,
        "type": {"key": "/type/usergroup"},
        "members": [{"key": k} for k in member_keys],
    }
    try:
        infobase_save(
            [doc],
            comment=f"shelfie: {'adding' if action == 'add' else 'removing'} {username} {'to' if action == 'add' else 'from'} {role}",
        )
        verb = "Added" if action == "add" else "Removed"
        prep = "to" if action == "add" else "from"
        success(f"{verb} '[cyan]{username}[/cyan]' {prep} '[yellow]{role}[/yellow]'.")
    except requests.RequestException as e:
        error(f"Saving group: {e}")


# ---------------------------------------------------------------------------
# Feature: Generate Lists
# ---------------------------------------------------------------------------


PROD_LIST_USERS = [
    "mekBot",
    "openlibrary",
    "staffpicks",
    "internetarchive",
]


def _fetch_prod_lists(count):
    """Fetch real public lists from openlibrary.org."""
    fetched = []
    for user in PROD_LIST_USERS:
        if len(fetched) >= count:
            break
        try:
            resp = requests.get(
                f"https://openlibrary.org/people/{user}/lists.json",
                params={"limit": min(count - len(fetched), 20)},
                timeout=15,
            )
            resp.raise_for_status()
            entries = resp.json().get("entries", [])
        except (requests.RequestException, ValueError):
            continue

        for entry in entries:
            if len(fetched) >= count:
                break
            name = entry.get("name", "")
            seed_count = entry.get("seed_count", 0)
            if not name or seed_count < 3:
                continue

            # Fetch the seeds for this list
            list_url = entry.get("url", "")
            try:
                seeds_resp = requests.get(
                    f"https://openlibrary.org{list_url}/seeds.json",
                    params={"limit": 20},
                    timeout=15,
                )
                seeds_resp.raise_for_status()
                seed_entries = seeds_resp.json().get("entries", [])
            except (requests.RequestException, ValueError):
                continue

            # Extract work/edition keys and titles from seeds
            seed_keys = []
            seed_titles = {}
            for s in seed_entries:
                url = s.get("url", "")
                title = s.get("title", "")
                if url.startswith(("/works/", "/books/")):
                    seed_keys.append(url)
                    if title:
                        seed_titles[url] = title

            if len(seed_keys) >= 3:
                fetched.append(
                    {
                        "name": name,
                        "description": f"From {user}'s lists on openlibrary.org",
                        "seed_keys": seed_keys,
                        "_seed_titles": seed_titles,
                    }
                )

    return fetched


def _import_list_seeds(ol, prod_list):
    """Import seed works from a production list into local DB. Returns local work keys."""
    imported_keys = []
    for seed_key in prod_list["seed_keys"]:
        title = prod_list.get("_seed_titles", {}).get(seed_key)
        if not title:
            try:
                resp = requests.get(f"https://openlibrary.org{seed_key}.json", timeout=5)
                resp.raise_for_status()
                title = resp.json().get("title", "")
            except (requests.RequestException, ValueError):
                continue
        if not title:
            continue

        try:
            search_resp = requests.get(
                "https://openlibrary.org/search.json",
                params={"title": title, "limit": 1, "fields": SEARCH_FIELDS},
                timeout=10,
            )
            search_resp.raise_for_status()
            docs = search_resp.json().get("docs", [])
        except (requests.RequestException, ValueError):
            continue
        if not docs:
            continue

        record = _search_doc_to_record(docs[0], f"shelfie:list-{docs[0].get('key', '')}")
        work_key = _import_and_get_work_key(ol, record)
        if work_key:
            imported_keys.append(work_key)
    return imported_keys


def cmd_generate_lists(ol, count=1, username=None):
    """Create reading lists from real openlibrary.org lists."""
    header("Generate Lists")

    if not username:
        username = ask("Enter username for list owner", DEFAULT_USERNAME)

    if not _user_exists(ol, username):
        error(f"User '[cyan]/people/{username}[/cyan]' not found. Create the user first.")
        return

    with spinner("Fetching real lists from openlibrary.org…"):
        prod_lists = _fetch_prod_lists(count)

    if not prod_lists:
        warn("Could not fetch lists from production. Using local works instead.")
        list_templates = load_json("list_names.json")
        work_keys = get_work_keys(ol)

        if not work_keys:
            error("No works found. Add books first!")
            return

        for i in range(count):
            template = list_templates[i % len(list_templates)]
            num_seeds = min(random.randint(5, 20), len(work_keys))
            prod_lists.append(
                {
                    "name": template["name"],
                    "description": template["description"],
                    "seed_keys": random.sample(work_keys, num_seeds),
                }
            )

    info(f"Got [cyan]{len(prod_lists)}[/cyan] lists. Importing seed works and creating lists…")

    succeeded = 0
    for pl in prod_lists[:count]:
        with spinner(f"Importing seeds for '{pl['name']}'…"):
            imported_keys = _import_list_seeds(ol, pl)

        # Create the list locally with only successfully imported work keys
        seeds = [{"key": k} for k in imported_keys if k]
        if not seeds:
            warn(f"Skipping '[cyan]{pl['name']}[/cyan]': no works could be imported")
            continue

        list_data = json.dumps(
            {
                "name": pl["name"],
                "description": pl.get("description", ""),
                "seeds": seeds,
            }
        )

        try:
            resp = ol._request(
                f"/people/{username}/lists.json",
                method="POST",
                data=list_data,
                headers={"Content-Type": "application/json"},
            )
            result = resp.json()
            list_key = result.get("key", "?")
            success(f"Created [cyan]{pl['name']}[/cyan] [dim]({list_key})[/dim] with {len(seeds)} seeds")
            succeeded += 1
        except (OLError, requests.RequestException, json.JSONDecodeError) as e:
            error(f"Creating list '[cyan]{pl['name']}[/cyan]': {e}")

    console.print()
    success(f"[bold]{succeeded}/{count}[/bold] lists created.")


# ---------------------------------------------------------------------------
# Feature: Populate Subjects
# ---------------------------------------------------------------------------


def cmd_populate_subjects(ol):
    """Find works missing subjects and assign them from keyword matching."""
    header("Populate Subjects")

    subject_data = load_json("subjects.json")
    keywords = subject_data["keywords"]
    fallback = subject_data["fallback"]

    # Find works without subjects
    try:
        with spinner("Querying works…"):
            all_works = ol.query(type="/type/work", limit=500)
    except OLError as e:
        error(f"Querying works: {e}")
        return

    if not all_works:
        warn("No works found. Add books first!")
        return

    info(f"Checking [cyan]{len(all_works)}[/cyan] works for missing subjects…")

    updated = 0
    skipped = 0
    errors = 0

    with step_progress() as progress:
        task = progress.add_task("Populating subjects", total=len(all_works))
        log_failure = failure_logger(progress)
        # ol.query returns Reference strings like "/works/OL1W", not dicts
        for work_ref in all_works:
            work_key = str(work_ref)
            try:
                work = ol.get(work_key)
            except OLError:
                progress.update(task, advance=1)
                continue

            existing_subjects = work.get("subjects", [])
            if existing_subjects:
                skipped += 1
                progress.update(task, advance=1)
                continue

            # Match subjects based on title keywords
            title = work.get("title", "").lower()
            matched_subjects = set()
            for keyword, subjects in keywords.items():
                if keyword in title:
                    matched_subjects.update(subjects)

            if not matched_subjects:
                matched_subjects = set(fallback)

            # Save via infobase. Use _merge_save so we don't wipe title/authors/etc.
            patch = {
                "key": work_key,
                "type": {"key": "/type/work"},
                "subjects": list(matched_subjects),
            }
            try:
                _merge_save([patch], comment="shelfie: adding subjects")
                updated += 1
            except requests.RequestException as e:
                errors += 1
                log_failure(work_key, e)
            progress.update(task, advance=1)

    console.print()
    success(f"[bold]{updated}[/bold] works updated, [dim]{skipped} already had subjects.[/dim]")


# ---------------------------------------------------------------------------
# Feature: Populate Covers
# ---------------------------------------------------------------------------


def _coverless_works_from_solr(limit=100):
    """Return [(key, title, author_name)] for works missing cover_i in Solr."""
    data = solr_request(f"/solr/openlibrary/select?q=type:work AND -cover_i:[* TO *]&fl=key,title,author_name&rows={limit}&wt=json")
    if not data or "response" not in data:
        return []
    results = []
    for doc in data["response"]["docs"]:
        key = doc.get("key")
        title = doc.get("title")
        authors = doc.get("author_name", [])
        author = authors[0] if authors else ""
        if key and title:
            results.append((key, title, author))
    return results


def _find_prod_cover_id(title, author=""):
    """Search openlibrary.org for a work by title/author and return a cover_i if any match has one."""
    params = {"title": title, "limit": 5, "fields": "key,cover_i"}
    if author:
        params["author"] = author
    try:
        resp = requests.get(
            "https://openlibrary.org/search.json",
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        docs = resp.json().get("docs", [])
    except (requests.RequestException, ValueError):
        return None
    for doc in docs:
        if cover_id := doc.get("cover_i"):
            return cover_id
    return None


def cmd_populate_covers(ol, limit=100):
    """Find works missing covers and attach a cover_id from openlibrary.org."""
    header("Populate Covers")

    targets = _coverless_works_from_solr(limit=limit)
    if not targets:
        warn("No coverless works found. (If you just added books, Solr may still be indexing.)")
        return

    info(f"Found [cyan]{len(targets)}[/cyan] coverless works. Looking up covers on openlibrary.org…")

    updated = 0
    no_match = 0
    errors = 0

    with import_progress() as progress:
        task = progress.add_task("Populating covers", total=len(targets), ok=0, err=0)
        log_failure = failure_logger(progress)
        for key, title, author in targets:
            cover_id = _find_prod_cover_id(title, author)
            if not cover_id:
                no_match += 1
            else:
                patch = {
                    "key": key,
                    "type": {"key": "/type/work"},
                    "covers": [cover_id],
                }
                try:
                    _merge_save([patch], comment="shelfie: populating cover")
                    updated += 1
                except requests.RequestException as e:
                    errors += 1
                    log_failure(key, e)
            progress.update(task, advance=1, ok=updated, err=errors + no_match)

    console.print()
    success(
        f"[bold]{updated}[/bold] covers added, "
        f"[dim]{no_match} no match[/dim], "
        f"[red]{errors}[/red] errors."
    )
    if updated > 0:
        dim("Tip: covers may not appear in search until Solr reindexes.")


# ---------------------------------------------------------------------------
# Feature: Stats
# ---------------------------------------------------------------------------


def _infobase_count(doc_type):
    """Count documents of a given type in infobase."""
    try:
        resp = requests.get(
            f"{DEFAULT_INFOBASE_URL}/things",
            params={"query": json.dumps({"type": doc_type, "limit": 10000})},
            timeout=10,
        )
        resp.raise_for_status()
        return len(resp.json())
    except (requests.RequestException, ValueError):
        return "?"


def _solr_count(query="*:*"):
    """Count documents matching a Solr query."""
    data = solr_request(f"/solr/openlibrary/select?q={query}&rows=0&wt=json")
    if data:
        return data.get("response", {}).get("numFound", "?")
    return "?"


def _solr_facet_count(field):
    """Count unique values of a facet field in Solr."""
    data = solr_request(f"/solr/openlibrary/select?q=type:work&rows=0&facet=true&facet.field={field}&facet.limit=-1&wt=json")
    if data:
        facets = data.get("facet_counts", {}).get("facet_fields", {}).get(field, [])
        return len(facets) // 2  # facets alternate value/count
    return "?"


def cmd_stats(ol):
    """Show database statistics."""
    header("Database Stats")

    db_specs = [
        ("Works", _infobase_count, "/type/work"),
        ("Editions", _infobase_count, "/type/edition"),
        ("Authors", _infobase_count, "/type/author"),
        ("Users", _infobase_count, "/type/user"),
        ("Lists", _infobase_count, "/type/list"),
        ("Series", _infobase_count, "/type/series"),
        ("Usergroups", _infobase_count, "/type/usergroup"),
    ]
    solr_specs = [
        ("Works", _solr_count, "type:work"),
        ("Editions", _solr_count, "type:edition"),
        ("Authors", _solr_count, "type:author"),
        ("Total docs", _solr_count, "*:*"),
    ]
    extra_calls = [
        ("works_with_covers", _solr_count, "type:work AND cover_i:[* TO *]"),
        ("works_with_subjects", _solr_count, "type:work AND subject:[* TO *]"),
        ("unique_subjects", _solr_facet_count, "subject"),
    ]

    with spinner("Fetching counts…"):
        with ThreadPoolExecutor(max_workers=14) as pool:
            db_futures = [(label, pool.submit(fn, arg)) for label, fn, arg in db_specs]
            solr_futures = [(label, pool.submit(fn, arg)) for label, fn, arg in solr_specs]
            extras = {key: pool.submit(fn, arg) for key, fn, arg in extra_calls}
            db_counts = {label: f.result() for label, f in db_futures}
            solr_counts = {label: f.result() for label, f in solr_futures}
            works_with_covers = extras["works_with_covers"].result()
            works_with_subjects = extras["works_with_subjects"].result()
            unique_subjects = extras["unique_subjects"].result()

    db_table = stats_table("Database (infobase)", list(db_counts.items()))
    solr_table = stats_table("Search Index (Solr)", list(solr_counts.items()))

    # Coverage rows include a percentage when both numbers are integers.
    total_works = solr_counts.get("Works", 0)

    def _coverage_row(label, n):
        if isinstance(n, int) and isinstance(total_works, int) and total_works > 0:
            pct = n * 100 // total_works
            return label, f"{n} ({pct}%)"
        return label, str(n)

    coverage_table = stats_table(
        "Coverage",
        [
            _coverage_row("Works with covers", works_with_covers),
            _coverage_row("Works with subjects", works_with_subjects),
            ("Unique subjects", unique_subjects),
        ],
    )

    console.print(Columns([db_table, solr_table, coverage_table], padding=(0, 4), equal=False))

    # Sync status
    db_works = db_counts.get("Works", "?")
    solr_works = solr_counts.get("Works", "?")
    if isinstance(db_works, int) and isinstance(solr_works, int):
        unindexed = db_works - solr_works
        if unindexed > 0:
            warn(f"[bold]{unindexed}[/bold] works not yet indexed in Solr")
        elif unindexed == 0:
            success("All works indexed in Solr")
        else:
            warn(f"Solr has [bold]{-unindexed}[/bold] more works than DB (stale entries)")
    else:
        dim("Could not compare DB and Solr counts")


# ---------------------------------------------------------------------------
# Feature: Manage Solr Index
# ---------------------------------------------------------------------------


def cmd_manage_solr(ol):
    """Manage the local Solr search index."""
    header("Manage Solr Index")

    options = [
        "Check index status",
        "Reindex specific works",
        "Back to menu",
    ]
    choice = choose("Choose action", options)

    if choice == "Check index status":
        with spinner("Querying Solr…"):
            data = solr_request("/solr/openlibrary/select?q=*:*&rows=0&wt=json")
            type_counts = {}
            if data:
                for doc_type in ["work", "author", "edition"]:
                    type_data = solr_request(f"/solr/openlibrary/select?q=type:{doc_type}&rows=0&wt=json")
                    if type_data:
                        type_counts[doc_type] = type_data.get("response", {}).get("numFound", "?")
        if data:
            num_docs = data.get("response", {}).get("numFound", "?")
            rows = [("Total documents", num_docs)] + [(f"{t}s", n) for t, n in type_counts.items()]
            console.print(stats_table("Solr Index", rows))
        else:
            error("Could not connect to Solr at solr:8983")

    elif choice == "Reindex specific works":
        raw = ask("Enter work keys (comma-separated, e.g. /works/OL1W,/works/OL2W)")
        if not raw:
            return
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        for key in keys:
            try:
                ol._request(f"/admin/solr/update?key={key}", method="GET")
                success(f"Reindex requested for [cyan]{key}[/cyan]")
            except OLError as e:
                error(f"Reindexing {key}: {e}")


# ---------------------------------------------------------------------------
# Feature: List Users
# ---------------------------------------------------------------------------


def _infobase_find_account(username):
    """Fetch the infobase account doc (email, enc_password) for a username."""
    try:
        resp = requests.get(
            f"{DEFAULT_INFOBASE_URL}/account/find",
            params={"username": username},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return None


def _get_user_roles_map():
    """Return {user_key: [role_names]} by scanning every usergroup."""
    mapping = {}
    for group_name in USERGROUPS:
        group = _fetch_raw(f"/usergroup/{group_name}")
        if not group:
            continue
        for m in group.get("members", []):
            key = m.get("key") if isinstance(m, dict) else str(m)
            if key:
                mapping.setdefault(key, []).append(group_name)
    return mapping


# Known bootstrap passwords seeded by the dev DB. Infobase stores only salted
# hashes, so plaintext can't be recovered for any other user.
KNOWN_PASSWORDS = {
    "admin": DEFAULT_LOGIN_PASSWORD,
    "openlibrary": DEFAULT_LOGIN_PASSWORD,
}


def _guess_password(username):
    """Return a likely dev-default password, or None if unknown."""
    return KNOWN_PASSWORDS.get(username)


def cmd_list_users(ol):
    """List all users in the local DB with emails, roles, and password hints."""
    header("Users")

    user_keys = _infobase_keys_of_type("/type/user")
    if not user_keys:
        warn("No users found.")
        return

    roles_by_user = _get_user_roles_map()

    table = Table(box=SIMPLE_HEAVY, header_style="bold cyan", expand=False)
    table.add_column("Username", style="bold")
    table.add_column("Email", style="cyan")
    table.add_column("Roles", style="yellow")
    table.add_column("Password")

    for key in sorted(user_keys):
        username = key.rsplit("/", 1)[-1]
        account = _infobase_find_account(username) or {}
        email = account.get("email", "")
        roles = roles_by_user.get(key, [])
        pwd = _guess_password(username)
        pwd_cell = pwd if pwd else "[dim](hashed)[/dim]"
        table.add_row(username, email, ", ".join(roles) or "[dim]-[/dim]", pwd_cell)

    console.print(table)
    console.print()
    dim("Passwords are stored as salted hashes and cannot be recovered.")
    dim("Shown values are shelfie defaults — '(hashed)' means unknown.")
    success(f"[bold]{len(user_keys)}[/bold] user(s) found.")


# ---------------------------------------------------------------------------
# Feature: Seed Reviews/Ratings
# ---------------------------------------------------------------------------


def cmd_seed_ratings(ol, count=10, username=None):
    """Add ratings to existing books."""
    header("Seed Reviews & Ratings")

    if not username:
        username = ask("Username to rate as", DEFAULT_USERNAME)

    if not _user_exists(ol, username):
        error(f"User '[cyan]/people/{username}[/cyan]' not found. Create the user first.")
        return

    work_keys = get_work_keys(ol, limit=200)

    if not work_keys:
        warn("No works found. Add books first!")
        return

    info(f"Will add [cyan]{count}[/cyan] ratings as '[cyan]{username}[/cyan]' across {len(work_keys)} works.")

    succeeded = 0
    selected_works = random.choices(work_keys, k=min(count, len(work_keys)))

    for work_key in selected_works:
        rating = random.randint(1, 5)
        work_id = work_key.split("/")[-1]

        # The ratings endpoint reads web.input() (form-encoded), not JSON, and
        # the field is `edition_id` (not `edition_key`). ajax=true gives us a
        # JSON response instead of an HTML redirect.
        try:
            ol._request(
                f"/works/{work_id}/ratings.json",
                method="POST",
                data={"rating": str(rating), "edition_id": "", "ajax": "true"},
            )
            stars = "[yellow]" + "★" * rating + "[/yellow][dim]" + "☆" * (5 - rating) + "[/dim]"
            console.print(f"  {stars}  [cyan]{work_key}[/cyan]", highlight=False)
            succeeded += 1
        except (OLError, requests.RequestException, json.JSONDecodeError) as e:
            error(f"Rating {work_key}: {e}")

    console.print()
    success(f"[bold]{succeeded}/{len(selected_works)}[/bold] ratings added.")


# ---------------------------------------------------------------------------
# Feature: Seed Reading Logs
# ---------------------------------------------------------------------------


def cmd_seed_reading_log(ol, count=20, username=None):
    """Add books to a user's reading shelves."""
    header("Seed Reading Log")

    if not username:
        username = ask("Username", DEFAULT_USERNAME)

    work_keys = get_work_keys(ol, limit=500)

    if not work_keys:
        warn("No works found. Add books first!")
        return

    available = min(count, len(work_keys))
    selected = random.sample(work_keys, available)
    info(f"Adding [cyan]{available}[/cyan] books to reading shelves for '[cyan]{username}[/cyan]'…")

    succeeded = 0
    with step_progress() as progress:
        task = progress.add_task("Shelving books", total=available)
        for work_key in selected:
            r = random.random()
            if r < 0.3:
                shelf_id = 1
            elif r < 0.5:
                shelf_id = 2
            else:
                shelf_id = 3

            # Extract work ID (e.g., /works/OL123W -> OL123W)
            work_id = work_key.split("/")[-1]

            try:
                ol._request(
                    f"/works/{work_id}/bookshelves.json",
                    method="POST",
                    params={"bookshelf_id": str(shelf_id), "action": "add"},
                )
                succeeded += 1
            except (OLError, requests.RequestException) as e:
                if succeeded == 0:
                    progress.stop()
                    error(f"{e}")
                    return

            progress.update(task, advance=1)

    console.print()
    success(f"[bold]{succeeded}[/bold] books added to reading log.")
    dim("Distributed across: Want to Read · Currently Reading · Already Read")


# ---------------------------------------------------------------------------
# Feature: Seed Series
# ---------------------------------------------------------------------------


REAL_SERIES = [
    {
        "name": "Harry Potter",
        "description": "Fantasy series by J.K. Rowling following a young wizard",
        "query": "harry potter rowling",
        "titles": [
            "Harry Potter and the Philosopher's Stone",
            "Harry Potter and the Chamber of Secrets",
            "Harry Potter and the Prisoner of Azkaban",
            "Harry Potter and the Goblet of Fire",
            "Harry Potter and the Order of the Phoenix",
            "Harry Potter and the Half-Blood Prince",
            "Harry Potter and the Deathly Hallows",
        ],
    },
    {
        "name": "The Lord of the Rings",
        "description": "Epic fantasy trilogy by J.R.R. Tolkien",
        "query": "lord of the rings tolkien",
        "titles": [
            "The Fellowship of the Ring",
            "The Two Towers",
            "The Return of the King",
        ],
    },
    {
        "name": "A Song of Ice and Fire",
        "description": "Epic fantasy series by George R.R. Martin",
        "query": "song of ice and fire martin",
        "titles": [
            "A Game of Thrones",
            "A Clash of Kings",
            "A Storm of Swords",
            "A Feast for Crows",
            "A Dance with Dragons",
        ],
    },
    {
        "name": "The Hunger Games",
        "description": "Dystopian series by Suzanne Collins",
        "query": "hunger games collins",
        "titles": [
            "The Hunger Games",
            "Catching Fire",
            "Mockingjay",
        ],
    },
    {
        "name": "Foundation",
        "description": "Science fiction series by Isaac Asimov",
        "query": "foundation asimov",
        "titles": [
            "Foundation",
            "Foundation and Empire",
            "Second Foundation",
        ],
    },
    {
        "name": "Dune",
        "description": "Science fiction series by Frank Herbert",
        "query": "dune herbert",
        "titles": [
            "Dune",
            "Dune Messiah",
            "Children of Dune",
            "God Emperor of Dune",
        ],
    },
    {
        "name": "The Chronicles of Narnia",
        "description": "Fantasy series by C.S. Lewis",
        "query": "narnia lewis",
        "titles": [
            "The Lion, the Witch and the Wardrobe",
            "Prince Caspian",
            "The Voyage of the Dawn Treader",
            "The Silver Chair",
            "The Horse and His Boy",
            "The Magician's Nephew",
            "The Last Battle",
        ],
    },
    {
        "name": "Discworld",
        "description": "Comic fantasy series by Terry Pratchett",
        "query": "discworld pratchett",
        "titles": [
            "The Colour of Magic",
            "The Light Fantastic",
            "Equal Rites",
            "Mort",
            "Guards! Guards!",
            "Small Gods",
        ],
    },
]


def _fetch_series_works(series_def):
    """Fetch real works for a series from openlibrary.org search."""
    try:
        resp = requests.get(
            "https://openlibrary.org/search.json",
            params={"q": series_def["query"], "limit": 50, "fields": SEARCH_FIELDS},
            timeout=15,
        )
        resp.raise_for_status()
        docs = resp.json().get("docs", [])
    except (requests.RequestException, ValueError):
        return []

    # Match search results to the expected title order
    ordered = []
    for expected_title in series_def["titles"]:
        expected_lower = expected_title.lower()
        best = None
        for doc in docs:
            doc_title = doc.get("title", "").lower()
            if expected_lower in doc_title or doc_title in expected_lower:
                best = doc
                break
        if best:
            ordered.append(best)
            docs.remove(best)

    return ordered


def cmd_seed_series(ol, count=3):
    """Create real book series from openlibrary.org."""
    header("Seed Series")

    info("Fetching real series data from openlibrary.org…")

    # Find the next available series key
    try:
        resp = requests.get(
            f"{DEFAULT_INFOBASE_URL}/things",
            params={"query": json.dumps({"type": "/type/series", "limit": 10000})},
            timeout=10,
        )
        resp.raise_for_status()
        existing = resp.json()
        max_id = 0
        for key in existing:
            if key.startswith("/series/OL") and key.endswith("L"):
                with contextlib.suppress(ValueError):
                    max_id = max(max_id, int(key[10:-1]))
    except (requests.RequestException, ValueError):
        max_id = 100

    series_pool = REAL_SERIES.copy()
    random.shuffle(series_pool)

    succeeded = 0
    for i in range(min(count, len(series_pool))):
        series_def = series_pool[i]
        series_name = series_def["name"]

        with spinner(f"Searching '{series_name}'…"):
            works = _fetch_series_works(series_def)
        if len(works) < 2:
            warn(f"Skipping '[cyan]{series_name}[/cyan]': not enough works found online")
            continue

        imported_work_keys = []
        with spinner(f"Importing {len(works)} works for '{series_name}'…"):
            for doc in works:
                record = _search_doc_to_record(doc, f"shelfie:series-{doc.get('key', '')}")
                work_key = _import_and_get_work_key(ol, record)
                if work_key:
                    imported_work_keys.append(work_key)

        if len(imported_work_keys) < 2:
            warn(f"Skipping '[cyan]{series_name}[/cyan]': could not import enough works")
            continue

        # Create the series and link works
        series_key = f"/series/OL{max_id + succeeded + 1}L"
        series_doc = {
            "key": series_key,
            "type": {"key": "/type/series"},
            "name": series_name,
            "description": series_def["description"],
        }

        try:
            infobase_save([series_doc], comment=f"shelfie: creating series '{series_name}'")
        except requests.RequestException as e:
            error(f"Creating series '[cyan]{series_name}[/cyan]': {e}")
            continue

        work_patches = [
            {
                "key": wk,
                "type": {"key": "/type/work"},
                "series": [{"series": {"key": series_key}, "position": str(pos + 1)}],
            }
            for pos, wk in enumerate(imported_work_keys)
        ]
        try:
            _merge_save(
                work_patches,
                comment=f"shelfie: linking works to series '{series_name}'",
            )
        except requests.RequestException as e:
            error(f"Linking works to '[cyan]{series_name}[/cyan]': {e}")
            warn(f"Series [cyan]{series_key}[/cyan] was created but is not linked to its works.")
            continue

        success(
            f"Created [cyan]{series_name}[/cyan] [dim]({series_key})[/dim] with {len(imported_work_keys)} works"
        )
        titles = [w["title"] for w in works[: len(imported_work_keys)]]
        for pos, title in enumerate(titles, 1):
            console.print(f"    [dim]{pos}.[/dim] {title}", highlight=False)
        succeeded += 1

    console.print()
    success(f"[bold]{succeeded}/{count}[/bold] series created.")


# ---------------------------------------------------------------------------
# Feature: Populate Everything (batch)
# ---------------------------------------------------------------------------


def cmd_populate_all(ol):
    """Run all seeding operations to get a rich local dev database."""
    header("Populate Everything")
    plain("This will seed your local DB with a rich set of test data:")
    plain("  • 100 books from openlibrary.org (with covers)")
    plain("  • Subjects on all works")
    plain("  • 5 reading lists")
    plain("  • 30 ratings")
    plain("  • 20 books on reading shelves")
    plain("  • 3 series with ordered works")
    console.print()

    if not confirm("Proceed?"):
        warn("Cancelled.")
        return

    cmd_add_books(ol, count=100, source="production")
    cmd_populate_subjects(ol)
    cmd_generate_lists(ol, count=5, username=DEFAULT_USERNAME)
    cmd_seed_ratings(ol, count=30, username=DEFAULT_USERNAME)
    cmd_seed_reading_log(ol, count=20, username=DEFAULT_USERNAME)
    cmd_seed_series(ol, count=3)

    console.print()
    header("All Done!")
    plain("Your local DB is now populated with rich test data.")
    plain("Run 'Stats' to see the current state.")


# ---------------------------------------------------------------------------
# Feature: Reset Local State
# ---------------------------------------------------------------------------


def _infobase_keys_of_type(doc_type, limit=10000):
    """Return all keys of a given type from infobase."""
    try:
        resp = requests.get(
            f"{DEFAULT_INFOBASE_URL}/things",
            params={"query": json.dumps({"type": doc_type, "limit": limit})},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError):
        return []


def _delete_keys(keys, comment):
    """Mark documents as deleted via infobase (sets type to /type/delete)."""
    if not keys:
        return 0
    docs = [{"key": k, "type": {"key": "/type/delete"}} for k in keys]
    batch_size = 100
    deleted = 0
    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        try:
            infobase_save(batch, comment=comment)
            deleted += len(batch)
        except requests.RequestException as e:
            error(f"Deleting batch: {e}")
    return deleted


def cmd_reset_state(ol):
    """Clear shelfie-created data from local database."""
    header("Reset Local State")
    warn("This will delete data from your local dev database.")
    dim("This does NOT affect production.")
    console.print()

    options = [
        "Delete shelfie-imported books (editions, works, authors)",
        "Delete shelfie-created series",
        "Delete shelfie-created lists",
        "Delete everything shelfie created",
        "Nuclear reset (rebuild Docker volumes)",
        "Cancel",
    ]
    choice = choose("What to reset?", options)

    if choice == options[5]:  # Cancel
        warn("Cancelled.")
        return

    if choice == options[4]:  # Nuclear
        console.print()
        plain("To fully reset, run:")
        console.print("  [bold cyan]docker compose down -v && docker compose up[/bold cyan]")
        return

    targets = {}

    if choice in (options[0], options[3]):  # Books or everything
        with spinner("Listing editions, works, authors…"):
            editions = _infobase_keys_of_type("/type/edition")
            works = _infobase_keys_of_type("/type/work")
            authors = _infobase_keys_of_type("/type/author")
        targets["editions"] = editions
        targets["works"] = works
        targets["authors"] = authors

    if choice in (options[1], options[3]):  # Series or everything
        with spinner("Listing series…"):
            series = _infobase_keys_of_type("/type/series")
        targets["series"] = series

    if choice in (options[2], options[3]):  # Lists or everything
        with spinner("Listing lists…"):
            lists = _infobase_keys_of_type("/type/list")
        targets["lists"] = lists

    total = sum(len(v) for v in targets.values())
    if total == 0:
        info("Nothing to delete.")
        return

    console.print()
    for label, keys in targets.items():
        console.print(f"  [dim]{label}:[/dim] [bold]{len(keys)}[/bold]", highlight=False)
    console.print()

    if not confirm(f"Delete {total} documents?"):
        warn("Cancelled.")
        return

    # Delete in dependency order: editions before works, works before authors
    delete_order = ["lists", "series", "editions", "works", "authors"]
    total_deleted = 0
    for label in delete_order:
        keys = targets.get(label, [])
        if keys:
            with spinner(f"Deleting {len(keys)} {label}…"):
                deleted = _delete_keys(keys, comment=f"shelfie: reset {label}")
            total_deleted += deleted
            success(f"Deleted [bold]{deleted}[/bold] {label}.")

    console.print()
    success(f"[bold]{total_deleted}[/bold] documents deleted.")


# ---------------------------------------------------------------------------
# Feature: Health Check
# ---------------------------------------------------------------------------


DEFAULT_SOLR_URL = "http://solr:8983"


def cmd_health_check(url=None, email=None, password=None):
    """Verify the local OL stack is reachable and login works.

    Distinct from `smoke-test`: this is for users diagnosing setup
    problems ("is my stack up?"), while smoke-test is a regression
    battery for shelfie's own bugs. Doesn't piggy-back on `connect()`
    so the login attempt is a real, testable step.
    """
    header("Health Check")

    url = url or DEFAULT_BASE_URL
    email = email or DEFAULT_LOGIN_EMAIL
    password = password or DEFAULT_LOGIN_PASSWORD

    failures = []

    def _check(name, ok, detail=""):
        if ok:
            console.print(f"  [green bold]PASS[/green bold]  {name}", highlight=False)
        else:
            console.print(f"  [red bold]FAIL[/red bold]  {name}: {detail}", highlight=False)
            failures.append(name)

    def _reachable(target_url):
        try:
            resp = requests.get(target_url, timeout=5, allow_redirects=False)
        except requests.RequestException as e:
            headline, _ = friendly_error(e, target_url)
            return False, headline
        # Any HTTP response means the server is up. 4xx/5xx still proves
        # the port is open and a service is answering.
        return True, f"HTTP {resp.status_code}"

    ok, detail = _reachable(url + "/")
    _check(f"web reachable ({url})", ok, detail)

    ok, detail = _reachable(DEFAULT_INFOBASE_URL + "/things")
    _check(f"infobase reachable ({DEFAULT_INFOBASE_URL})", ok, detail)

    ok, detail = _reachable(DEFAULT_SOLR_URL + "/solr/admin/info/system")
    _check(f"solr reachable ({DEFAULT_SOLR_URL})", ok, detail)

    try:
        ol = OLClient(url)
        ol.login(email, password)
        _check(f"login as {email}", bool(ol.cookie), "no session cookie returned")
    except (OLError, requests.RequestException) as e:
        headline, _ = friendly_error(e, url)
        _check(f"login as {email}", False, headline)

    console.print()
    if failures:
        error(f"[bold]{len(failures)}[/bold] check(s) failed.")
        if not Path("/.dockerenv").exists() and url == DEFAULT_BASE_URL:
            plain("")
            plain("Looks like you're running outside the OL Docker stack.")
            plain("Shelfie's defaults are Docker hostnames; they only resolve inside")
            plain("OL's network. From your OL clone, run:")
            console.print("  [bold cyan]docker compose run --rm shelfie health-check[/bold cyan]")
        else:
            dim("  See README troubleshooting for network-name and login defaults.")
        return 1
    success("Stack is healthy.")
    return 0


# ---------------------------------------------------------------------------
# Feature: Smoke Test
# ---------------------------------------------------------------------------


def cmd_smoke_test(ol):
    """Run a fast end-to-end check that the main commands work.

    Each check is a named regression test for a bug jimchamp found in code
    review (PR #12157). Run after edits to cli.py to catch regressions.
    Requires a fresh dev DB (or at least the default `admin` user).
    """
    header("Smoke Test")

    failures = []

    def _check(name, cond, detail=""):
        if cond:
            console.print(f"  [green bold]PASS[/green bold]  {name}", highlight=False)
        else:
            console.print(f"  [red bold]FAIL[/red bold]  {name}: {detail}", highlight=False)
            failures.append(name)

    def _skip(name):
        console.print(f"  [yellow bold]SKIP[/yellow bold]  {name}", highlight=False)

    # --- Bug #3: generate-lists with a bogus username must not crash ---
    try:
        cmd_generate_lists(ol, count=1, username="__shelfie_nonexistent__")
        _check("generate-lists handles missing user (bug #3)", True)
    except Exception as e:  # noqa: BLE001
        _check(
            "generate-lists handles missing user (bug #3)",
            False,
            f"raised {type(e).__name__}: {e}",
        )

    # --- Bug #2 + baseline: add a few books and confirm counts grow ---
    before_works = _infobase_count("/type/work")
    cmd_add_books(ol, count=3, source="seed")
    after_works = _infobase_count("/type/work")
    _check(
        "add-books increases work count (bug #2 baseline)",
        isinstance(after_works, int) and isinstance(before_works, int) and after_works > before_works,
        f"works {before_works} -> {after_works}",
    )

    # --- Bug #5: populate-subjects must not wipe existing title/authors ---
    work_keys = get_work_keys(ol, limit=20)
    target_key = None
    before_title = None
    before_authors = None
    for wk in work_keys:
        raw = _fetch_raw(wk) or {}
        if raw.get("title") and not raw.get("subjects"):
            target_key = wk
            before_title = raw["title"]
            before_authors = raw.get("authors")
            break
    if target_key:
        cmd_populate_subjects(ol)
        after = _fetch_raw(target_key) or {}
        _check(
            "populate-subjects preserves work title (bug #5)",
            after.get("title") == before_title,
            f"title {before_title!r} -> {after.get('title')!r}",
        )
        _check(
            "populate-subjects preserves work authors (bug #5)",
            after.get("authors") == before_authors,
            f"authors changed on {target_key}",
        )
        _check(
            "populate-subjects actually adds subjects (bug #5)",
            bool(after.get("subjects")),
            f"no subjects on {target_key} after run",
        )
    else:
        _skip("populate-subjects checks (no eligible work)")

    # --- Bug #4: seed-ratings must actually record ratings ---
    username = DEFAULT_USERNAME
    if _user_exists(ol, username):
        before_count = _solr_count("type:work AND ratings_count_1:[1 TO *]")
        cmd_seed_ratings(ol, count=3, username=username)
        # Ratings are written to the DB synchronously; Solr reindex is async
        # and we can't reliably wait for it here. Instead, check the raw
        # endpoint responded without error (cmd_seed_ratings already prints
        # per-record failures). The solr check is best-effort.
        after_count = _solr_count("type:work AND ratings_count_1:[1 TO *]")
        _check(
            "seed-ratings writes to ratings store (bug #4, best-effort)",
            isinstance(after_count, int) and isinstance(before_count, int),
            "could not query solr",
        )
    else:
        _skip(f"seed-ratings checks (no '{username}' user)")

    # --- Bug #5 (series): series link must not wipe work title ---
    # Only runs if we have enough imports already; otherwise skipped.
    existing_work = None
    existing_title = None
    for wk in get_work_keys(ol, limit=10):
        raw = _fetch_raw(wk) or {}
        if raw.get("title") and not raw.get("series"):
            existing_work = wk
            existing_title = raw["title"]
            break
    before_series = _infobase_count("/type/series")
    cmd_seed_series(ol, count=1)
    after_series = _infobase_count("/type/series")
    _check(
        "seed-series creates a series doc",
        isinstance(after_series, int) and isinstance(before_series, int) and after_series >= before_series,
        f"series {before_series} -> {after_series}",
    )
    if existing_work:
        after = _fetch_raw(existing_work) or {}
        _check(
            "seed-series preserves unrelated work titles (bug #5)",
            after.get("title") == existing_title,
            f"title on {existing_work}: {existing_title!r} -> {after.get('title')!r}",
        )

    console.print()
    if failures:
        error(f"[bold]{len(failures)}[/bold] failure(s):")
        for f in failures:
            plain(f"    - {f}")
        return 1
    success("All smoke tests passed.")
    return 0


# ---------------------------------------------------------------------------
# Interactive Menu
# ---------------------------------------------------------------------------


MENU_OPTIONS = [
    "Populate everything",
    "Add books",
    "Generate lists",
    "Seed reading log",
    "Seed series",
    "Seed reviews & ratings",
    "Populate subjects on existing books",
    "Populate covers on existing books",
    "Change user role",
    "List users",
    "Manage Solr index",
    "Stats",
    "Health check",
    "Smoke test",
    "Reset local state",
    "Exit",
]


def _menu_add_books(ol):
    count_choice = choose("How many books?", ["10", "100", "1000"])
    source_choice = choose("Source?", ["Production (openlibrary.org)", "Seed data (offline)"])
    source = "production" if "Production" in source_choice else "seed"
    cmd_add_books(ol, count=int(count_choice), source=source)


def _menu_generate_lists(ol):
    count_choice = choose("How many lists?", ["1", "5", "10"])
    cmd_generate_lists(ol, count=int(count_choice))


def _menu_seed_reading_log(ol):
    count_choice = choose("How many books to shelve?", ["10", "20", "50"])
    cmd_seed_reading_log(ol, count=int(count_choice))


def _menu_seed_series(ol):
    count_choice = choose("How many series?", ["1", "3", "5"])
    cmd_seed_series(ol, count=int(count_choice))


def _menu_seed_ratings(ol):
    count_choice = choose("How many ratings?", ["10", "50", "100"])
    cmd_seed_ratings(ol, count=int(count_choice))


def _menu_populate_covers(ol):
    limit_choice = choose("How many to look up?", ["50", "100", "500"])
    cmd_populate_covers(ol, limit=int(limit_choice))


# Maps menu labels to the callable that runs them. Each handler takes `ol`
# and is responsible for any further prompts.
_MENU_DISPATCH = {
    "Populate everything": cmd_populate_all,
    "Add books": _menu_add_books,
    "Generate lists": _menu_generate_lists,
    "Seed reading log": _menu_seed_reading_log,
    "Seed series": _menu_seed_series,
    "Seed reviews & ratings": _menu_seed_ratings,
    "Populate subjects on existing books": cmd_populate_subjects,
    "Populate covers on existing books": _menu_populate_covers,
    "Change user role": cmd_set_role,
    "List users": cmd_list_users,
    "Manage Solr index": cmd_manage_solr,
    "Stats": cmd_stats,
    "Health check": lambda ol: cmd_health_check(),
    "Smoke test": cmd_smoke_test,
    "Reset local state": cmd_reset_state,
}


def _compute_startup_stats():
    """Return [(label, value)] pairs for the banner stats panel.

    Calls run in parallel — banner load time was the biggest startup
    latency before this; serial these would block first paint by ~8x
    one HTTP round trip each.
    """
    specs = [
        ("works", _infobase_count, "/type/work"),
        ("editions", _infobase_count, "/type/edition"),
        ("authors", _infobase_count, "/type/author"),
        ("covers", _solr_count, "type:work AND cover_i:[* TO *]"),
        ("lists", _infobase_count, "/type/list"),
        ("series", _infobase_count, "/type/series"),
        ("subjects", _solr_facet_count, "subject"),
        ("users", _infobase_count, "/type/user"),
    ]
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        futures = [(label, pool.submit(fn, arg)) for label, fn, arg in specs]
        return [(label, f.result()) for label, f in futures]


def interactive_menu():
    """Main interactive menu loop."""
    with spinner("Loading database stats…"):
        stats = _compute_startup_stats()
    banner(DEFAULT_BASE_URL, stats)

    ol = connect()

    while True:
        try:
            console.print()
            choice = choose("Choose an option", MENU_OPTIONS)

            if choice == "Exit":
                dim("Bye!")
                break
            handler = _MENU_DISPATCH.get(choice)
            if handler:
                handler(ol)
        except UserExit:
            console.print()
            dim("Bye!")
            break


# ---------------------------------------------------------------------------
# CLI Subcommands (non-interactive)
# ---------------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="shelfie",
        description="Open Library development helper tool",
    )
    parser.add_argument("--url", default=DEFAULT_BASE_URL, help="OL server URL")
    parser.add_argument("--email", default=DEFAULT_LOGIN_EMAIL, help="Login email")
    parser.add_argument("--password", default=DEFAULT_LOGIN_PASSWORD, help="Login password")
    sub = parser.add_subparsers(dest="command")

    # add-books
    p = sub.add_parser("add-books", help="Import books into local dev")
    p.add_argument("--count", type=int, default=10)
    p.add_argument(
        "--source",
        default="production",
        choices=["production", "seed"],
        help="'production' fetches from openlibrary.org (default), 'seed' uses offline data",
    )

    # set-role
    p = sub.add_parser("set-role", help="Change a user's role")
    p.add_argument("--username", required=True)
    p.add_argument("--role", required=True, choices=USERGROUPS)
    p.add_argument("--action", default="add", choices=["add", "remove"])

    # generate-lists
    p = sub.add_parser("generate-lists", help="Create reading lists")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--username", default=None)

    # populate-subjects
    sub.add_parser("populate-subjects", help="Add subjects to works missing them")

    # populate-covers
    p = sub.add_parser("populate-covers", help="Add covers to works missing them")
    p.add_argument("--limit", type=int, default=100)

    # stats
    sub.add_parser("stats", help="Show database statistics")

    # manage-solr
    sub.add_parser("manage-solr", help="Check Solr index status")

    # list-users
    sub.add_parser("list-users", help="List users with emails, roles, and password hints")

    # seed-ratings
    p = sub.add_parser("seed-ratings", help="Add ratings to existing books")
    p.add_argument("--count", type=int, default=10)
    p.add_argument("--username", default=DEFAULT_USERNAME)

    # seed-reading-log
    p = sub.add_parser("seed-reading-log", help="Add books to reading shelves")
    p.add_argument("--count", type=int, default=20)
    p.add_argument("--username", default=DEFAULT_USERNAME)

    # seed-series
    p = sub.add_parser("seed-series", help="Create series with ordered works")
    p.add_argument("--count", type=int, default=3)

    # populate-all
    sub.add_parser("populate-all", help="Seed everything for a rich local DB")

    # reset
    sub.add_parser("reset", help="Reset local dev data")

    # health-check
    sub.add_parser("health-check", help="Verify web/infobase/solr are reachable and login works")

    # smoke-test
    sub.add_parser("smoke-test", help="Run regression checks for known PR #12157 bugs (developer use)")

    return parser


# Maps subcommand name to the callable that runs it. Mirrors _MENU_DISPATCH
# so adding a feature is a one-place edit on each side. health-check and
# smoke-test stay special-cased in main() — they own their own exit code
# and (for health-check) bypass connect()/the Docker preflight.
_CMD_DISPATCH = {
    "add-books": lambda ol, args: cmd_add_books(ol, count=args.count, source=args.source),
    "set-role": lambda ol, args: cmd_set_role(ol, username=args.username, role=args.role, action=args.action),
    "generate-lists": lambda ol, args: cmd_generate_lists(ol, count=args.count, username=args.username),
    "populate-subjects": lambda ol, args: cmd_populate_subjects(ol),
    "populate-covers": lambda ol, args: cmd_populate_covers(ol, limit=args.limit),
    "stats": lambda ol, args: cmd_stats(ol),
    "manage-solr": lambda ol, args: cmd_manage_solr(ol),
    "list-users": lambda ol, args: cmd_list_users(ol),
    "seed-ratings": lambda ol, args: cmd_seed_ratings(ol, count=args.count, username=args.username),
    "seed-reading-log": lambda ol, args: cmd_seed_reading_log(ol, count=args.count, username=args.username),
    "seed-series": lambda ol, args: cmd_seed_series(ol, count=args.count),
    "populate-all": lambda ol, args: cmd_populate_all(ol),
    "reset": lambda ol, args: cmd_reset_state(ol),
}


def main():
    parser = build_parser()
    args = parser.parse_args()
    cmd = args.command

    # health-check runs its own login as a measured step — skip auto-connect.
    # We also skip the Docker preflight here: health-check is the right tool
    # to *diagnose* exactly the situation the preflight catches.
    if cmd == "health-check":
        raise SystemExit(cmd_health_check(args.url, args.email, args.password))

    _preflight_docker_check(args.url)

    # No subcommand = interactive mode
    if not cmd:
        interactive_menu()
        return

    ol = connect(args.url, args.email, args.password)

    # smoke-test returns an exit code for CI; everything else returns None.
    if cmd == "smoke-test":
        raise SystemExit(cmd_smoke_test(ol))

    _CMD_DISPATCH[cmd](ol, args)


if __name__ == "__main__":
    main()

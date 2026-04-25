"""Tests for shelfie.cli - pure helpers and the merge-save logic
that protects against the bug where partial saves clobber existing fields
(see PR #12157 review, bug #5)."""

from unittest.mock import MagicMock, patch

import requests

from shelfie.cli import (
    _coverless_works_from_solr,
    _find_prod_cover_id,
    _guess_password,
    _is_low_quality,
    _merge_save,
    _pick_publisher,
    _search_doc_to_record,
    cmd_add_books,
    cmd_list_users,
    cmd_populate_covers,
    cmd_seed_series,
    cmd_set_role,
)


class TestIsLowQuality:
    def test_rejects_study_guide_title(self):
        assert _is_low_quality({"title": "Study Guide to Moby-Dick"})

    def test_rejects_workbook_title(self):
        assert _is_low_quality({"title": "Algebra II Workbook"})

    def test_rejects_self_published_publisher(self):
        assert _is_low_quality({"title": "My Book", "publisher": ["Independently published"]})

    def test_rejects_createspace(self):
        assert _is_low_quality({"title": "My Book", "publisher": ["CreateSpace"]})

    def test_accepts_normal_book(self):
        assert not _is_low_quality({"title": "Pride and Prejudice", "publisher": ["Penguin Classics"]})

    def test_accepts_mixed_publishers(self):
        # As long as one real publisher exists, don't reject.
        assert not _is_low_quality({"title": "Some Book", "publisher": ["Independently published", "Vintage"]})


class TestPickPublisher:
    def test_skips_rejected_and_returns_first_real(self):
        assert _pick_publisher({"publisher": ["Independently published", "Penguin", "Vintage"]}) == ["Penguin"]

    def test_fallback_when_empty(self):
        assert _pick_publisher({"publisher": []}) == ["Unknown"]

    def test_fallback_when_missing(self):
        assert _pick_publisher({}) == ["Unknown"]

    def test_fallback_when_all_rejected(self):
        assert _pick_publisher({"publisher": ["Independently published", "CreateSpace"]}) == ["Unknown"]


class TestSearchDocToRecord:
    def test_minimal_doc(self):
        rec = _search_doc_to_record({"title": "Hi"}, "shelfie:tag")
        assert rec["title"] == "Hi"
        assert rec["authors"] == [{"name": "Unknown"}]
        assert rec["source_records"] == ["shelfie:tag"]

    def test_isbn_13_routing(self):
        rec = _search_doc_to_record({"title": "X", "isbn": ["9780140449136"]}, "tag")
        assert rec["isbn_13"] == ["9780140449136"]
        assert "isbn_10" not in rec

    def test_isbn_10_routing(self):
        rec = _search_doc_to_record({"title": "X", "isbn": ["0140449132"]}, "tag")
        assert rec["isbn_10"] == ["0140449132"]
        assert "isbn_13" not in rec

    def test_cover_url(self):
        rec = _search_doc_to_record({"title": "X", "cover_i": 42}, "tag")
        assert rec["cover"] == "https://covers.openlibrary.org/b/id/42-L.jpg"

    def test_no_cover_when_missing(self):
        rec = _search_doc_to_record({"title": "X"}, "tag")
        assert "cover" not in rec

    def test_subjects_capped_at_ten(self):
        doc = {"title": "X", "subject": [f"s{i}" for i in range(20)]}
        rec = _search_doc_to_record(doc, "tag")
        assert len(rec["subjects"]) == 10


class TestMergeSave:
    """Regression test for PR #12157 bug #5 - partial saves wiping fields."""

    def test_preserves_existing_fields(self):
        existing = {
            "key": "/works/OL1W",
            "type": {"key": "/type/work"},
            "title": "Catching Fire",
            "subjects": ["Dystopia", "Young Adult"],
            "authors": [{"key": "/authors/OL1A"}],
            "covers": [135],
            "revision": 1,
            "latest_revision": 1,
            "created": {"type": "/type/datetime", "value": "2026-01-01T00:00:00"},
            "last_modified": {"type": "/type/datetime", "value": "2026-01-01T00:00:00"},
        }
        new_patch = {
            "key": "/works/OL1W",
            "type": {"key": "/type/work"},
            "series": [{"series": {"key": "/series/OL9L"}, "position": "2"}],
        }

        with (
            patch("shelfie.cli._fetch_raw", return_value=existing),
            patch("shelfie.cli.infobase_save") as mock_save,
        ):
            _merge_save([new_patch])

        saved_docs = mock_save.call_args[0][0]
        assert len(saved_docs) == 1
        saved = saved_docs[0]

        # All prior fields retained
        assert saved["title"] == "Catching Fire"
        assert saved["subjects"] == ["Dystopia", "Young Adult"]
        assert saved["authors"] == [{"key": "/authors/OL1A"}]
        assert saved["covers"] == [135]
        # New field applied
        assert saved["series"] == new_patch["series"]
        # Revision metadata stripped so save_many will accept the doc
        assert "revision" not in saved
        assert "latest_revision" not in saved
        assert "created" not in saved
        assert "last_modified" not in saved

    def test_patch_overrides_existing_field(self):
        existing = {
            "key": "/works/OL1W",
            "type": {"key": "/type/work"},
            "subjects": ["old"],
        }
        new_patch = {
            "key": "/works/OL1W",
            "type": {"key": "/type/work"},
            "subjects": ["new"],
        }

        with (
            patch("shelfie.cli._fetch_raw", return_value=existing),
            patch("shelfie.cli.infobase_save") as mock_save,
        ):
            _merge_save([new_patch])

        saved = mock_save.call_args[0][0][0]
        assert saved["subjects"] == ["new"]

    def test_fetch_failure_uses_empty_base(self):
        """If the existing doc can't be fetched, the patch still saves on its own
        (rather than exploding). This is a degraded path - not ideal but safer
        than crashing mid-batch."""
        new_patch = {
            "key": "/works/OL9999W",
            "type": {"key": "/type/work"},
            "series": [{"series": {"key": "/series/OL1L"}, "position": "1"}],
        }

        with (
            patch("shelfie.cli._fetch_raw", return_value=None),
            patch("shelfie.cli.infobase_save") as mock_save,
        ):
            _merge_save([new_patch])

        saved = mock_save.call_args[0][0][0]
        assert saved == new_patch


class TestCoverlessWorksFromSolr:
    def test_parses_solr_response(self):
        fake = {
            "response": {
                "docs": [
                    {"key": "/works/OL1W", "title": "Book 1", "author_name": ["Author A"]},
                    {"key": "/works/OL2W", "title": "Book 2"},
                ]
            }
        }
        with patch("shelfie.cli.solr_request", return_value=fake):
            assert _coverless_works_from_solr(limit=50) == [
                ("/works/OL1W", "Book 1", "Author A"),
                ("/works/OL2W", "Book 2", ""),
            ]

    def test_skips_docs_missing_key_or_title(self):
        fake = {
            "response": {
                "docs": [
                    {"key": "/works/OL1W"},
                    {"title": "Orphan"},
                    {"key": "/works/OL3W", "title": "Good", "author_name": ["A"]},
                ]
            }
        }
        with patch("shelfie.cli.solr_request", return_value=fake):
            assert _coverless_works_from_solr() == [("/works/OL3W", "Good", "A")]

    def test_empty_when_solr_unavailable(self):
        with patch("shelfie.cli.solr_request", return_value=None):
            assert _coverless_works_from_solr() == []

    def test_empty_when_response_missing(self):
        with patch("shelfie.cli.solr_request", return_value={}):
            assert _coverless_works_from_solr() == []


class TestFindProdCoverId:
    def _mock_response(self, docs):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"docs": docs}
        return resp

    def test_returns_first_cover_id(self):
        resp = self._mock_response([{"key": "/works/X", "cover_i": 42}])
        with patch("shelfie.cli.requests.get", return_value=resp) as mock_get:
            assert _find_prod_cover_id("Dune", "Herbert") == 42
        # Author should be passed when given
        assert mock_get.call_args.kwargs["params"]["author"] == "Herbert"

    def test_skips_docs_without_cover(self):
        resp = self._mock_response(
            [
                {"key": "/works/X"},
                {"key": "/works/Y", "cover_i": 99},
            ]
        )
        with patch("shelfie.cli.requests.get", return_value=resp):
            assert _find_prod_cover_id("Hi") == 99

    def test_none_when_no_docs(self):
        resp = self._mock_response([])
        with patch("shelfie.cli.requests.get", return_value=resp):
            assert _find_prod_cover_id("Hi") is None

    def test_none_when_no_doc_has_cover(self):
        resp = self._mock_response([{"key": "/works/X"}, {"key": "/works/Y"}])
        with patch("shelfie.cli.requests.get", return_value=resp):
            assert _find_prod_cover_id("Hi") is None

    def test_none_on_http_error(self):
        with patch("shelfie.cli.requests.get", side_effect=requests.RequestException):
            assert _find_prod_cover_id("Hi") is None

    def test_omits_author_param_when_empty(self):
        resp = self._mock_response([{"cover_i": 7}])
        with patch("shelfie.cli.requests.get", return_value=resp) as mock_get:
            _find_prod_cover_id("Solo Title")
        assert "author" not in mock_get.call_args.kwargs["params"]


class TestCmdPopulateCovers:
    def test_patches_only_works_with_cover_matches(self):
        targets = [
            ("/works/OL1W", "Found Book", "A"),
            ("/works/OL2W", "Missing Book", "B"),
        ]

        def fake_find(title, author=""):
            return 42 if title == "Found Book" else None

        with (
            patch("shelfie.cli._coverless_works_from_solr", return_value=targets),
            patch("shelfie.cli._find_prod_cover_id", side_effect=fake_find),
            patch("shelfie.cli._merge_save") as mock_save,
        ):
            cmd_populate_covers(ol=MagicMock())

        assert mock_save.call_count == 1
        (patches,) = mock_save.call_args[0]
        assert patches == [{"key": "/works/OL1W", "type": {"key": "/type/work"}, "covers": [42]}]

    def test_noop_when_no_coverless_works(self):
        with (
            patch("shelfie.cli._coverless_works_from_solr", return_value=[]),
            patch("shelfie.cli._find_prod_cover_id") as mock_find,
            patch("shelfie.cli._merge_save") as mock_save,
        ):
            cmd_populate_covers(ol=MagicMock())
        mock_find.assert_not_called()
        mock_save.assert_not_called()

    def test_continues_after_save_error(self):
        targets = [
            ("/works/OL1W", "Book 1", "A"),
            ("/works/OL2W", "Book 2", "B"),
        ]

        def fake_save(patches, comment=""):
            if patches[0]["key"] == "/works/OL1W":
                raise requests.RequestException("boom")

        with (
            patch("shelfie.cli._coverless_works_from_solr", return_value=targets),
            patch("shelfie.cli._find_prod_cover_id", return_value=7),
            patch("shelfie.cli._merge_save", side_effect=fake_save) as mock_save,
        ):
            cmd_populate_covers(ol=MagicMock())
        # Both works attempted even though the first raised.
        assert mock_save.call_count == 2


class TestGuessPassword:
    def test_admin_uses_default_login_password(self):
        assert _guess_password("admin") == "admin123"

    def test_openlibrary_uses_default_login_password(self):
        assert _guess_password("openlibrary") == "admin123"

    def test_unknown_username_returns_none(self):
        assert _guess_password("alice") is None
        assert _guess_password("") is None
        assert _guess_password("testuser_1") is None


class TestCmdListUsers:
    def test_lists_users_with_roles_and_password_hints(self, capsys):
        user_keys = ["/people/admin", "/people/bob", "/people/alice"]
        accounts = {
            "admin": {"email": "admin@example.com"},
            "bob": {"email": "bob@example.com"},
            "alice": {"email": "alice@example.com"},
        }
        roles = {
            "/people/admin": ["admin", "librarians"],
            "/people/bob": ["beta-testers"],
        }

        def find_account(username):
            return accounts.get(username)

        with (
            patch("shelfie.cli._infobase_keys_of_type", return_value=user_keys),
            patch("shelfie.cli._infobase_find_account", side_effect=find_account),
            patch("shelfie.cli._get_user_roles_map", return_value=roles),
        ):
            cmd_list_users(ol=MagicMock())

        out = capsys.readouterr().out
        # Each user is listed with its email.
        assert "admin" in out
        assert "admin@example.com" in out
        assert "bob@example.com" in out
        assert "alice@example.com" in out
        # Bootstrap admin shows its default password hint; others show "(hashed)".
        assert "admin123" in out
        assert "(hashed)" in out
        # Roles are joined; users with no roles show "-".
        assert "admin, librarians" in out
        assert "beta-testers" in out
        # Footer reports the count.
        assert "3 user(s) found" in out

    def test_handles_missing_account_lookup(self, capsys):
        """If /account/find fails for a user, email falls back to empty."""
        with (
            patch("shelfie.cli._infobase_keys_of_type", return_value=["/people/ghost"]),
            patch("shelfie.cli._infobase_find_account", return_value=None),
            patch("shelfie.cli._get_user_roles_map", return_value={}),
        ):
            cmd_list_users(ol=MagicMock())

        out = capsys.readouterr().out
        assert "ghost" in out
        assert "(hashed)" in out
        assert "1 user(s) found" in out

    def test_empty_when_no_users(self, capsys):
        with patch("shelfie.cli._infobase_keys_of_type", return_value=[]):
            cmd_list_users(ol=MagicMock())
        assert "No users found." in capsys.readouterr().out


def _max_id_response(keys):
    """Build the response object cmd_seed_series uses to discover existing series keys."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = keys
    return resp


class TestCmdSeedSeries:
    def _series_works(self, n):
        """Generate n minimal search docs that pass the >= 2-works threshold."""
        return [{"key": f"/works/W{i}", "title": f"Title {i}"} for i in range(n)]

    def test_creates_series_and_links_works(self, capsys):
        works = self._series_works(3)
        imported_keys = ["/works/OL10W", "/works/OL11W", "/works/OL12W"]

        with (
            patch("shelfie.cli.requests.get", return_value=_max_id_response([])),
            patch("shelfie.cli._fetch_series_works", return_value=works),
            patch("shelfie.cli._import_and_get_work_key", side_effect=imported_keys * 5),
            patch("shelfie.cli.infobase_save") as mock_infobase,
            patch("shelfie.cli._merge_save") as mock_merge,
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        # Series doc went through infobase_save (full new doc, not a partial).
        assert mock_infobase.call_count == 1
        (series_docs,) = mock_infobase.call_args[0]
        assert len(series_docs) == 1
        series_doc = series_docs[0]
        assert series_doc["type"] == {"key": "/type/series"}
        # Empty existing -> max_id stays 0, first key is OL1L.
        assert series_doc["key"] == "/series/OL1L"

        # Work-to-series links went through _merge_save (partial patch).
        assert mock_merge.call_count == 1
        (patches,) = mock_merge.call_args[0]
        assert [p["key"] for p in patches] == imported_keys
        assert all(p["series"][0]["series"]["key"] == "/series/OL1L" for p in patches)

        assert "1/1" in capsys.readouterr().out

    def test_max_id_advances_past_existing_series(self):
        existing = ["/series/OL3L", "/series/OL7L", "/series/OL5L", "/garbage/OLxL"]
        works = self._series_works(2)

        with (
            patch("shelfie.cli.requests.get", return_value=_max_id_response(existing)),
            patch("shelfie.cli._fetch_series_works", return_value=works),
            patch("shelfie.cli._import_and_get_work_key", side_effect=["/works/OL1W", "/works/OL2W"]),
            patch("shelfie.cli.infobase_save") as mock_infobase,
            patch("shelfie.cli._merge_save"),
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        # max_id = 7, succeeded = 0, so first new key is /series/OL8L.
        assert mock_infobase.call_args[0][0][0]["key"] == "/series/OL8L"

    def test_falls_back_to_safe_offset_when_infobase_unreachable(self):
        works = self._series_works(2)

        with (
            patch("shelfie.cli.requests.get", side_effect=requests.RequestException("infobase down")),
            patch("shelfie.cli._fetch_series_works", return_value=works),
            patch("shelfie.cli._import_and_get_work_key", side_effect=["/works/OL1W", "/works/OL2W"]),
            patch("shelfie.cli.infobase_save") as mock_infobase,
            patch("shelfie.cli._merge_save"),
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        # Fetch failure -> max_id = 100 safety buffer, so first new key is /series/OL101L.
        assert mock_infobase.call_args[0][0][0]["key"] == "/series/OL101L"

    def test_falls_back_to_safe_offset_on_http_error(self):
        """A 5xx from infobase must take the same path as a network error -
        regression for the missing raise_for_status() that let JSON-shaped
        error bodies through."""
        bad_resp = MagicMock()
        bad_resp.raise_for_status.side_effect = requests.HTTPError("500")

        works = self._series_works(2)
        with (
            patch("shelfie.cli.requests.get", return_value=bad_resp),
            patch("shelfie.cli._fetch_series_works", return_value=works),
            patch("shelfie.cli._import_and_get_work_key", side_effect=["/works/OL1W", "/works/OL2W"]),
            patch("shelfie.cli.infobase_save") as mock_infobase,
            patch("shelfie.cli._merge_save"),
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        assert mock_infobase.call_args[0][0][0]["key"] == "/series/OL101L"

    def test_skips_series_when_too_few_works_found(self, capsys):
        with (
            patch("shelfie.cli.requests.get", return_value=_max_id_response([])),
            patch("shelfie.cli._fetch_series_works", return_value=self._series_works(1)),
            patch("shelfie.cli._import_and_get_work_key") as mock_import,
            patch("shelfie.cli.infobase_save") as mock_infobase,
            patch("shelfie.cli._merge_save") as mock_merge,
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        mock_import.assert_not_called()
        mock_infobase.assert_not_called()
        mock_merge.assert_not_called()
        assert "0/1" in capsys.readouterr().out

    def test_skips_series_when_too_few_works_imported(self, capsys):
        with (
            patch("shelfie.cli.requests.get", return_value=_max_id_response([])),
            patch("shelfie.cli._fetch_series_works", return_value=self._series_works(3)),
            # Only one of three imports succeeds.
            patch("shelfie.cli._import_and_get_work_key", side_effect=["/works/OL1W", None, None]),
            patch("shelfie.cli.infobase_save") as mock_infobase,
            patch("shelfie.cli._merge_save") as mock_merge,
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        mock_infobase.assert_not_called()
        mock_merge.assert_not_called()
        assert "0/1" in capsys.readouterr().out

    def test_link_failure_reports_error_and_does_not_count_as_success(self, capsys):
        """Regression: a series whose work-links fail must not be reported as
        successful, and must surface an error so the user knows the series doc
        is orphaned. Previously this was silently swallowed by contextlib.suppress."""
        works = self._series_works(2)

        with (
            patch("shelfie.cli.requests.get", return_value=_max_id_response([])),
            patch("shelfie.cli._fetch_series_works", return_value=works),
            patch("shelfie.cli._import_and_get_work_key", side_effect=["/works/OL1W", "/works/OL2W"]),
            patch("shelfie.cli.infobase_save"),
            patch("shelfie.cli._merge_save", side_effect=requests.RequestException("infobase 500")),
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        out = capsys.readouterr().out
        # Error is surfaced and the orphan series key is named.
        assert "Linking works" in out
        assert "/series/OL1L" in out
        # Final tally must NOT count this as a success.
        assert "0/1" in out

    def test_series_creation_failure_skips_to_next(self, capsys):
        works = self._series_works(2)

        with (
            patch("shelfie.cli.requests.get", return_value=_max_id_response([])),
            patch("shelfie.cli._fetch_series_works", return_value=works),
            patch("shelfie.cli._import_and_get_work_key", side_effect=["/works/OL1W", "/works/OL2W"]),
            patch("shelfie.cli.infobase_save", side_effect=requests.RequestException("boom")),
            patch("shelfie.cli._merge_save") as mock_merge,
        ):
            cmd_seed_series(ol=MagicMock(), count=1)

        # If the series doc itself can't be saved, don't attempt to link works to a non-existent key.
        mock_merge.assert_not_called()
        assert "0/1" in capsys.readouterr().out


class TestCmdAddBooks:
    def test_production_source_imports_fetched_records(self):
        records = [{"title": "A", "authors": [{"name": "X"}], "source_records": ["s:1"]}]

        with (
            patch("shelfie.cli._fetch_books_from_prod", return_value=records),
            patch("shelfie.cli._import_books", return_value=(1, 0)) as mock_import,
        ):
            n = cmd_add_books(ol=MagicMock(), count=1, source="production")

        assert n == 1
        passed_records = mock_import.call_args[0][1]
        assert passed_records == records

    def test_falls_back_to_seed_when_prod_unreachable(self, capsys):
        with (
            patch("shelfie.cli._fetch_books_from_prod", return_value=[]),
            patch("shelfie.cli._import_books", return_value=(2, 0)) as mock_import,
        ):
            cmd_add_books(ol=MagicMock(), count=2, source="production")

        # Imported from seed_data/books.json on fallback.
        assert mock_import.call_count == 1
        records = mock_import.call_args[0][1]
        assert len(records) == 2
        assert all(r["source_records"][0].startswith("shelfie:seed-") for r in records)
        assert "Falling back to seed data" in capsys.readouterr().out

    def test_explicit_seed_source_does_not_hit_network(self):
        with (
            patch("shelfie.cli._fetch_books_from_prod") as mock_fetch,
            patch("shelfie.cli._import_books", return_value=(3, 0)) as mock_import,
        ):
            cmd_add_books(ol=MagicMock(), count=3, source="seed")

        mock_fetch.assert_not_called()
        records = mock_import.call_args[0][1]
        assert len(records) == 3


class TestCmdSetRole:
    def _ol_with(self, user_doc=None, group_doc=None, missing=()):
        """Build an OLClient mock that returns user/group docs from .get(key)."""
        ol = MagicMock()
        docs = {}
        if user_doc is not None:
            docs[user_doc["key"]] = user_doc
        if group_doc is not None:
            docs[group_doc["key"]] = group_doc

        def get(key, v=None):
            if key in missing:
                from shelfie.client import OLError
                err = MagicMock()
                err.response.status_code = 404
                err.response.headers = {}
                err.response.text = "not found"
                raise OLError(err)
            return docs.get(key, {})

        ol.get.side_effect = get
        return ol

    def test_add_user_to_group_writes_full_doc(self):
        user = {"key": "/people/alice", "displayname": "Alice"}
        group = {"key": "/usergroup/admin", "type": {"key": "/type/usergroup"}, "members": []}
        ol = self._ol_with(user_doc=user, group_doc=group)

        with patch("shelfie.cli.infobase_save") as mock_save:
            cmd_set_role(ol, username="alice", role="admin", action="add")

        (docs,) = mock_save.call_args[0]
        assert len(docs) == 1
        saved = docs[0]
        # Full doc replacement is correct here - usergroups have no other fields shelfie cares about.
        assert saved["key"] == "/usergroup/admin"
        assert saved["type"] == {"key": "/type/usergroup"}
        assert saved["members"] == [{"key": "/people/alice"}]

    def test_remove_user_from_group(self):
        user = {"key": "/people/alice", "displayname": "Alice"}
        group = {
            "key": "/usergroup/admin",
            "type": {"key": "/type/usergroup"},
            "members": [{"key": "/people/alice"}, {"key": "/people/bob"}],
        }
        ol = self._ol_with(user_doc=user, group_doc=group)

        with patch("shelfie.cli.infobase_save") as mock_save:
            cmd_set_role(ol, username="alice", role="admin", action="remove")

        saved = mock_save.call_args[0][0][0]
        assert saved["members"] == [{"key": "/people/bob"}]

    def test_noop_when_already_member(self, capsys):
        user = {"key": "/people/alice"}
        group = {
            "key": "/usergroup/admin",
            "type": {"key": "/type/usergroup"},
            "members": [{"key": "/people/alice"}],
        }
        ol = self._ol_with(user_doc=user, group_doc=group)

        with patch("shelfie.cli.infobase_save") as mock_save:
            cmd_set_role(ol, username="alice", role="admin", action="add")

        mock_save.assert_not_called()
        assert "already in" in capsys.readouterr().out

    def test_noop_when_removing_non_member(self, capsys):
        user = {"key": "/people/alice"}
        group = {
            "key": "/usergroup/admin",
            "type": {"key": "/type/usergroup"},
            "members": [{"key": "/people/bob"}],
        }
        ol = self._ol_with(user_doc=user, group_doc=group)

        with patch("shelfie.cli.infobase_save") as mock_save:
            cmd_set_role(ol, username="alice", role="admin", action="remove")

        mock_save.assert_not_called()
        assert "not in" in capsys.readouterr().out

    def test_unknown_user_aborts_before_save(self, capsys):
        ol = self._ol_with(missing=["/people/ghost"])

        with patch("shelfie.cli.infobase_save") as mock_save:
            cmd_set_role(ol, username="ghost", role="admin", action="add")

        mock_save.assert_not_called()
        assert "not found" in capsys.readouterr().out

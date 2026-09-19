"""Tests for the download workflow: planning, enrichment, failure records, and full offline runs."""

import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET
import zipfile

from ao3archiver import ao3_client, bookorbit, download
from ao3archiver.ao3_client import AO3FetchRecord
from ao3archiver.metadata import (
    AO3Metadata,
    enrich_epub,
    epub_rating_subjects,
    has_ao3_metadata,
    read_ao3_metadata,
    validate_epub_file,
)
from ao3archiver.run_log import configure_logging, register_secret, RunInterrupted
from tests.support import (
    CREDENTIALS,
    epub_bytes,
    epub_reply,
    front_page,
    html,
    MYSTERY_PAGE,
    RoutedSession,
    serve_work,
    sign_in_replies,
    work_page,
)


def write_links(directory, *urls, name="all.txt"):
    path = Path(directory) / name
    path.write_text("\n".join(urls) + "\n", encoding="utf-8")
    return path


def links_for(*work_ids):
    return [f"https://archiveofourown.org/works/{work_id}" for work_id in work_ids]


def calibre_columns(epub_path: Path) -> dict[str, object]:
    with zipfile.ZipFile(epub_path) as archive:
        package = ET.fromstring(archive.read("content.opf"))
    metadata = next(child for child in package if child.tag.rsplit("}", 1)[-1] == "metadata")
    return {
        child.attrib["name"].rsplit(":#", 1)[-1]: json.loads(child.attrib["content"])["#value#"]
        for child in metadata
        if child.attrib.get("name", "").startswith("calibre:user_metadata:")
    }


class LinkInventoryTest(unittest.TestCase):
    def test_duplicate_links_collapse_to_one_target(self):
        with tempfile.TemporaryDirectory() as directory:
            write_links(
                directory,
                "https://archiveofourown.org/works/111",
                "https://archiveofourown.org/works/222",
                "https://archiveofourown.org/works/111",
                "https://archiveofourown.org/works/111/chapters/9",
            )
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual([target.work_id for target in inventory.targets], ["111", "222"])
        self.assertEqual(inventory.lines, 4)
        self.assertEqual(inventory.duplicates, 2)

    def test_unparseable_lines_are_reported_not_silently_dropped(self):
        with tempfile.TemporaryDirectory() as directory:
            write_links(directory, "https://archiveofourown.org/works/111", "not-a-url",
                        "https://archiveofourown.org/series/5")
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual(len(inventory.targets), 1)
        self.assertEqual(inventory.invalid, ("not-a-url", "https://archiveofourown.org/series/5"))

    def test_links_from_several_files_are_merged_and_attributed(self):
        with tempfile.TemporaryDirectory() as directory:
            write_links(directory, "https://archiveofourown.org/works/111", name="a.txt")
            write_links(directory, "https://archiveofourown.org/works/222", name="b.txt")
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual(inventory.files, 2)
        self.assertEqual({target.source for target in inventory.targets}, {"a.txt", "b.txt"})

    def test_an_empty_directory_yields_no_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            inventory = download.load_link_inventory(Path(directory))

        self.assertEqual(inventory.targets, ())
        self.assertEqual(inventory.lines, 0)


class FailureLogTest(unittest.TestCase):
    def test_the_latest_entry_per_work_wins(self):
        with tempfile.TemporaryDirectory() as directory:
            log = download.FailureLog(Path(directory) / "failures.jsonl")
            log.record("1", stage="download", error_type="NetworkStopError", message="older", permanent=False)
            log.record("1", stage="page", error_type="Unavailable", message="newer", permanent=True)

            entry = log.latest()["1"]

        self.assertEqual((entry.stage, entry.error, entry.permanent), ("page", "newer", True))

    def test_only_permanent_entries_are_skipped_by_later_runs(self):
        with tempfile.TemporaryDirectory() as directory:
            log = download.FailureLog(Path(directory) / "failures.jsonl")
            log.record("1", stage="page", error_type="Unavailable", message="HTTP 404", permanent=True)
            log.record("2", stage="download", error_type="NetworkStopError", message="blip", permanent=False)

            self.assertEqual(log.permanently_unavailable(), frozenset({"1"}))

    def test_a_torn_final_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            log = download.FailureLog(Path(directory) / "failures.jsonl")
            log.record("1", stage="page", error_type="Unavailable", message="gone", permanent=True)
            with log.path.open("a", encoding="utf-8") as stream:
                stream.write('{"work_id": "2", "sta')

            self.assertEqual(list(log.latest()), ["1"])

    def test_a_missing_log_is_empty(self):
        self.assertEqual(download.FailureLog(Path("/definitely-missing.jsonl")).latest(), {})

    def test_registered_secrets_are_scrubbed_before_persisting(self):
        with tempfile.TemporaryDirectory() as directory:
            run = configure_logging("unit-dl-redaction", log_dir=Path(directory), stream=io.StringIO(),
                                    root_name="ao3")
            try:
                register_secret("sekrit-reader")
                log = download.FailureLog(Path(directory) / "failures.jsonl")
                log.record("1", stage="page", error_type="AuthenticationFailure",
                           message="redirected to /users/sekrit-reader", permanent=False)
                persisted = log.path.read_text(encoding="utf-8")
            finally:
                for handler in list(run.logger.handlers):
                    run.logger.removeHandler(handler)
                    handler.close()

        self.assertNotIn("sekrit-reader", persisted)


class PlanWorkTest(unittest.TestCase):
    def plan(self, directory, work_ids, **options):
        write_links(directory, *links_for(*work_ids))
        inventory = download.load_link_inventory(Path(directory))
        output = Path(directory) / "out"
        output.mkdir(exist_ok=True)
        return download.plan_work(inventory, output, **options), output

    def test_a_missing_file_is_planned_as_a_download(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, counts), _ = self.plan(directory, ["111"])

        self.assertEqual([(p.target.work_id, p.action) for p in planned], [("111", "download")])
        self.assertEqual(counts.complete, 0)

    def test_an_unenriched_existing_file_is_planned_as_an_enrich(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            (output / "111.epub").write_bytes(epub_bytes())
            (planned, _), _ = self.plan(directory, ["111"])

        self.assertEqual([p.action for p in planned], ["enrich"])

    def test_a_fully_enriched_file_is_skipped_until_refresh(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            epub = output / "111.epub"
            epub.write_bytes(epub_bytes())
            metadata = AO3Metadata("111", "https://archiveofourown.org/works/111", kudos=5)
            download.enrich_for_calibre(epub, download.with_local_metrics(metadata, epub))

            (planned, counts), _ = self.plan(directory, ["111"])
            (refreshed, _), _ = self.plan(directory, ["111"], refresh=True)

        self.assertEqual(planned, ())
        self.assertEqual(counts.complete, 1)
        self.assertEqual([p.action for p in refreshed], ["enrich"])

    def test_a_file_from_the_old_downloader_without_calibre_columns_is_re_enriched(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            epub = output / "111.epub"
            epub.write_bytes(epub_bytes())
            enrich_epub(epub, AO3Metadata("111", "https://archiveofourown.org/works/111", kudos=5))
            self.assertTrue(has_ao3_metadata(epub))

            (planned, _), _ = self.plan(directory, ["111"])

        self.assertEqual([p.action for p in planned], ["enrich"])

    def test_a_corrupt_existing_file_is_re_downloaded_rather_than_trusted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "out"
            output.mkdir()
            (output / "111.epub").write_bytes(b"not an epub")
            (planned, _), _ = self.plan(directory, ["111"])

        self.assertEqual([p.action for p in planned], ["download"])

    def test_unavailable_works_are_skipped_and_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, counts), _ = self.plan(directory, ["111", "222"], skip_work_ids=frozenset({"111"}))

        self.assertEqual([p.target.work_id for p in planned], ["222"])
        self.assertEqual(counts.unavailable, 1)

    def test_metadata_only_never_plans_a_download(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, counts), _ = self.plan(directory, ["111"], metadata_only=True)

        self.assertEqual(planned, ())
        self.assertEqual(counts.missing_file, 1)

    def test_the_limit_bounds_the_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            (planned, _), _ = self.plan(directory, ["111", "222", "333"], limit=2)

        self.assertEqual(len(planned), 2)


class EstimateTest(unittest.TestCase):
    def test_a_download_costs_two_paced_requests_and_an_enrich_one(self):
        target = download.WorkTarget("1", "u", "all.txt")
        planned = [
            download.PlannedWork(target, Path("1.epub"), "download"),
            download.PlannedWork(target, Path("1.epub"), "enrich"),
        ]

        self.assertEqual(download.estimate_seconds(planned, 10.0), 30.0)

    def test_nothing_planned_takes_no_time(self):
        self.assertEqual(download.estimate_seconds([], 10.0), 0.0)


class SaveEpubTest(unittest.TestCase):
    def test_invalid_bytes_are_not_saved_and_leave_no_temporary_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "111.epub"

            with self.assertRaises(Exception):
                download.save_epub_atomically(b"<html>Cloudflare challenge</html>", destination)

            self.assertFalse(destination.exists())
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_valid_bytes_replace_the_destination(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "111.epub"
            destination.write_bytes(b"old")

            download.save_epub_atomically(epub_bytes(), destination)

            validate_epub_file(destination)

    def test_a_saved_book_is_readable_by_others_as_the_umask_allows(self):
        """A temporary file is 0600; a book sent to a server must be readable by its container."""

        with tempfile.TemporaryDirectory() as directory:
            modes = {}
            for mask in (0o022, 0o077):
                previous = os.umask(mask)
                try:
                    destination = Path(directory) / f"{mask:o}.epub"
                    download.save_epub_atomically(epub_bytes(), destination)
                finally:
                    os.umask(previous)
                modes[mask] = stat.S_IMODE(destination.stat().st_mode)

        self.assertEqual(modes, {0o022: 0o644, 0o077: 0o600})


class EnrichForCalibreTest(unittest.TestCase):
    def test_every_column_including_local_words_and_gfog_is_baked_in(self):
        with tempfile.TemporaryDirectory() as directory:
            epub = Path(directory) / "111.epub"
            epub.write_bytes(epub_bytes())
            page = ao3_client.FetchedWorkPage(
                record=AO3FetchRecord(
                    work_id="111", work_url="https://archiveofourown.org/works/111",
                    fetched_at="2026-01-01T00:00:00+00:00", availability="ok",
                ),
                html=work_page("111"),
            )
            metadata = download.with_local_metrics(download.metadata_from_page(page, "111"), epub)

            download.enrich_for_calibre(epub, metadata)
            columns = calibre_columns(epub)
            stored = read_ao3_metadata(epub)

        self.assertEqual(columns["ao3_kudos"], 68)
        self.assertEqual(columns["ao3_hits"], 1040)
        self.assertEqual(columns["ao3_bookmarks"], 6)
        self.assertEqual(columns["ao3_comments"], 4)
        self.assertEqual(columns["ao3_words"], 1315)
        self.assertEqual(columns["ao3_chapters"], "3/3")
        self.assertEqual(columns["ao3_category"], "F/F")
        self.assertEqual(columns["words"], metadata.local_words)
        self.assertEqual(columns["gfog"], metadata.local_gfog)
        self.assertIsNotNone(metadata.local_words)
        self.assertEqual(stored.published, "2018-06-01")


class DownloadHarness:
    """A links folder, an output folder and a fake AO3, for whole offline runs."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.links = self.root / "links"
        self.links.mkdir()
        self.output = self.root / "out"
        self.failure_log = self.root / "failures.jsonl"
        self.lock = self.root / "ao3-cache.jsonl.fetch.lock"
        self.session = RoutedSession()
        self.credentials_loaded = 0

    def tearDown(self):
        self._directory.cleanup()

    def load_credentials(self):
        self.credentials_loaded += 1
        return CREDENTIALS

    def run_downloads(self, **options):
        def factory(credentials, delay, sleep_fn):
            return ao3_client.AO3DownloadClient(
                credentials, delay_seconds=delay, sleep_fn=lambda _: None, session=self.session
            )

        options.setdefault("harvesters", lambda: ())
        return download.run_downloads(
            self.links,
            self.output,
            failure_log_path=self.failure_log,
            lock_path=self.lock,
            credentials_loader=self.load_credentials,
            client_factory=factory,
            sleep_fn=lambda _: None,
            **options,
        )


class RunDownloadsTest(DownloadHarness, unittest.TestCase):
    def test_a_work_is_downloaded_enriched_and_ready_for_calibre(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        serve_work(self.session, "111")
        self.session.add(
            "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
            epub_reply(),
        )

        summary = self.run_downloads()

        epub = self.output / "111.epub"
        self.assertEqual(summary.outcomes, {"downloaded": 1})
        self.assertTrue(download.is_complete(epub))
        columns = calibre_columns(epub)
        self.assertEqual(columns["ao3_kudos"], 68)
        self.assertIn("words", columns)
        self.assertIn("gfog", columns)
        self.assertEqual(self.session.requests, [
            "GET https://archiveofourown.org/token_dispenser.json",
            "POST https://archiveofourown.org/users/login",
            "GET https://archiveofourown.org/works/111",
            "GET https://archiveofourown.org/works/111/chapters/9",
            "GET https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
        ])
        self.assertTrue(self.session.closed)

    def test_the_rating_on_the_work_page_becomes_a_tag(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        serve_work(self.session, "111", work_page("111", rating="Mature"))
        self.session.add(
            "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
            epub_reply(),
        )

        summary = self.run_downloads()

        self.assertEqual(epub_rating_subjects(self.output / "111.epub"), ("Mature",))
        self.assertEqual(summary.without_rating, ())
        self.assertEqual(summary.outcomes, {"downloaded": 1})

    def test_a_rating_missing_from_the_page_is_read_from_the_downloaded_file(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        serve_work(self.session, "111")  # an AO3 page without a rating row
        self.session.add(
            "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
            epub_reply(rating_page=front_page("Explicit")),
        )

        summary = self.run_downloads()

        self.assertEqual(epub_rating_subjects(self.output / "111.epub"), ("Explicit",))
        self.assertEqual(summary.without_rating, ())
        self.assertEqual(
            [request for request in self.session.requests if "/works/" in request],
            [
                "GET https://archiveofourown.org/works/111",
                "GET https://archiveofourown.org/works/111/chapters/9",
            ],
            "the rating must cost no extra AO3 request",
        )

    def test_a_work_with_no_rating_anywhere_is_still_saved_and_reported(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        serve_work(self.session, "111")
        self.session.add(
            "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
            epub_reply(),
        )

        summary = self.run_downloads()

        self.assertEqual(summary.outcomes, {"downloaded": 1}, "the work is saved regardless")
        self.assertEqual(summary.without_rating, ("111",))
        self.assertEqual(summary.still_failed, {})
        self.assertTrue(download.is_complete(self.output / "111.epub"))
        self.assertFalse(self.failure_log.exists(), "a missing rating is not a failure")

    def test_an_unavailable_work_is_never_downloaded_and_is_skipped_next_time(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        self.session.add("https://archiveofourown.org/works/111", html(200, MYSTERY_PAGE))

        summary = self.run_downloads()
        again = self.run_downloads(dry_run=True)
        retried = self.run_downloads(dry_run=True, retry_unavailable=True)

        self.assertEqual(summary.outcomes, {"unavailable": 1})
        self.assertFalse(any("/downloads/" in request for request in self.session.requests))
        self.assertTrue(download.FailureLog(self.failure_log).latest()["111"].permanent)
        self.assertEqual(again.planned, 0)
        self.assertEqual(retried.planned, 1)

    def test_an_existing_unenriched_file_is_enriched_without_downloading(self):
        write_links(self.links, *links_for("111"))
        self.output.mkdir()
        (self.output / "111.epub").write_bytes(epub_bytes())
        sign_in_replies(self.session)
        serve_work(self.session, "111")

        summary = self.run_downloads()

        self.assertEqual(summary.outcomes, {"enriched": 1})
        self.assertFalse(any("/downloads/" in request for request in self.session.requests))
        self.assertTrue(download.is_complete(self.output / "111.epub"))

    def test_a_failed_work_does_not_stop_the_others_and_is_retried_next_run(self):
        write_links(self.links, *links_for("111", "222"))
        sign_in_replies(self.session)
        bad_download = "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full"
        for _ in range(2):  # the main pass and the retry pass
            serve_work(self.session, "111")
            self.session.add(bad_download, html(200, "<title>Please try again later</title>"))
        serve_work(self.session, "222")
        self.session.add(
            "https://archiveofourown.org/downloads/222/Example.epub?updated_at=1700000000&view=full",
            epub_reply(),
        )

        summary = self.run_downloads()
        next_plan = self.run_downloads(dry_run=True)

        self.assertEqual(summary.outcomes, {"failed": 2, "downloaded": 1})
        self.assertEqual(list(summary.still_failed), ["111"])
        self.assertFalse((self.output / "111.epub").exists())
        entry = download.FailureLog(self.failure_log).latest()["111"]
        self.assertEqual((entry.stage, entry.permanent), ("download", False))
        self.assertEqual(next_plan.planned, 1, "a non-permanent failure is retried next run")

    def test_a_dry_run_makes_no_requests_and_needs_no_credentials(self):
        write_links(self.links, *links_for("111", "111", "222"))

        summary = self.run_downloads(dry_run=True)

        self.assertEqual(summary.planned, 2)
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.credentials_loaded, 0)

    def test_a_dry_run_reports_the_plan(self):
        write_links(self.links, *links_for("111", "111", "222"))
        stream = io.StringIO()
        run = configure_logging("unit-download-plan", log_dir=self.root, stream=stream, root_name="ao3")
        try:
            self.run_downloads(dry_run=True)
        finally:
            for handler in list(run.logger.handlers):
                run.logger.removeHandler(handler)
                handler.close()

        output = stream.getvalue()
        # Label padding depends on the longest label, so match label and value.
        self.assertRegex(output, r"unique works\s+2\n")
        self.assertRegex(output, r"duplicate lines\s+1\n")
        self.assertRegex(output, r"to download\s+2\n")
        self.assertIn("would download: work=111", output)

    def test_declining_the_confirmation_makes_no_requests_at_all(self):
        """The question comes after the plan and before the first request."""

        write_links(self.links, *links_for("111", "222"))

        summary = self.run_downloads(confirm_start=lambda: False)

        self.assertEqual(summary.planned, 2)
        self.assertEqual(summary.outcomes, {})
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.credentials_loaded, 0)
        self.assertFalse(self.output.exists())

    def test_the_confirmation_is_asked_once_and_only_when_there_is_work(self):
        asked = []
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)
        serve_work(self.session, "111")
        self.session.add(
            "https://archiveofourown.org/downloads/111/Example.epub?updated_at=1700000000&view=full",
            epub_reply(),
        )

        self.run_downloads(confirm_start=lambda: asked.append(True) or True)

        self.assertEqual(len(asked), 1)

    def test_nothing_planned_is_never_worth_a_question(self):
        self.run_downloads(confirm_start=lambda: self.fail("asked with nothing to do"))

    def test_an_empty_links_directory_stops_before_signing_in(self):
        summary = self.run_downloads()

        self.assertEqual(summary.planned, 0)
        self.assertEqual(self.credentials_loaded, 0)

    def test_a_real_run_refuses_to_start_while_another_run_holds_the_account(self):
        write_links(self.links, *links_for("111"))

        with ao3_client.account_lock(self.lock):
            with self.assertRaises(ao3_client.AnotherRunActive):
                self.run_downloads()

        self.assertEqual(self.credentials_loaded, 0)
        self.assertEqual(self.session.requests, [])

    def test_an_interrupt_is_not_swallowed_as_a_work_failure(self):
        write_links(self.links, *links_for("111"))
        sign_in_replies(self.session)

        def interrupted(url, timeout=None, allow_redirects=True):
            if "/works/" in url:
                raise RunInterrupted("signal")
            return RoutedSession.get(self.session, url, timeout, allow_redirects)

        self.session.get = interrupted

        with self.assertRaises(RunInterrupted):
            self.run_downloads()

        self.assertFalse(self.failure_log.exists())



def owned_library(**chapters_by_work):
    """What BookOrbit would report holding: each work with its AO3 Chapters value."""

    rows = [
        {"bookId": index, "filePaths": [f"/books/fanfiction/{work_id}.epub"], "custom.ao3_chapters": chapters}
        for index, (work_id, chapters) in enumerate(chapters_by_work.items(), start=1)
    ]
    fields = [bookorbit.CustomField(8, "AO3 Chapters", "text", "ao3_chapters")]
    return bookorbit.owned_from_rows(rows, fields)


EPUB_URL = "https://archiveofourown.org/downloads/{work_id}/Example.epub?updated_at=1700000000&view=full"


class SplitOwnedTest(unittest.TestCase):
    def planned(self, *work_ids, action="download"):
        return [
            download.PlannedWork(download.WorkTarget(work_id, f"u{work_id}", "all.txt"), Path(f"{work_id}.epub"), action)
            for work_id in work_ids
        ]

    def test_owned_works_are_sorted_by_their_chapter_count(self):
        split = download.split_owned(
            self.planned("1", "2", "3", "4", "5"),
            owned_library(**{"2": "3/?", "3": "3/10", "4": "12/12", "5": None}),
        )

        self.assertEqual([item.target.work_id for item in split.kept], ["1"])
        self.assertEqual([(item.target.work_id, item.action, item.owned_chapters) for item in split.in_progress],
                         [("2", "update", "3/?"), ("3", "update", "3/10")])
        self.assertEqual([item.target.work_id for item in split.complete], ["4"])
        self.assertEqual([item.target.work_id for item in split.unknown], ["5"])

    def test_a_work_bookorbit_holds_twice_is_owned_not_new(self):
        rows = [
            {"bookId": 1, "filePaths": ["/books/fanfiction/2.epub"], "custom.ao3_chapters": "3/?"},
            {"bookId": 2, "filePaths": ["/books/fanfiction/Old/Copy (2)/Copy.epub"],
             "custom.ao3_link": "https://archiveofourown.org/works/2", "custom.ao3_chapters": "3/?"},
        ]
        fields = [bookorbit.CustomField(7, "AO3 Link", "url", "ao3_link"),
                  bookorbit.CustomField(8, "AO3 Chapters", "text", "ao3_chapters")]

        split = download.split_owned(self.planned("2"), bookorbit.owned_from_rows(rows, fields))

        self.assertEqual(split.kept, ())
        self.assertEqual([item.target.work_id for item in split.unknown], ["2"],
                         "which copy's chapter count counts is unclear, so it is asked about with the uncounted ones")

    def test_a_file_already_in_downloaded_is_not_looked_up(self):
        split = download.split_owned(self.planned("2", action="enrich"), owned_library(**{"2": "12/12"}))

        self.assertEqual([item.action for item in split.kept], ["enrich"])

    def test_without_an_owned_list_everything_is_new(self):
        split = download.split_owned(self.planned("1", "2"), None)

        self.assertEqual(len(split.kept), 2)

    def test_more_chapters_is_new_and_an_unreadable_count_is_treated_as_new(self):
        self.assertTrue(download.has_new_chapters("3/?", "4/?"))
        self.assertFalse(download.has_new_chapters("3/?", "3/?"))
        self.assertFalse(download.has_new_chapters("3/?", "3/10"), "a new total is not a new chapter")
        self.assertTrue(download.has_new_chapters("3/?", None))


class OwnedWorksRunTest(DownloadHarness, unittest.TestCase):
    """The plan asks BookOrbit what is already on the server, before any AO3 request."""

    def setUp(self):
        super().setUp()
        self.questions = []
        self.stream = io.StringIO()
        run = configure_logging("unit-download-owned", log_dir=self.root, stream=self.stream, root_name="ao3")
        self.addCleanup(lambda: [run.logger.removeHandler(h) or h.close() for h in list(run.logger.handlers)])

    def answers(self, **by_default):
        """Answer the opening questions: ``unfinished`` for the first, ``finished`` for the second."""

        def ask(question, default):
            self.questions.append(question)
            return by_default["unfinished" if "unfinished" in question else "finished"]

        return ask

    def serve_page(self, work_id, chapters):
        serve_work(self.session, work_id, work_page(work_id, chapters=chapters))

    def test_declining_the_chapter_check_asks_ao3_nothing(self):
        write_links(self.links, *links_for("111"))

        summary = self.run_downloads(
            owned_loader=lambda: owned_library(**{"111": "3/?"}), ask=self.answers(unfinished=False, finished=False)
        )

        self.assertEqual(summary.planned, 0)
        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.credentials_loaded, 0)

    def test_each_unfinished_work_costs_one_page_and_downloads_only_when_it_grew(self):
        write_links(self.links, *links_for("111", "222"))
        sign_in_replies(self.session)
        self.serve_page("111", "3/?")
        self.serve_page("222", "5/10")
        self.session.add(EPUB_URL.format(work_id="222"), epub_reply())

        summary = self.run_downloads(
            owned_loader=lambda: owned_library(**{"111": "3/?", "222": "3/10"}),
            ask=self.answers(unfinished=True, finished=False),
        )

        self.assertEqual(summary.outcomes, {"unchanged": 1, "updated": 1})
        self.assertFalse((self.output / "111.epub").exists(), "no new chapters, no file")
        self.assertTrue(download.is_complete(self.output / "222.epub"))
        self.assertEqual(
            [request for request in self.session.requests if "/downloads/" in request],
            ["GET " + EPUB_URL.format(work_id="222")],
        )
        self.assertRegex(self.stream.getvalue(), r"to check for new chapters\s+2\n")

    def test_finished_works_are_downloaded_again_only_on_a_yes(self):
        write_links(self.links, *links_for("333", "444"))
        owned = owned_library(**{"333": "12/12", "444": None})

        declined = self.run_downloads(owned_loader=lambda: owned, ask=self.answers(unfinished=True, finished=False))

        self.assertEqual(declined.planned, 0)
        self.assertEqual(self.session.requests, [])
        self.assertIn("2 of these works are on the server and finished, or have no chapter count", self.questions[0])

        sign_in_replies(self.session)
        for work_id in ("333", "444"):
            serve_work(self.session, work_id)
            self.session.add(EPUB_URL.format(work_id=work_id), epub_reply())

        accepted = self.run_downloads(owned_loader=lambda: owned, ask=self.answers(unfinished=True, finished=True))

        self.assertEqual(accepted.outcomes, {"downloaded": 2})

    def test_a_question_with_nothing_behind_it_is_never_asked(self):
        write_links(self.links, *links_for("111"))

        self.run_downloads(owned_loader=lambda: owned_library(), ask=self.answers(), dry_run=True)
        self.run_downloads(owned_loader=lambda: owned_library(), ask=self.answers(), confirm_start=lambda: False)

        self.assertEqual(self.questions, [])

    def test_a_dry_run_takes_the_defaults_without_asking(self):
        write_links(self.links, *links_for("111", "333"))

        summary = self.run_downloads(
            owned_loader=lambda: owned_library(**{"111": "3/?", "333": "12/12"}),
            ask=lambda question, default: self.fail(f"asked in a dry run: {question}"),
            dry_run=True,
        )

        self.assertEqual(summary.planned, 1, "the unfinished work is checked; the finished one is not")
        self.assertIn("would update: work=111", self.stream.getvalue())

    def test_without_bookorbit_the_plan_says_every_work_is_new(self):
        write_links(self.links, *links_for("111"))

        summary = self.run_downloads(owned_loader=lambda: None, dry_run=True)

        self.assertEqual(summary.planned, 1)
        self.assertIn("BookOrbit is not configured, so every work is new", self.stream.getvalue())

    def test_an_unreachable_bookorbit_stops_before_any_ao3_request(self):
        write_links(self.links, *links_for("111"))

        def unreachable():
            raise bookorbit.BookOrbitError("Could not reach BookOrbit at http://books.example/api/v1")

        with self.assertRaises(bookorbit.BookOrbitError):
            self.run_downloads(owned_loader=unreachable)

        self.assertEqual(self.session.requests, [])
        self.assertEqual(self.credentials_loaded, 0)

    def test_a_file_waiting_in_downloaded_needs_no_lookup_at_all(self):
        write_links(self.links, *links_for("111"))
        self.output.mkdir()
        epub = self.output / "111.epub"
        epub.write_bytes(epub_bytes())
        download.enrich_for_calibre(epub, AO3Metadata("111", "https://archiveofourown.org/works/111", kudos=5))

        self.run_downloads(owned_loader=lambda: self.fail("BookOrbit was asked"), dry_run=True)

    def test_a_running_harvester_is_warned_about(self):
        write_links(self.links, *links_for("111"))

        self.run_downloads(dry_run=True, harvesters=lambda: ("4242 python3 ao3downloader.py",))

        self.assertIn("ao3downloader is running (4242 python3 ao3downloader.py)", self.stream.getvalue())


class MainTest(unittest.TestCase):
    def test_yes_never_means_download_everything_again(self):
        captured = {}

        def fake_run_downloads(*args, **options):
            captured.update(options)
            return download.RunSummary(planned=0, outcomes={}, still_failed={})

        with tempfile.TemporaryDirectory() as directory, patch.object(download, "run_downloads", fake_run_downloads):
            download.main(["--yes", "--log-dir", directory])
            for handler in list(download.LOGGER.parent.handlers):
                download.LOGGER.parent.removeHandler(handler)
                handler.close()

        ask = captured["ask"]
        self.assertFalse(ask("Download them again anyway?", False))
        self.assertTrue(ask("Check AO3 for new chapters?", True))
        self.assertTrue(captured["confirm_start"]())
        self.assertIs(captured["owned_loader"], download.load_owned_works)


if __name__ == "__main__":
    unittest.main()

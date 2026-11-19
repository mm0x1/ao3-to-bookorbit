from pathlib import Path
import tempfile
import unittest

from ao3archiver import ao3_client, bookorbit, download, harvest
from ao3archiver.ao3_client import AnotherRunActive, account_lock
from ao3archiver.harvest import Blurb, HarvestError, Library, Search
from ao3archiver.run_log import RunInterrupted
from tests.support import (
    CREDENTIALS,
    create_epub,
    front_page,
    html,
    listing_page,
    RoutedSession,
    SERIES_BLURB,
    sign_in_replies,
    work_blurb,
)

BASE = "https://archiveofourown.org/tags/Example/works"


def page_url(page: int) -> str:
    return BASE if page == 1 else f"{BASE}?page={page}"


def pages(*works_per_page: list[tuple[str, str]] | list[str]) -> dict[str, str]:
    """A listing whose pages hold these works, each page linking to the next."""

    served: dict[str, str] = {}
    for number, works in enumerate(works_per_page, start=1):
        blurbs = [work_blurb(*work) if isinstance(work, tuple) else work_blurb(work) for work in works]
        has_next = number < len(works_per_page)
        served[page_url(number)] = listing_page(
            *blurbs, page=number, next_href=f"/tags/Example/works?page={number + 1}" if has_next else None
        )
    return served


def ids(start: int, count: int) -> list[str]:
    return [str(work_id) for work_id in range(start, start + count)]


def owned(**chapters_by_work: str | None) -> bookorbit.OwnedLibrary:
    works = {
        work_id.lstrip("w"): bookorbit.OwnedWork(work_id.lstrip("w"), index, (), chapters, {})
        for index, (work_id, chapters) in enumerate(chapters_by_work.items(), start=1)
    }
    return bookorbit.OwnedLibrary(works=works, ambiguous={}, books=len(works), unidentified=0)


def library(*, linked=(), downloaded=(), unavailable=(), owned_library=None) -> Library:
    return Library(frozenset(linked), frozenset(downloaded), frozenset(unavailable), owned_library)


def search(works: int, *, max_pages: int | None = None, name: str = "Example", url: str = BASE) -> Search:
    return Search(
        name=name, url=url, works=works, slug=harvest.slugify(name),
        max_pages=max_pages if max_pages is not None else harvest.default_page_limit(works),
    )


class ParseListingTest(unittest.TestCase):
    def test_works_are_read_in_order_with_their_chapter_counts(self):
        page = harvest.parse_listing(
            listing_page(work_blurb("11", "3/10"), work_blurb("12", "1/1"), work_blurb("13", "5/?")), BASE
        )

        self.assertEqual(
            page.blurbs, (Blurb("11", "3/10"), Blurb("12", "1/1"), Blurb("13", "5/?"))
        )

    def test_a_series_and_a_work_a_summary_links_to_are_not_listed_works(self):
        page = harvest.parse_listing(listing_page(work_blurb("11", summary_link="/works/999"), SERIES_BLURB), BASE)

        self.assertEqual([blurb.work_id for blurb in page.blurbs], ["11"])

    def test_the_next_page_is_the_paginations_next_link(self):
        page = harvest.parse_listing(listing_page(work_blurb("11"), next_href="/tags/Example/works?page=2"), BASE)

        self.assertEqual(page.next_url, f"{BASE}?page=2")

    def test_the_last_page_has_no_next_page(self):
        self.assertIsNone(harvest.parse_listing(listing_page(work_blurb("11")), BASE).next_url)

    def test_a_bookmarked_work_is_a_listed_work(self):
        bookmark = (
            '<li id="bookmark_5" class="bookmark blurb group work-42 user-1" role="article">'
            '<h4 class="heading"><a href="/works/42">Work</a></h4>'
            '<dl class="stats"><dd class="chapters"><a href="/works/42/chapters/1">3</a>/?</dd></dl>'
            '<div class="user module group"><ul class="meta tags commas"><li><a class="tag">mine</a></li></ul></div>'
            "</li>"
        )

        self.assertEqual(harvest.parse_listing(listing_page(bookmark), BASE).blurbs, (Blurb("42", "3/?"),))

    def test_a_work_listed_twice_keeps_its_first_place(self):
        page = harvest.parse_listing(listing_page(work_blurb("1"), work_blurb("2"), work_blurb("1")), BASE)

        self.assertEqual([blurb.work_id for blurb in page.blurbs], ["1", "2"])

    def test_pagination_is_found_whatever_element_holds_it(self):
        text = (
            '<body class="logged-in"><ol class="work index group">' + work_blurb("1") + "</ol>"
            '<nav class="pagination"><ul><li class="next"><a href="/tags/Example/works?page=2">Next</a></li></ul></nav>'
            "</body>"
        )

        page = harvest.parse_listing(text, BASE)

        self.assertEqual((page.next_url, page.paginated), (f"{BASE}?page=2", True))

    def test_a_next_link_that_leaves_ao3_is_not_followed(self):
        page = harvest.parse_listing(listing_page(work_blurb("11"), next_href="https://evil.example/works"), BASE)

        self.assertIsNone(page.next_url)


class LoadSearchesTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.path = Path(self._directory.name) / "searches.toml"

    def tearDown(self):
        self._directory.cleanup()

    def load(self, text: str):
        self.path.write_text(text, encoding="utf-8")
        return harvest.load_searches(self.path)

    def assert_refused(self, text: str, *fragments: str):
        with self.assertRaises(HarvestError) as raised:
            self.load(text)
        for fragment in fragments:
            self.assertIn(fragment, str(raised.exception))

    def test_each_search_has_a_name_a_listing_a_count_and_a_page_limit(self):
        searches = self.load(f'[[search]]\nname = "DCU"\nurl = "{BASE}"\nworks = 400\n'
                             f'[[search]]\nname = "Star Wars"\nurl = "{BASE}"\nworks = 30\nmax_pages = 9\n')

        self.assertEqual([(s.name, s.works, s.max_pages, s.link_file) for s in searches], [
            ("DCU", 400, 100, "dcu.txt"),
            ("Star Wars", 30, 9, "star-wars.txt"),
        ])

    def test_the_committed_example_is_a_valid_searches_file(self):
        example = Path(__file__).resolve().parent.parent / harvest.EXAMPLE_NAME

        self.assertTrue(harvest.load_searches(example))

    def test_a_missing_file_says_to_copy_the_example(self):
        with self.assertRaises(HarvestError) as raised:
            harvest.load_searches(self.path)

        self.assertIn(harvest.EXAMPLE_NAME, str(raised.exception))

    def test_a_single_work_is_not_a_listing(self):
        self.assert_refused(f'[[search]]\nname = "x"\nurl = "https://archiveofourown.org/works/123"\nworks = 5\n',
                            "single work")

    def test_a_single_work_inside_a_collection_is_not_a_listing_either(self):
        self.assert_refused('[[search]]\nname = "x"\n'
                            'url = "https://archiveofourown.org/collections/fest/works/123"\nworks = 5\n', "single work")

    def test_a_name_in_any_script_names_its_link_file(self):
        searches = self.load(f'[[search]]\nname = "呪術廻戦"\nurl = "{BASE}"\nworks = 5\n')

        self.assertEqual(searches[0].link_file, "呪術廻戦.txt")

    def test_only_ao3_is_harvested(self):
        self.assert_refused('[[search]]\nname = "x"\nurl = "https://example.com/works"\nworks = 5\n',
                            "archiveofourown.org")

    def test_nothing_is_guessed_about_a_count(self):
        self.assert_refused(f'[[search]]\nname = "x"\nurl = "{BASE}"\n', "works is missing")
        self.assert_refused(f'[[search]]\nname = "x"\nurl = "{BASE}"\nworks = 0\n', "above 0")
        self.assert_refused(f'[[search]]\nname = "x"\nurl = "{BASE}"\nworks = true\n', "above 0")

    def test_a_misspelt_key_is_named_rather_than_ignored(self):
        self.assert_refused(f'[[search]]\nname = "x"\nurl = "{BASE}"\nworks = 5\nmax_page = 3\n', "max_page")

    def test_two_searches_cannot_share_a_link_file(self):
        self.assert_refused(f'[[search]]\nname = "Star Wars"\nurl = "{BASE}"\nworks = 5\n'
                            f'[[search]]\nname = "star wars!"\nurl = "{BASE}"\nworks = 5\n', "star-wars.txt")

    def test_broken_toml_is_reported_as_such(self):
        self.assert_refused('[[search]\nname = "x"', "not valid TOML")


class JudgeTest(unittest.TestCase):
    def test_a_work_found_anywhere_is_not_new(self):
        held = library(linked={"1"}, downloaded={"2"}, unavailable={"3"}, owned_library=owned(w4="1/1"))

        self.assertEqual(
            [harvest.judge(Blurb(work_id, "1/1"), held, {"5"}) for work_id in "123456"],
            ["in links/", "in downloaded/", "unavailable on AO3", "on the server", "earlier search", "new"],
        )

    def test_an_unfinished_work_on_the_server_is_an_update_only_when_the_listing_shows_more_chapters(self):
        held = library(owned_library=owned(w1="3/10", w2="3/10", w3="3/?", w4="10/10", w5=None))

        verdicts = [
            harvest.judge(Blurb("1", "4/10"), held, set()),
            harvest.judge(Blurb("2", "3/10"), held, set()),
            harvest.judge(Blurb("3", "4/?"), held, set()),
            harvest.judge(Blurb("4", "11/11"), held, set()),
            harvest.judge(Blurb("5", "4/4"), held, set()),
        ]

        self.assertEqual(verdicts, ["update", "on the server", "update", "on the server", "on the server"])

    def test_a_work_bookorbit_holds_twice_is_owned(self):
        held = library(owned_library=bookorbit.OwnedLibrary(works={}, ambiguous={"1": (7, 8)}, books=2, unidentified=0))

        self.assertEqual(harvest.judge(Blurb("1", "1/1"), held, set()), "on the server")

    def test_without_bookorbit_a_work_nowhere_local_is_new(self):
        self.assertEqual(harvest.judge(Blurb("1", "1/1"), library(), set()), "new")


class HarvestSearchTest(unittest.TestCase):
    def run_search(self, served: dict[str, str], wanted: Search, held: Library, taken: set[str] | None = None):
        fetched: list[str] = []
        written: list[list[str]] = []

        def fetch(url: str) -> str:
            fetched.append(url)
            return served[url]

        result = harvest.harvest_search(wanted, fetch, held, set() if taken is None else taken, written.append)
        return result, fetched, written

    def test_pages_already_owned_are_turned_past_until_enough_new_works_are_found(self):
        served = pages(ids(100, 20), ids(200, 20), ids(300, 20))
        held = library(owned_library=owned(**{f"w{n}": "1/1" for n in [*ids(100, 20), *ids(200, 15)]}))

        result, fetched, _ = self.run_search(served, search(10), held)

        self.assertEqual(result.new, [*ids(215, 5), *ids(300, 5)])
        self.assertEqual(fetched, [page_url(1), page_url(2), page_url(3)])
        self.assertEqual(result.stopped, "found them all")

    def test_only_the_chosen_works_are_written_page_by_page(self):
        served = pages(["1", "2"], ["3", "4"])

        _, _, written = self.run_search(served, search(3), library(linked={"2"}))

        self.assertEqual(written, [["1"], ["3", "4"]])

    def test_a_listing_that_runs_out_stops_short_and_says_so(self):
        result, _, _ = self.run_search(pages(["1", "2"]), search(5), library())

        self.assertEqual((result.new, result.short), (["1", "2"], True))
        self.assertEqual(result.stopped, "reached the last page of the listing")

    def test_a_full_page_without_page_links_is_reported_as_a_possible_layout_change(self):
        text = '<body class="logged-in">' + "".join(work_blurb(work_id) for work_id in ids(100, 20)) + "</body>"

        with self.assertLogs("ao3.harvest", "WARNING") as logs:
            result, _, _ = self.run_search({BASE: text}, search(40), library())

        self.assertEqual(len(result.new), 20)
        self.assertIn("page layout", "\n".join(logs.output))

    def test_the_page_limit_ends_a_listing_the_library_already_covers(self):
        served = pages(ids(100, 20), ids(200, 20), ids(300, 20))
        held = library(owned_library=owned(**{f"w{n}": "1/1" for n in [*ids(100, 20), *ids(200, 20)]}))

        result, fetched, _ = self.run_search(served, search(5, max_pages=2), held)

        self.assertEqual((result.new, len(fetched)), ([], 2))
        self.assertIn("2-page limit", result.stopped)

    def test_updates_are_written_but_do_not_count_towards_the_number(self):
        served = pages([("1", "4/10"), ("2", "1/1")])
        held = library(owned_library=owned(w1="3/10"))

        result, _, written = self.run_search(served, search(1), held)

        self.assertEqual((result.new, result.updates, written), (["2"], ["1"], [["1", "2"]]))

    def test_a_crossover_found_by_an_earlier_search_is_not_counted_again(self):
        taken: set[str] = set()
        held = library()
        self.run_search(pages(["1", "2"]), search(2, name="A"), held, taken)

        result, _, _ = self.run_search(pages(["2", "3"]), search(2, name="B"), held, taken)

        self.assertEqual(result.new, ["3"])
        self.assertEqual(result.skipped["earlier search"], 1)


class HarvestHarness:
    """A searches file, a links folder and a fake AO3, for whole offline runs."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.searches = self.root / "searches.toml"
        self.links = self.root / "links"
        self.output = self.root / "downloaded"
        self.failure_log = self.root / "failures.jsonl"
        self.lock = self.root / "ao3.lock"
        self.session = RoutedSession()
        self.sleeps: list[float] = []

    def tearDown(self):
        self._directory.cleanup()

    def write_searches(self, *entries: tuple[str, str, int]):
        self.searches.write_text("".join(
            f'[[search]]\nname = "{name}"\nurl = "{url}"\nworks = {works}\n' for name, url, works in entries
        ), encoding="utf-8")

    def serve(self, served: dict[str, str]):
        for url, text in served.items():
            self.session.add(url, html(200, text))

    def harvest(self, **options):
        clock = [1000.0]

        def sleep(seconds: float) -> None:
            self.sleeps.append(round(seconds, 3))
            clock[0] += seconds

        def factory(credentials, delay, sleep_fn):
            return ao3_client.AO3DownloadClient(
                credentials, delay_seconds=delay, sleep_fn=sleep, session=self.session, monotonic_fn=lambda: clock[0]
            )

        options.setdefault("harvesters", lambda: ())
        return harvest.run_harvest(
            self.searches,
            self.links,
            self.output,
            failure_log_path=self.failure_log,
            lock_path=self.lock,
            credentials_loader=lambda: CREDENTIALS,
            client_factory=factory,
            sleep_fn=sleep,
            **options,
        )


class RunHarvestTest(HarvestHarness, unittest.TestCase):
    def test_new_works_are_written_where_download_fics_reads_them(self):
        self.write_searches(("DCU", BASE, 3))
        sign_in_replies(self.session)
        self.serve(pages(["1", "2"], ["3", "4"]))

        summary = self.harvest(owned_loader=lambda: owned(w2="1/1"))

        self.assertEqual(summary.new, 3)
        inventory = download.load_link_inventory(self.links)
        self.assertEqual([target.work_id for target in inventory.targets], ["1", "3", "4"])
        self.assertEqual({target.source for target in inventory.targets}, {"dcu.txt"})

    def test_listing_pages_are_paced_by_the_delay(self):
        self.write_searches(("DCU", BASE, 40))
        sign_in_replies(self.session)
        self.serve(pages(ids(100, 20), ids(200, 20)))

        self.harvest(delay_seconds=30.0)

        # Sign-in's two requests and both listing pages, each 30s after the last.
        self.assertEqual(self.sleeps, [30.0, 30.0, 30.0])

    def test_a_second_run_skips_what_the_first_already_linked(self):
        self.write_searches(("DCU", BASE, 2))
        sign_in_replies(self.session)
        self.serve(pages(["1", "2"]))
        self.harvest()
        sign_in_replies(self.session)
        self.serve(pages(["1", "2", "3"]))

        summary = self.harvest()

        self.assertEqual(summary.results[0].new, ["3"])

    def test_works_waiting_in_downloaded_are_not_harvested_again(self):
        self.output.mkdir()
        create_epub(self.output / "1.epub", front_page())
        self.write_searches(("DCU", BASE, 1))
        sign_in_replies(self.session)
        self.serve(pages(["1", "2"]))

        self.assertEqual(self.harvest().results[0].new, ["2"])

    def test_a_failing_search_does_not_stop_the_next_one(self):
        other = "https://archiveofourown.org/tags/Other/works"
        self.write_searches(("Gone", "https://archiveofourown.org/tags/Gone/works", 1), ("Other", other, 1))
        sign_in_replies(self.session)
        self.session.add("https://archiveofourown.org/tags/Gone/works", html(404, "<title>Error 404</title>"))
        self.session.add(other, html(200, listing_page(work_blurb("9"))))

        summary = self.harvest()

        self.assertIn("404", summary.results[0].error or "")
        self.assertEqual(summary.results[1].new, ["9"])

    def test_a_search_that_fails_partway_keeps_and_reports_what_it_found(self):
        self.write_searches(("DCU", BASE, 30))
        sign_in_replies(self.session)
        self.session.add(page_url(1), html(200, pages(ids(100, 20), ["x"])[page_url(1)]))
        self.session.add(page_url(2), html(404, "<title>Error 404</title>"))

        summary = self.harvest()

        result = summary.results[0]
        self.assertEqual((len(result.new), result.pages, summary.new), (20, 1, 20))
        self.assertIn("404", result.error or "")
        self.assertEqual(len(download.load_link_inventory(self.links).targets), 20)

    def test_trouble_with_ao3_itself_gets_a_cool_down_a_fresh_session_and_the_same_page_again(self):
        self.write_searches(("DCU", BASE, 1))
        sign_in_replies(self.session)
        challenge = html(503, "<title>Just a moment...</title>")
        self.session.add(BASE, challenge, challenge)
        sign_in_replies(self.session)
        self.serve(pages(["1"]))

        summary = self.harvest()

        self.assertEqual(summary.results[0].new, ["1"])
        self.assertIn(ao3_client.COOLDOWN_SCHEDULE_SECONDS[0], self.sleeps)
        self.assertEqual(self.session.requests.count("POST https://archiveofourown.org/users/login"), 2)

    def test_trouble_that_outlasts_the_cool_down_stops_the_run_instead_of_blaming_each_search(self):
        other = "https://archiveofourown.org/tags/Other/works"
        self.write_searches(("DCU", BASE, 1), ("Other", other, 1))
        sign_in_replies(self.session)
        challenge = html(503, "<title>Just a moment...</title>")
        self.session.add(BASE, challenge, challenge)
        sign_in_replies(self.session)
        self.session.add(BASE, challenge, challenge)

        with self.assertRaises(ao3_client.RepeatedCloudflare):
            self.harvest()
        self.assertNotIn(f"GET {other}", self.session.requests)

    def test_links_that_cannot_be_written_stop_the_run_before_another_request(self):
        self.links.write_text("not a folder", encoding="utf-8")
        other = "https://archiveofourown.org/tags/Other/works"
        self.write_searches(("DCU", BASE, 1), ("Other", other, 1))
        sign_in_replies(self.session)
        self.serve(pages(["1"]))

        with self.assertRaises(HarvestError):
            self.harvest()
        self.assertNotIn(f"GET {other}", self.session.requests)

    def test_an_interrupt_keeps_the_pages_already_written(self):
        self.write_searches(("DCU", BASE, 40))
        sign_in_replies(self.session)
        self.session.add(page_url(1), html(200, pages(ids(100, 20), ["x"])[page_url(1)]))
        original_get = self.session.get

        def get(url, *args, **kwargs):
            if url == page_url(2):
                raise RunInterrupted("interrupted")
            return original_get(url, *args, **kwargs)

        self.session.get = get

        with self.assertRaises(RunInterrupted):
            self.harvest()
        self.assertEqual(len(download.load_link_inventory(self.links).targets), 20)

    def test_a_dry_run_makes_no_requests_and_writes_nothing(self):
        self.write_searches(("DCU", BASE, 5))

        self.harvest(dry_run=True)

        self.assertEqual(self.session.requests, [])
        self.assertFalse(self.links.exists())

    def test_declining_to_start_makes_no_requests(self):
        self.write_searches(("DCU", BASE, 5))

        self.harvest(confirm_start=lambda: False)

        self.assertEqual(self.session.requests, [])

    def test_an_unreadable_bookorbit_stops_before_any_ao3_request(self):
        self.write_searches(("DCU", BASE, 5))

        def unreachable():
            raise bookorbit.BookOrbitError("BookOrbit is down")

        with self.assertRaises(bookorbit.BookOrbitError):
            self.harvest(owned_loader=unreachable)
        self.assertEqual(self.session.requests, [])

    def test_a_harvest_refuses_to_start_while_another_run_holds_the_account(self):
        self.write_searches(("DCU", BASE, 5))

        with account_lock(self.lock), self.assertRaises(AnotherRunActive):
            self.harvest()
        self.assertEqual(self.session.requests, [])

    def test_a_bad_searches_file_stops_before_anything_else(self):
        self.searches.write_text('[[search]]\nname = "x"\n', encoding="utf-8")

        with self.assertRaises(HarvestError):
            self.harvest()


if __name__ == "__main__":
    unittest.main()

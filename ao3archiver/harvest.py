"""Harvest AO3 work links into ``links/``, counting only works this library lacks.

Each search in ``searches.toml`` is an AO3 listing - a tag's works, a filtered
search, a bookmarks page - and how many works to bring back from it. A listing
sorted by hits barely changes between runs, so its first pages are mostly works
already on the server; reading a fixed number of pages would bring back mostly
duplicates. Instead each search keeps turning pages until it has found that
many works that are nowhere yet: not on the server, not in ``downloaded/``, not
already in ``links/``, not known to be gone from AO3, and not found by an
earlier search in the same run (crossovers sit in several fandoms).

A work the server holds unfinished, whose listing shows more chapters than the
server's copy, is written too, for ``download_fics.py`` to update; it does not
count towards the search's number. Every search stops at a page limit, so a
fandom the library already covers cannot be read to its last page.

Links are appended to ``links/<search>.txt`` page by page, so an interrupted
harvest keeps what it found, and a rerun skips it.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from html.parser import HTMLParser
import logging
import math
import os
from pathlib import Path
import re
import time
import tomllib
from typing import Literal
import urllib.parse

import requests

from ao3archiver.ao3_client import (
    account_lock,
    ACCOUNT_LOCK_PATH,
    ao3_run_active,
    _is_ao3_url,
    AO3_HOSTS,
    AO3DownloadClient,
    Cooldown,
    CredentialsRejected,
    DEFAULT_DELAY_SECONDS,
    MINIMUM_DELAY_SECONDS,
    NetworkStopError,
    SYSTEMIC_FETCH_ERRORS,
)
from ao3archiver.bookorbit import OwnedLibrary
from ao3archiver.common import ArchiverError, REPO_ROOT
from ao3archiver.credentials import AO3Credentials, load_run_credentials
from ao3archiver.download import (
    ClientFactory,
    DEFAULT_FAILURE_LOG,
    DEFAULT_LINKS_DIR,
    DEFAULT_OUTPUT_DIR,
    FailureLog,
    has_new_chapters,
    load_link_inventory,
    load_owned_works,
    OwnedLoader,
    running_harvesters,
)
from ao3archiver.metadata import canonical_work_url, is_in_progress
from ao3archiver import prompts
from ao3archiver.run_log import (
    configure_logging,
    DEFAULT_LOG_DIR,
    format_clock,
    format_duration,
    interrupt_guard,
    log_banner,
    RunInterrupted,
)

LOGGER = logging.getLogger("ao3.harvest")

DEFAULT_SEARCHES_PATH = REPO_ROOT / "searches.toml"
EXAMPLE_NAME = "searches.example.toml"
# AO3 shows twenty works on every listing page.
WORKS_PER_PAGE = 20
# Without its own max_pages, a search reads at most this many times the pages
# its number would need if every work on them were new. Past that, the listing
# is mostly works the library already has, and paging on is AO3's cost for
# little return.
PAGE_LIMIT_FACTOR = 5
SEARCH_KEYS = frozenset({"name", "url", "works", "max_pages"})
SINGLE_WORK_PATH = re.compile(r"/works/\d+(/|$)")
WORK_CLASS = re.compile(r"^work-(\d+)$")
EPUB_NAME = re.compile(r"^(\d+)\.epub$")


class HarvestError(ArchiverError):
    """A searches file, or a search in it, that a harvest cannot use."""


# --- searches ------------------------------------------------------------------


@dataclass(frozen=True)
class Search:
    """One AO3 listing, and how many works the library does not have to bring back from it."""

    name: str
    url: str
    works: int
    max_pages: int
    slug: str

    @property
    def link_file(self) -> str:
        return f"{self.slug}.txt"


def slugify(name: str) -> str:
    # Any script's letters will do for a file name; only separators become dashes.
    return re.sub(r"[\W_]+", "-", name.casefold()).strip("-")


def default_page_limit(works: int) -> int:
    return PAGE_LIMIT_FACTOR * math.ceil(works / WORKS_PER_PAGE)


def _positive_int(value: object, where: str, key: str) -> int:
    # TOML's true is a bool, and a bool is an int to Python.
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise HarvestError(f"{where}: {key} must be a whole number above 0, not {value!r}")
    return value


def _listing_url(value: object, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarvestError(f"{where}: url must be the address of an AO3 listing page")
    url = value.strip()
    if not _is_ao3_url(url):
        raise HarvestError(f"{where}: url must start with https://archiveofourown.org/, not {url!r}")
    if SINGLE_WORK_PATH.search(urllib.parse.urlparse(url).path):
        raise HarvestError(
            f"{where}: {url} is a single work. Use the page that lists works, "
            "such as a tag's works or a filtered search."
        )
    return url


def parse_search(table: object, index: int) -> Search:
    where = f"search {index}"
    if not isinstance(table, dict):
        raise HarvestError(f"{where} is not a [[search]] table")
    unknown = sorted(set(table) - SEARCH_KEYS)
    if unknown:
        raise HarvestError(f"{where}: unknown key(s) {', '.join(unknown)}; a search takes {', '.join(sorted(SEARCH_KEYS))}")
    name = table.get("name")
    if not isinstance(name, str) or not name.strip():
        raise HarvestError(f"{where}: name is missing; give each search a short name, such as \"DCU\"")
    name = name.strip()
    where = f"search {index} ({name})"
    slug = slugify(name)
    if not slug:
        raise HarvestError(f"{where}: the name needs at least one letter or digit, for its link file's name")
    if "works" not in table:
        raise HarvestError(f"{where}: works is missing; say how many works you do not have yet to bring back")
    works = _positive_int(table["works"], where, "works")
    max_pages = (
        _positive_int(table["max_pages"], where, "max_pages")
        if "max_pages" in table
        else default_page_limit(works)
    )
    return Search(name=name, url=_listing_url(table.get("url"), where), works=works, max_pages=max_pages, slug=slug)


def load_searches(path: Path) -> tuple[Search, ...]:
    """Read every ``[[search]]`` in ``path``; anything unclear stops the run, nothing is guessed."""

    if not path.is_file():
        raise HarvestError(
            f"No {path} yet. Copy {EXAMPLE_NAME} to {path.name} and list the AO3 searches to harvest."
        )
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise HarvestError(f"{path} is not valid TOML: {error}") from error
    unknown = sorted(set(document) - {"search"})
    if unknown:
        raise HarvestError(f"{path}: unknown setting(s) {', '.join(unknown)}; only [[search]] tables belong here")
    tables = document.get("search", [])
    if not isinstance(tables, list) or not tables:
        raise HarvestError(f"{path} lists no searches; add a [[search]] table for each (see {EXAMPLE_NAME})")
    searches = tuple(parse_search(table, index) for index, table in enumerate(tables, start=1))
    slugs = Counter(search.slug for search in searches)
    clashing = sorted(slug for slug, count in slugs.items() if count > 1)
    if clashing:
        raise HarvestError(
            f"{path}: more than one search would write links/{clashing[0]}.txt; give each search its own name"
        )
    return searches


# --- listing pages -------------------------------------------------------------


@dataclass(frozen=True)
class Blurb:
    """One work as a listing shows it."""

    work_id: str
    # "3/10", "1/1", "5/?": what the listing says, before it is trusted.
    chapters: str | None


@dataclass(frozen=True)
class ListingPage:
    blurbs: tuple[Blurb, ...]
    next_url: str | None
    # Whether the page had pagination at all: AO3 leaves it out of a
    # one-page listing, so a full page without it is a layout change.
    paginated: bool = False


class _ListingParser(HTMLParser):
    """Finds each work blurb, its chapter count, and the pagination's Next link.

    A blurb is an ``<li>`` classed ``blurb`` whose ``work-<id>`` class names the
    work. Series and external-work blurbs have no such class and are passed
    over; so are links inside summaries, which can point at any work.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blurbs: list[Blurb] = []
        self.next_href: str | None = None
        self._work_id: str | None = None
        self._li_depth = 0
        self._chapters: list[str] | None = None
        self._chapters_text: str | None = None
        self._pagination_tag: str | None = None
        self._pagination_depth = 0
        self._pagination_seen = False
        self._in_next = False

    @property
    def _in_pagination(self) -> bool:
        return self._pagination_tag is not None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = (dict(attrs).get("class") or "").split()
        if tag == self._pagination_tag:
            self._pagination_depth += 1
        if "pagination" in classes and not self._in_pagination and not self._pagination_seen:
            self._pagination_tag, self._pagination_depth = tag, 1
        elif tag == "li":
            if self._work_id is not None:
                self._li_depth += 1
            elif "blurb" in classes:
                work_ids = [match.group(1) for match in map(WORK_CLASS.match, classes) if match]
                if work_ids:
                    self._work_id, self._li_depth, self._chapters_text = work_ids[0], 1, None
            elif self._in_pagination and "next" in classes:
                self._in_next = True
        elif tag == "dd" and self._work_id is not None and "chapters" in classes:
            self._chapters = []
        elif tag == "a" and self._in_next and self.next_href is None:
            self.next_href = dict(attrs).get("href")

    def handle_endtag(self, tag: str) -> None:
        if tag == "dd" and self._chapters is not None:
            self._chapters_text = "".join(self._chapters).strip() or None
            self._chapters = None
        elif tag == "li" and self._work_id is not None:
            self._li_depth -= 1
            if self._li_depth == 0:
                self.blurbs.append(Blurb(self._work_id, self._chapters_text))
                self._work_id = None
        elif tag == "li" and self._in_next:
            self._in_next = False
        if tag == self._pagination_tag:
            self._pagination_depth -= 1
            if self._pagination_depth == 0:
                # AO3 repeats the pagination under the list; the first says it all.
                self._pagination_tag, self._pagination_seen = None, True

    def handle_data(self, data: str) -> None:
        if self._chapters is not None:
            self._chapters.append(data)


def parse_listing(html: str, page_url: str) -> ListingPage:
    """The works on one listing page, in order, and the next page's address if there is one."""

    parser = _ListingParser()
    parser.feed(html)
    parser.close()
    seen: set[str] = set()
    unique: list[Blurb] = []
    for blurb in parser.blurbs:
        if blurb.work_id not in seen:
            seen.add(blurb.work_id)
            unique.append(blurb)
    next_url = None
    if parser.next_href:
        candidate = urllib.parse.urljoin(page_url, parser.next_href)
        parsed = urllib.parse.urlparse(candidate)
        if parsed.scheme == "https" and parsed.hostname in AO3_HOSTS:
            next_url = candidate
        else:
            LOGGER.warning(f"ignoring a Next link that leaves AO3: {candidate}")
    return ListingPage(
        blurbs=tuple(unique), next_url=next_url, paginated=parser._pagination_seen or parser._in_pagination
    )


# --- what the library already has ------------------------------------------------


Verdict = Literal[
    "new", "update", "earlier search", "in links/", "in downloaded/", "unavailable on AO3", "on the server"
]


@dataclass(frozen=True)
class Library:
    """Every work a harvest must not bring back as new, by where it already is."""

    linked: frozenset[str]
    downloaded: frozenset[str]
    unavailable: frozenset[str]
    owned: OwnedLibrary | None


def downloaded_work_ids(output_dir: Path) -> frozenset[str]:
    if not output_dir.is_dir():
        return frozenset()
    return frozenset(
        match.group(1) for match in (EPUB_NAME.match(path.name) for path in output_dir.iterdir()) if match
    )


def judge(blurb: Blurb, library: Library, taken: Set[str]) -> Verdict:
    """Whether a listed work is new to this library, worth updating, or already somewhere."""

    work_id = blurb.work_id
    if work_id in taken:
        return "earlier search"
    if work_id in library.linked:
        return "in links/"
    if work_id in library.downloaded:
        return "in downloaded/"
    if work_id in library.unavailable:
        return "unavailable on AO3"
    owned = library.owned
    if owned is None:
        return "new"
    if work_id in owned.ambiguous:
        return "on the server"
    work = owned.works.get(work_id)
    if work is None:
        return "new"
    if is_in_progress(work.chapters) and has_new_chapters(work.chapters, blurb.chapters):
        return "update"
    return "on the server"


# --- one search ------------------------------------------------------------------


@dataclass
class SearchResult:
    search: Search
    new: list[str] = field(default_factory=list)
    updates: list[str] = field(default_factory=list)
    pages: int = 0
    skipped: Counter[str] = field(default_factory=Counter)
    stopped: str = ""
    error: str | None = None

    @property
    def short(self) -> bool:
        return len(self.new) < self.search.works


def append_links(path: Path, work_ids: Sequence[str]) -> None:
    """Add links to a link file and make them durable before the next request."""

    if not work_ids:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as stream:
        for work_id in work_ids:
            stream.write(canonical_work_url(work_id) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def harvest_search(
    search: Search,
    fetch_page: Callable[[str], str],
    library: Library,
    taken: set[str],
    write: Callable[[Sequence[str]], None],
    *,
    into: SearchResult | None = None,
) -> SearchResult:
    """Page through one listing until it yields ``search.works`` new works, runs out, or hits its limit.

    ``into`` is filled as pages are read, so a caller still has the progress
    when a page fails partway.
    """

    result = into if into is not None else SearchResult(search)
    url: str | None = search.url
    while url is not None:
        if result.pages >= search.max_pages:
            result.stopped = f"stopped at its {search.max_pages}-page limit"
            break
        page = parse_listing(fetch_page(url), url)
        result.pages += 1
        if result.pages == 1 and not page.blurbs:
            LOGGER.warning(f"{search.name}: no works on the first page; is {search.url} an AO3 listing?")
        verdicts: Counter[Verdict] = Counter()
        chosen: list[str] = []
        for blurb in page.blurbs:
            if len(result.new) >= search.works:
                break
            verdict = judge(blurb, library, taken)
            verdicts[verdict] += 1
            if verdict == "new":
                result.new.append(blurb.work_id)
            elif verdict == "update":
                result.updates.append(blurb.work_id)
            else:
                result.skipped[verdict] += 1
                continue
            chosen.append(blurb.work_id)
            taken.add(blurb.work_id)
        write(chosen)
        LOGGER.info(
            f"{search.name}: page {result.pages}: {len(page.blurbs)} works, {verdicts['new']} new"
            + (f", {verdicts['update']} with new chapters" if verdicts["update"] else "")
            + f" | {len(result.new)}/{search.works} new so far"
        )
        if len(result.new) >= search.works:
            result.stopped = "found them all"
            break
        if page.next_url is None:
            result.stopped = "reached the last page of the listing"
            if not page.paginated and len(page.blurbs) >= WORKS_PER_PAGE:
                LOGGER.warning(
                    f"{search.name}: a full page of works with no page links; if AO3 changed its page "
                    "layout, this search stopped early"
                )
        url = page.next_url
    return result


# --- run -------------------------------------------------------------------------


@dataclass(frozen=True)
class HarvestSummary:
    results: tuple[SearchResult, ...]

    @property
    def new(self) -> int:
        return sum(len(result.new) for result in self.results)

    @property
    def updates(self) -> int:
        return sum(len(result.updates) for result in self.results)


def _default_client(
    credentials: AO3Credentials,
    delay_seconds: float,
    sleep_fn: Callable[[float], None],
) -> AO3DownloadClient:
    return AO3DownloadClient(credentials, delay_seconds=delay_seconds, sleep_fn=sleep_fn)


def _owned_row(owned: OwnedLibrary | None) -> tuple[str, object]:
    if owned is None:
        return ("on the server", "unknown: BookOrbit is not configured, so it cannot rule works out")
    return ("on the server", f"{len(owned.works) + len(owned.ambiguous)} works")


def estimate_pages(searches: Sequence[Search]) -> tuple[int, int]:
    """The fewest pages the run can take (every work new) and the most (every limit reached)."""

    fewest = sum(min(search.max_pages, math.ceil(search.works / WORKS_PER_PAGE)) for search in searches)
    return fewest, sum(search.max_pages for search in searches)


def show_plan(
    searches: Sequence[Search],
    library: Library,
    *,
    searches_path: Path,
    links_dir: Path,
    delay_seconds: float,
    dry_run: bool,
) -> None:
    suffix = " (dry run)" if dry_run else ""
    log_banner(LOGGER, "AO3 link harvest" + suffix, [
        ("searches", f"{len(searches)} from {searches_path}"),
        ("new works wanted", sum(search.works for search in searches)),
        ("already in links/", len(library.linked)),
        ("already in downloaded/", len(library.downloaded)),
        ("unavailable on AO3", len(library.unavailable)),
        _owned_row(library.owned),
    ])
    width = max(len(search.name) for search in searches)
    for search in searches:
        LOGGER.info(
            f"  {search.name.ljust(width)}  {search.works} new works, at most {search.max_pages} pages "
            f"-> {links_dir.name}/{search.link_file}"
        )
    fewest, most = estimate_pages(searches)
    now = datetime.now().astimezone()
    log_banner(LOGGER, "Plan" + suffix, [
        ("listing pages", f"{fewest} to {most}, one request each"),
        ("delay", f"{delay_seconds:g}s between requests"),
        (
            "estimated time",
            f"{format_duration(fewest * delay_seconds)} to {format_duration(most * delay_seconds)} "
            f"(done between {format_clock(now + timedelta(seconds=fewest * delay_seconds))} "
            f"and {format_clock(now + timedelta(seconds=most * delay_seconds))})",
        ),
        ("link files", links_dir),
    ])


def report(results: Sequence[SearchResult], links_dir: Path) -> None:
    if not results:
        return
    width = max(len(result.search.name) for result in results)
    lines: list[tuple[str, object]] = []
    for result in results:
        detail = (
            f"{len(result.new)}/{result.search.works} new"
            + (f", {len(result.updates)} with new chapters" if result.updates else "")
            + f", {result.pages} page{'s' if result.pages != 1 else ''}; "
            + (f"failed: {result.error}" if result.error else result.stopped or "stopped partway")
        )
        lines.append((result.search.name.ljust(width), detail))
    log_banner(LOGGER, "Harvest finished", lines)
    for result in results:
        if result.error is None and result.short and "limit" in result.stopped:
            LOGGER.warning(
                f"{result.search.name}: found {len(result.new)} of {result.search.works} new works within "
                f"{result.search.max_pages} pages; raise its max_pages in the searches file to look further"
            )
        if result.error is not None:
            LOGGER.warning(f"{result.search.name}: rerun to try again; what it found is already in {links_dir}")


def run_harvest(
    searches_path: Path,
    links_dir: Path,
    output_dir: Path,
    *,
    delay_seconds: float = DEFAULT_DELAY_SECONDS,
    dry_run: bool = False,
    confirm_start: Callable[[], bool] | None = None,
    owned_loader: OwnedLoader | None = None,
    harvesters: Callable[[], Sequence[str]] = running_harvesters,
    sleep_fn: Callable[[float], None] = time.sleep,
    failure_log_path: Path = DEFAULT_FAILURE_LOG,
    lock_path: Path = ACCOUNT_LOCK_PATH,
    credentials_loader: Callable[[], AO3Credentials] = load_run_credentials,
    client_factory: ClientFactory = _default_client,
) -> HarvestSummary:
    """Plan the harvest, then read each search's listing until it has enough new works.

    Everything the plan needs - the searches, ``links/``, ``downloaded/``, the
    failure log and the server - is read before the first AO3 request; a
    BookOrbit that is configured but cannot be read stops the run there.
    """

    searches = load_searches(searches_path)
    inventory = load_link_inventory(links_dir) if links_dir.is_dir() else None
    library = Library(
        linked=frozenset(target.work_id for target in inventory.targets) if inventory else frozenset(),
        downloaded=downloaded_work_ids(output_dir),
        unavailable=FailureLog(failure_log_path).permanently_unavailable(),
        owned=None,
    )
    if owned_loader is not None:
        library = Library(library.linked, library.downloaded, library.unavailable, owned_loader())
    show_plan(
        searches, library,
        searches_path=searches_path, links_dir=links_dir, delay_seconds=delay_seconds, dry_run=dry_run,
    )
    if library.owned is None:
        LOGGER.warning(
            "BookOrbit is not configured: works already on the server cannot be ruled out, "
            "so they count as new"
        )
    for process in harvesters():
        LOGGER.warning(
            f"ao3downloader is running ({process}); it signs in to the same AO3 account, which this run's "
            "lock cannot see. Let it finish before harvesting."
        )

    if dry_run:
        if ao3_run_active(lock_path):
            LOGGER.warning("another AO3 run is active; a real harvest would refuse to start")
        LOGGER.info("dry run: no AO3 requests were made")
        return HarvestSummary(())
    if confirm_start is not None and not confirm_start():
        LOGGER.info("stopped before the first request; no links were written")
        return HarvestSummary(())

    results: list[SearchResult] = []
    with account_lock(lock_path):
        credentials = credentials_loader()
        LOGGER.info(f"using credentials from {credentials.source}")
        client = client_factory(credentials, delay_seconds, sleep_fn)
        cooldown = Cooldown(sleep_fn)

        def fetch_page(url: str) -> str:
            # A problem with AO3 or the session gets a cool-down, a fresh
            # session and one more try at the same page before it is the search's.
            for attempt in range(2):
                try:
                    page = client.fetch_listing_page(url)
                except SYSTEMIC_FETCH_ERRORS as error:
                    if attempt or isinstance(error, CredentialsRejected):
                        raise
                    cooldown.wait(f"{error}; this affects every page")
                    client.recover_session(cooldown)
                    continue
                cooldown.reset()
                return page
            raise NetworkStopError(f"could not fetch {url}")

        taken: set[str] = set()
        try:
            client.recover_session(cooldown)
            for search in searches:
                LOGGER.info(f"{search.name}: looking for {search.works} new works in {search.url}")
                link_path = links_dir / search.link_file
                result = SearchResult(search)
                results.append(result)
                try:
                    harvest_search(
                        search, fetch_page, library, taken,
                        lambda ids, path=link_path: append_links(path, ids), into=result,
                    )
                except SYSTEMIC_FETCH_ERRORS as error:
                    # It outlasted a cool-down and a fresh session: every later
                    # search would hit it too, so the run stops here.
                    result.error = f"{type(error).__name__}: {error}"
                    raise
                except (NetworkStopError, requests.RequestException) as error:
                    # This listing's own problem, such as a 404: the next search runs.
                    result.error = f"{type(error).__name__}: {error}"
                    LOGGER.error(f"{search.name}: {result.error}")
        except (RunInterrupted, ArchiverError, OSError) as error:
            report(results, links_dir)
            LOGGER.warning(f"stopped; the links found so far are in {links_dir}")
            if isinstance(error, OSError) and not isinstance(error, requests.RequestException):
                raise HarvestError(f"could not write to {links_dir}: {error}") from error
            raise
        finally:
            client.close()

    summary = HarvestSummary(tuple(results))
    report(results, links_dir)
    LOGGER.info(
        f"{summary.new} new works and {summary.updates} with new chapters added to {links_dir}; "
        "next: python3 download_fics.py"
    )
    return summary


# --- CLI -------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect links to AO3 works this library does not have yet, from the searches in searches.toml.",
    )
    parser.add_argument(
        "--searches", type=Path, default=DEFAULT_SEARCHES_PATH,
        help=f"the searches file (default: {DEFAULT_SEARCHES_PATH.name} in the repository)",
    )
    parser.add_argument("--links", type=Path, default=DEFAULT_LINKS_DIR, help="directory the link files go in")
    parser.add_argument(
        "--output", type=Path, default=DEFAULT_OUTPUT_DIR,
        help="directory of downloaded EPUBs, whose works are not harvested again",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY_SECONDS,
        help=f"seconds between AO3 requests (default: {DEFAULT_DELAY_SECONDS:g}, "
             f"minimum: {MINIMUM_DELAY_SECONDS:g})",
    )
    parser.add_argument("--dry-run", action="store_true", help="show the plan without making requests")
    parser.add_argument(
        "--failure-log", type=Path, default=DEFAULT_FAILURE_LOG,
        help="download_fics.py's failure log, whose unavailable works are not harvested",
    )
    parser.add_argument(
        "--log-dir", type=Path, default=None,
        help=f"directory for this run's log file (default: {DEFAULT_LOG_DIR})",
    )
    parser.add_argument("--yes", action="store_true", help="start without asking")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not math.isfinite(args.delay) or args.delay < MINIMUM_DELAY_SECONDS:
        parser.error(f"--delay must be at least {MINIMUM_DELAY_SECONDS:g} seconds")

    run = configure_logging("harvest", log_dir=args.log_dir)
    run.logger.info(f"ao3 harvest · log file: {run.log_path}")

    def confirm_start() -> bool:
        # The harvest only adds lines to links/, so off a terminal it starts.
        return prompts.confirm("Start this harvest now?", default=True, assume_yes=args.yes)

    with interrupt_guard(run.logger, unit="page") as guard:
        run_harvest(
            args.searches,
            args.links,
            args.output,
            delay_seconds=args.delay,
            dry_run=args.dry_run,
            confirm_start=None if args.dry_run else confirm_start,
            owned_loader=load_owned_works,
            sleep_fn=guard.sleep,
            failure_log_path=args.failure_log,
        )
    return 0


def run() -> None:
    """Command-line entry point used by ``harvest_links.py``."""

    try:
        raise SystemExit(main())
    except RunInterrupted as error:
        LOGGER.warning(f"{error}; rerun the same command to carry on")
        raise SystemExit(130) from error
    except ArchiverError as error:
        LOGGER.error(str(error))
        raise SystemExit(2) from error

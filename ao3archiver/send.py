"""Send downloaded EPUBs to the server, then fill in BookOrbit's custom fields.

Each EPUB in ``downloaded/`` goes to one of three places, decided by its AO3
work ID and by what BookOrbit already holds:

- a work BookOrbit does not hold is added as ``SERVER_LIBRARY_DIR/<work id>.epub``;
- a work it holds replaces that book's file at the book's own path, old
  ``Author/Title (id)/`` layout included, so BookOrbit keeps it as the same book;
- a file identical to the server's copy is left alone.

Adding and replacing are asked separately, at the start and never per book.
Nothing is deleted, locally or on the server: the user empties ``downloaded/``
by hand. BookOrbit's folder watch then picks the new books up; this waits for
it, and fills in the custom fields of every book whose server file now matches
the local one, sending only the values that changed.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from typing import Literal
import xml.etree.ElementTree as ET
import zipfile

from ao3archiver import prompts
from ao3archiver.bookorbit import (
    BookOrbitClient,
    BookOrbitConfig,
    choose_fields,
    CustomField,
    load_bookorbit_config,
    OwnedLibrary,
    OwnedWork,
    owned_works,
    publish_values,
    SyncSummary,
)
from ao3archiver.common import ArchiverError
from ao3archiver.credentials import DEFAULT_DOTENV_PATH
from ao3archiver.download import DEFAULT_OUTPUT_DIR
from ao3archiver.metadata import read_ao3_metadata
from ao3archiver.run_log import configure_logging, log_banner
from ao3archiver.server import (
    default_runner,
    last_lines,
    library_relative,
    LIBRARY_IN_CONTAINER,
    load_server_settings,
    remote,
    remote_dir_exists,
    rsync_command,
    Runner,
    SERVER,
    SERVER_BOOKORBIT_DIR,
    SERVER_LIBRARY_DIR,
    ServerError,
    ServerSettings,
    transfer,
)

LOGGER = logging.getLogger("ao3.send")

COMMAND = "send_to_server.py"
REQUIRED = (SERVER, SERVER_BOOKORBIT_DIR, SERVER_LIBRARY_DIR, LIBRARY_IN_CONTAINER)
# The test book took 29 s to appear with folder watch on; three minutes is
# plenty, and a book that is later still is filled in by the next run.
POLL_SECONDS = 10.0
POLL_ATTEMPTS = 18
PREVIEW = 20
EPUB_ERRORS = (OSError, ValueError, KeyError, zipfile.BadZipFile, ET.ParseError)
# rsync --itemize-changes: "<f" is a file sent to the server, ">f" one copied
# locally. Nine "+" mean the file does not exist there yet; anything else means
# it does and differs. An identical file is not listed at all.
ITEMIZED = re.compile(r"^[<>]f(?P<attributes>\S{9}) (?P<path>.+)$")

NO_BOOKORBIT = (
    f"{COMMAND} needs BookOrbit: it is how it knows which books are already on the server, and where. "
    "Add these lines to .env (docs/BOOKORBIT.md explains them):\n\n"
    "    BOOKORBIT_URL=\n"
    "    BOOKORBIT_USERNAME=\n"
    "    BOOKORBIT_PASSWORD="
)
REPLACE_WARNING = (
    "Replacing overwrites each book's file on the server, and BookOrbit re-reads it at once: the book's "
    "unlocked metadata (title, authors, tags, description) is replaced from the file. "
    "Back up first with backup_server.py, and export BookOrbit's metadata as a snapshot."
)

Change = Literal["add", "replace"]


class SendError(ServerError):
    """Something the send found that it will not work around."""


# --- what is being sent ---------------------------------------------------------


@dataclass(frozen=True)
class LocalEpub:
    path: Path
    work_id: str


@dataclass(frozen=True)
class Batch:
    """The EPUBs in ``downloaded/``, by work ID, and the files that cannot be sent."""

    epubs: tuple[LocalEpub, ...]
    unreadable: tuple[Path, ...]
    # A second file for a work already in the batch; the first one is sent.
    duplicates: tuple[Path, ...]


def read_batch(downloaded: Path) -> Batch:
    """Identify each EPUB by the work ID baked into it, not by its file name."""

    epubs: list[LocalEpub] = []
    unreadable: list[Path] = []
    duplicates: list[Path] = []
    seen: set[str] = set()
    paths = sorted(downloaded.glob("*.epub")) if downloaded.is_dir() else []
    for path in paths:
        if path.name.startswith("."):
            continue  # a download still being written
        try:
            work_id = read_ao3_metadata(path).work_id
        except EPUB_ERRORS:
            unreadable.append(path)
            continue
        if work_id in seen:
            duplicates.append(path)
            continue
        seen.add(work_id)
        epubs.append(LocalEpub(path, work_id))
    return Batch(tuple(epubs), tuple(unreadable), tuple(duplicates))


@dataclass(frozen=True)
class Placement:
    """Where one EPUB goes, relative to ``SERVER_LIBRARY_DIR``."""

    epub: LocalEpub
    target: PurePosixPath
    # The BookOrbit book whose file this is, when BookOrbit already holds the work.
    book: OwnedWork | None


@dataclass(frozen=True)
class Skipped:
    path: Path
    reason: str


def place(
    batch: Batch, owned: OwnedLibrary, settings: ServerSettings
) -> tuple[tuple[Placement, ...], tuple[Skipped, ...]]:
    """Decide each EPUB's path on the server. Anything unclear is skipped and said, never guessed."""

    taken: dict[PurePosixPath, str] = {}
    for work in owned.works.values():
        for path in work.file_paths:
            try:
                taken[library_relative(path, settings)] = work.work_id
            except ServerError:
                continue

    placements: list[Placement] = []
    skipped: list[Skipped] = []
    for epub in batch.epubs:
        if epub.work_id in owned.ambiguous:
            books = ", ".join(str(book_id) for book_id in owned.ambiguous[epub.work_id])
            skipped.append(Skipped(epub.path, f"BookOrbit has several books for work {epub.work_id} (ids {books})"))
            continue
        book = owned.works.get(epub.work_id)
        if book is None:
            target = PurePosixPath(f"{epub.work_id}.epub")
            if target in taken:
                skipped.append(Skipped(epub.path, f"{target} on the server is BookOrbit's copy of work {taken[target]}"))
                continue
            placements.append(Placement(epub, target, None))
            continue
        files = [path for path in book.file_paths if path.casefold().endswith(".epub")]
        if len(files) != 1:
            skipped.append(Skipped(epub.path, f"BookOrbit book {book.book_id} has {len(files)} EPUB files, not one"))
            continue
        try:
            target = library_relative(files[0], settings)
        except ServerError:
            skipped.append(Skipped(
                epub.path, f"BookOrbit keeps work {epub.work_id} at {files[0]}, outside {LIBRARY_IN_CONTAINER}"
            ))
            continue
        placements.append(Placement(epub, target, book))
    return tuple(placements), tuple(skipped)


def stage(placements: Sequence[Placement], into: Path) -> None:
    """A folder shaped like the server's library, of links to the local files.

    One rsync of it then sends every file to its own path, in place.
    """

    into.mkdir(parents=True, exist_ok=True)
    for placement in placements:
        link = into.joinpath(*placement.target.parts)
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(placement.epub.path.resolve())


def send_command(staging: Path, settings: ServerSettings, *options: str) -> list[str]:
    # --copy-links sends the files the links point to; --omit-dir-times leaves
    # the server's folders as they are; hidden files are a download in progress.
    return rsync_command(
        f"{staging}/",
        f"{remote(settings.server, settings.library_dir)}/",
        "--copy-links",
        "--omit-dir-times",
        "--exclude=.*",
        *options,
    )


def parse_itemized(output: str) -> dict[PurePosixPath, Change]:
    changes: dict[PurePosixPath, Change] = {}
    for line in output.splitlines():
        match = ITEMIZED.match(line)
        if match is not None:
            created = set(match.group("attributes")) == {"+"}
            changes[PurePosixPath(match.group("path"))] = "add" if created else "replace"
    return changes


def classify(staging: Path, settings: ServerSettings, runner: Runner) -> dict[PurePosixPath, Change]:
    """Ask rsync, without sending anything, which files are new, which differ, and which are identical."""

    result = runner(send_command(staging, settings, "--dry-run", "--itemize-changes"))
    if result.returncode != 0:
        raise SendError(
            f"rsync could not compare with {settings.server} (exit {result.returncode}):\n{last_lines(result.output)}"
        )
    return parse_itemized(result.output)


# --- after the transfer -----------------------------------------------------------


def wait_for_books(
    client: BookOrbitClient,
    library_id: int | None,
    fields: Sequence[CustomField],
    work_ids: set[str],
    *,
    sleep_fn: Callable[[float], None],
    attempts: int = POLL_ATTEMPTS,
    interval: float = POLL_SECONDS,
) -> OwnedLibrary:
    """Poll BookOrbit until its folder watch has picked up every new work, or give up."""

    LOGGER.info(f"waiting for BookOrbit to pick up {len(work_ids)} new book(s); checking every {interval:g}s")
    owned = None
    for attempt in range(1, attempts + 1):
        sleep_fn(interval)
        owned = owned_works(client, library_id, fields=fields)
        visible = len(work_ids & owned.works.keys())
        LOGGER.info(f"  check {attempt}/{attempts}: {visible}/{len(work_ids)} visible")
        if visible == len(work_ids):
            break
    assert owned is not None
    return owned


# --- the command --------------------------------------------------------------------


def _describe_bookorbit(config: BookOrbitConfig) -> str:
    library = f"library {config.library_id}" if config.library_id is not None else "every library"
    return f"{config.base_url} ({library})"


def show_plan(
    batch: Batch,
    adds: Sequence[Placement],
    replaces: Sequence[Placement],
    same: Sequence[Placement],
    skipped: Sequence[Skipped],
    settings: ServerSettings,
) -> None:
    not_sent = len(skipped) + len(batch.unreadable) + len(batch.duplicates)
    log_banner(LOGGER, "The plan", [
        ("EPUBs read", len(batch.epubs) + len(batch.unreadable) + len(batch.duplicates)),
        ("new, to add", len(adds)),
        ("on the server, to replace", len(replaces)),
        ("on the server, identical", len(same)),
        ("not sent", not_sent),
    ])
    for placement in adds[:PREVIEW]:
        LOGGER.info(f"  add      {placement.epub.path.name} -> {settings.library_dir / placement.target}")
    if len(adds) > PREVIEW:
        LOGGER.info(f"  ... and {len(adds) - PREVIEW} more to add")
    # Every replacement is listed: each one overwrites a file someone may be reading.
    for placement in replaces:
        book = f" (BookOrbit book {placement.book.book_id})" if placement.book is not None else ""
        LOGGER.info(f"  replace  {placement.epub.path.name} -> {settings.library_dir / placement.target}{book}")
    for item in skipped:
        LOGGER.warning(f"  not sent: {item.path.name}: {item.reason}")
    for path in batch.unreadable:
        LOGGER.warning(f"  not sent: {path.name}: not an EPUB with AO3 metadata in it")
    for path in batch.duplicates:
        LOGGER.warning(f"  not sent: {path.name}: another file in the folder is the same work")


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Send the EPUBs in downloaded/ to the server: new works are added, works BookOrbit already "
            "holds are replaced in place, then BookOrbit's custom fields are filled in. Takes no options: "
            "it shows the plan and asks first. See docs/SERVER.md."
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = default_runner,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
    downloaded: Path = DEFAULT_OUTPUT_DIR,
    client_factory: Callable[[BookOrbitConfig], BookOrbitClient] = BookOrbitClient,
    input_fn: Callable[[str], str] = input,
    terminal_fn: Callable[[], bool] = prompts.at_a_terminal,
    sleep_fn: Callable[[float], None] = time.sleep,
    log_dir: Path | None = None,
) -> int:
    build_parser().parse_args(argv)
    run = configure_logging("send-to-server", log_dir=log_dir)
    LOGGER.info(f"send to server · log file: {run.log_path}")
    prompts.require_terminal(COMMAND, terminal_fn=terminal_fn)

    settings = load_server_settings(REQUIRED, environ, dotenv_path=dotenv_path, command=COMMAND)
    config = load_bookorbit_config(environ, dotenv_path=dotenv_path)
    if config is None:
        raise SendError(NO_BOOKORBIT)
    log_banner(LOGGER, "Send to the server", [
        *settings.rows(),
        ("BookOrbit", _describe_bookorbit(config)),
        ("sending from", downloaded),
    ])

    def ask(question: str, default: bool) -> bool:
        return prompts.confirm(question, default=default, logger=LOGGER, input_fn=input_fn, terminal_fn=terminal_fn)

    batch = read_batch(downloaded)
    if not batch.epubs:
        show_plan(batch, (), (), (), (), settings)
        LOGGER.info(f"nothing to send: no EPUBs with AO3 metadata in {downloaded}")
        return 0
    LOGGER.info(f"checking that {settings.library_dir} exists on {settings.server}")
    if not remote_dir_exists(settings.server, settings.library_dir, runner):
        raise SendError(
            f"{settings.library_dir} does not exist on {settings.server}. Check {SERVER_LIBRARY_DIR} in {dotenv_path}."
        )

    client = client_factory(config)
    fields = client.custom_fields()
    choice = choose_fields(fields, ask=ask)
    if not choice.usable:
        LOGGER.warning("the books will be sent, but no BookOrbit custom field will be filled in")
    LOGGER.info("reading what BookOrbit already holds")
    owned = owned_works(client, config.library_id, fields=fields)
    LOGGER.info(
        f"BookOrbit holds {owned.books} books: {len(owned.works)} identified AO3 works, "
        f"{owned.unidentified} it cannot tell the work of"
    )

    placements, skipped = place(batch, owned, settings)
    with tempfile.TemporaryDirectory(prefix="send-to-server-") as directory:
        stage(placements, Path(directory))
        changes = classify(Path(directory), settings, runner)
    adds = [placement for placement in placements if changes.get(placement.target) == "add"]
    replaces = [placement for placement in placements if changes.get(placement.target) == "replace"]
    same = [placement for placement in placements if placement.target not in changes]
    show_plan(batch, adds, replaces, same, skipped, settings)

    approved: list[Placement] = []
    if adds and ask(f"Add {len(adds)} new book(s) to the server?", True):
        approved += adds
    if replaces:
        LOGGER.warning(REPLACE_WARNING)
        if ask(f"Replace {len(replaces)} book(s) on the server?", False):
            approved += replaces
    declined = [placement for placement in (*adds, *replaces) if placement not in approved]
    if not adds and not replaces:
        LOGGER.info("nothing to send: every file is already on the server")

    if approved:
        with tempfile.TemporaryDirectory(prefix="send-to-server-") as directory:
            stage(approved, Path(directory))
            LOGGER.info(f"sending {len(approved)} file(s) to {settings.server}")
            transfer(send_command(Path(directory), settings, "--stats", "--info=progress2"), runner, LOGGER)

    # Only a book whose server file now matches the local one gets the local
    # values: a declined replacement keeps its old file, and its old chapter count.
    matching = [*approved, *same]
    new_work_ids = {placement.epub.work_id for placement in approved if changes[placement.target] == "add"}
    if new_work_ids:
        owned = wait_for_books(client, config.library_id, fields, new_work_ids, sleep_fn=sleep_fn)

    pairs: list[tuple[Path, Mapping[str, object]]] = []
    not_visible: list[Placement] = []
    for placement in matching:
        book = owned.works.get(placement.epub.work_id)
        if book is None:
            not_visible.append(placement)
        else:
            pairs.append((placement.epub.path, book.row))
    summary = publish_values(pairs, client, choice.usable) if choice.usable else SyncSummary(configured=True)

    log_banner(LOGGER, "Sent", [
        ("added", sum(1 for placement in approved if changes[placement.target] == "add")),
        ("replaced", sum(1 for placement in approved if changes[placement.target] == "replace")),
        ("identical, not sent", len(same)),
        ("declined", len(declined)),
        ("not sent", len(skipped) + len(batch.unreadable) + len(batch.duplicates)),
        ("not in BookOrbit yet", len(not_visible)),
        ("fields filled in", f"{len(summary.updated)} book(s); {len(summary.unchanged)} already up to date"),
    ])
    if not_visible:
        LOGGER.warning(
            f"{len(not_visible)} book(s) are not in BookOrbit yet, so their fields are not filled in: "
            f"{', '.join(placement.epub.path.name for placement in not_visible[:10])}. "
            f"Rerun {COMMAND} in a minute; it sends nothing twice."
        )
    for name, error in summary.failed[:10]:
        LOGGER.error(f"fields not written for {name}: {error}")
    if summary.failed:
        raise SendError(f"{len(summary.failed)} book(s) could not have their fields written; rerun to retry them")
    return 130 if summary.interrupted else 0


def run() -> None:
    """Command-line entry point used by ``send_to_server.py``."""

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOGGER.warning(f"stopped; anything already sent stays on the server. Rerun {COMMAND} to finish.")
        raise SystemExit(130) from None
    except ArchiverError as error:
        LOGGER.error(str(error))
        raise SystemExit(2) from error

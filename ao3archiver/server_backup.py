"""Back up the server's BookOrbit folder, its library and its database to this PC.

``SERVER_BOOKORBIT_DIR`` is copied, folder and all, into ``BACKUP_DIR``: a
server folder ``/srv/bookorbit`` becomes ``BACKUP_DIR/bookorbit``. When
``SERVER_LIBRARY_DIR`` sits elsewhere, as when the books live on another drive,
it is copied the same way beside it, so every book sent is backed up.

The database is the exception. Postgres's own folder is left out of the copy:
its files are private to the database's user, and a copy of them taken while
the database runs need not be one Postgres can start from. Instead ``pg_dump``
runs inside BookOrbit's database container and its output is streamed straight
to ``BACKUP_DIR/bookorbit-database.sql``, so nothing is written on the server.

Nothing is deleted on either side. There is no archive and no copying onward:
taking the backup to a drive or a cloud folder is left to the person, on
purpose. The accepted cost of a plain mirror: a file replaced, or corrupted, on
the server replaces the backup's copy on the next run. The dump is replaced only
by a dump that finished.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import logging
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
from typing import Protocol

from ao3archiver import prompts
from ao3archiver.common import ArchiverError
from ao3archiver.credentials import DEFAULT_DOTENV_PATH
from ao3archiver.run_log import configure_logging, log_banner
from ao3archiver.server import (
    BACKUP_DIR,
    default_runner,
    last_lines,
    load_server_settings,
    remote,
    remote_dir_exists,
    rsync_command,
    Runner,
    SERVER,
    SERVER_BOOKORBIT_DIR,
    SERVER_LIBRARY_DIR,
    ssh_command,
    SSH_UNREACHABLE,
    ServerError,
    ServerSettings,
    transfer,
)

LOGGER = logging.getLogger("ao3.backup")

COMMAND = "backup_server.py"
REQUIRED = (SERVER, SERVER_BOOKORBIT_DIR, SERVER_LIBRARY_DIR, BACKUP_DIR)

# The names BookOrbit's own docker-compose.yml gives its database: the service,
# and the folder in SERVER_BOOKORBIT_DIR that holds the database's files.
DATABASE_SERVICE = "postgres"
DATABASE_FOLDER = PurePosixPath("data/postgres")
# The container already holds the user and database names, so they never need
# to be known here, and there is no password to pass inside the container.
PG_DUMP = 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'
# pg_dump writes this near its very end, so a dump cut short lacks it.
DUMP_COMPLETE = b"-- PostgreSQL database dump complete"
DUMP_TAIL_BYTES = 4096


@dataclass(frozen=True)
class DumpResult:
    """How the dump ended, and what it said on stderr. Its stdout is the file."""

    returncode: int
    errors: str


class Dumper(Protocol):
    def __call__(self, args: Sequence[str], into: Path) -> DumpResult: ...


def default_dumper(args: Sequence[str], into: Path) -> DumpResult:
    """Run a command with its stdout written to ``into``, keeping stderr apart from it."""

    try:
        with into.open("wb") as stream:
            completed = subprocess.run(list(args), stdout=stream, stderr=subprocess.PIPE, check=False)
            stream.flush()
            os.fsync(stream.fileno())
    except FileNotFoundError as error:
        raise ServerError(f"{args[0]} is not installed, or not on PATH") from error
    return DumpResult(completed.returncode, completed.stderr.decode("utf-8", errors="replace"))


def backup_sources(settings: ServerSettings) -> tuple[tuple[str, PurePosixPath], ...]:
    """The server folders to copy, each with the setting that names it.

    A library inside BookOrbit's folder is already in that copy; copying it
    again would only put a second copy of every book in the backup.
    """

    sources = [(SERVER_BOOKORBIT_DIR, settings.bookorbit_dir)]
    if not settings.library_dir.is_relative_to(settings.bookorbit_dir):
        sources.append((SERVER_LIBRARY_DIR, settings.library_dir))
    for key, path in sources:
        if not path.name:
            raise ServerError(f"{key} must name a folder, not the server's root")
    if len({path.name for _, path in sources}) < len(sources):
        # Each lands in a folder named like its own, so two with one name would mix into one.
        raise ServerError(
            f"{SERVER_BOOKORBIT_DIR} and {SERVER_LIBRARY_DIR} both end in {sources[0][1].name!r}, so their "
            "backups would land in the same folder and mix"
        )
    return tuple(sources)


def backup_folder(settings: ServerSettings, source: PurePosixPath) -> Path:
    """Where a copy lands: a folder in ``BACKUP_DIR`` named like the server's."""

    return settings.backup_dir / source.name


def backup_command(settings: ServerSettings, source: PurePosixPath) -> list[str]:
    """One pull. No slash after the source, so rsync copies the folder itself, not
    just what is in it: its contents never spill loose into ``BACKUP_DIR``."""

    options = ["--stats", "--info=progress2"]
    if source == settings.bookorbit_dir:
        # Anchored at the top of the transfer, which is the folder itself.
        options.append(f"--exclude=/{source.name}/{DATABASE_FOLDER}")
    return rsync_command(remote(settings.server, source), f"{settings.backup_dir}/", *options)


def dump_file(settings: ServerSettings) -> Path:
    return settings.backup_dir / f"{settings.bookorbit_dir.name}-database.sql"


def dump_command(settings: ServerSettings) -> list[str]:
    return ssh_command(
        settings.server,
        "docker", "compose", "--project-directory", str(settings.bookorbit_dir),
        "exec", "-T", DATABASE_SERVICE, "sh", "-c", PG_DUMP,
    )


def _finished(path: Path) -> bool:
    size = path.stat().st_size
    with path.open("rb") as stream:
        stream.seek(max(0, size - DUMP_TAIL_BYTES))
        return DUMP_COMPLETE in stream.read()


def dump_database(settings: ServerSettings, dumper: Dumper) -> Path:
    """Write the dump beside the copies, replacing the last one only once this one is whole."""

    target = dump_file(settings)
    partial = target.with_name(f".{target.name}.partial")
    command = dump_command(settings)
    try:
        result = dumper(command, partial)
        if result.returncode != 0:
            detail = last_lines(result.errors)
            if result.returncode == SSH_UNREACHABLE:
                raise ServerError(f"Could not reach {settings.server} over ssh to dump the database: {detail}")
            raise ServerError(
                f"The database dump failed (exit {result.returncode}); nothing was copied, and the last dump "
                f"was kept. Is BookOrbit running, with its database service named {DATABASE_SERVICE!r} in "
                f"{settings.bookorbit_dir}/docker-compose.yml?" + (f"\n{detail}" if detail else "")
            )
        if not _finished(partial):
            raise ServerError("The database dump ended early; nothing was copied, and the last dump was kept")
        os.replace(partial, target)
    finally:
        # Only ever this run's own half-written file.
        partial.unlink(missing_ok=True)
    return target


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Dump BookOrbit's database, then copy BookOrbit's folder on the server (SERVER_BOOKORBIT_DIR), and the "
            "library (SERVER_LIBRARY_DIR) when it sits elsewhere, into BACKUP_DIR on this PC. "
            "Takes no options: it shows what it will do and asks first. See docs/SERVER.md."
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = default_runner,
    dumper: Dumper = default_dumper,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
    input_fn: Callable[[str], str] = input,
    terminal_fn: Callable[[], bool] = prompts.at_a_terminal,
    log_dir: Path | None = None,
) -> int:
    build_parser().parse_args(argv)
    run = configure_logging("backup-server", log_dir=log_dir)
    LOGGER.info(f"backup server · log file: {run.log_path}")
    prompts.require_terminal(COMMAND, terminal_fn=terminal_fn)

    settings = load_server_settings(REQUIRED, environ, dotenv_path=dotenv_path, command=COMMAND)
    destination = settings.backup_dir
    sources = backup_sources(settings)
    commands = [backup_command(settings, source) for _, source in sources]
    log_banner(LOGGER, "Back up the server", [
        *settings.rows(),
        ("database dump goes to", dump_file(settings)),
        *(("backup goes into", backup_folder(settings, source)) for _, source in sources),
        ("dump command", shlex.join(dump_command(settings))),
        *(("command", shlex.join(command)) for command in commands),
    ])

    for key, source in sources:
        LOGGER.info(f"checking that {source} exists on {settings.server}")
        if not remote_dir_exists(settings.server, source, runner):
            raise ServerError(
                f"{source} does not exist on {settings.server}, so there is nothing to back up. "
                f"Check {key} in {dotenv_path}."
            )
    if destination.exists() and not destination.is_dir():
        raise ServerError(f"{BACKUP_DIR} is {destination}, which is a file, not a folder")

    def ask(question: str, *, default: bool) -> bool:
        return prompts.confirm(question, default=default, logger=LOGGER, input_fn=input_fn, terminal_fn=terminal_fn)

    create = not destination.exists()
    if create:
        LOGGER.warning(f"{destination} does not exist on this PC yet")
        if not ask(f"Create {destination} for the backup?", default=False):
            LOGGER.info("nothing was copied")
            return 0
    if not ask("Start the backup now?", default=True):
        LOGGER.info("nothing was copied")
        return 0
    if create:
        destination.mkdir(parents=True)

    # The dump comes first: it takes seconds, and a database that cannot be
    # dumped is worth knowing about before hours of copying.
    LOGGER.info("dumping BookOrbit's database")
    dumped = dump_database(settings, dumper)
    LOGGER.info(f"database dumped: {dumped} ({dumped.stat().st_size:,} bytes)")

    LOGGER.info("copying; the first backup copies everything and can take hours")
    for (_, source), command in zip(sources, commands):
        LOGGER.info(f"copying {settings.server}:{source}")
        transfer(command, runner, LOGGER)
    folders = ", ".join(str(backup_folder(settings, source)) for _, source in sources)
    LOGGER.info(f"backup complete: {dumped}, {folders}")
    return 0


def run() -> None:
    """Command-line entry point used by ``backup_server.py``."""

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOGGER.warning("stopped; the server was not changed, and the next backup carries on from what was copied")
        raise SystemExit(130) from None
    except ArchiverError as error:
        LOGGER.error(str(error))
        raise SystemExit(2) from error

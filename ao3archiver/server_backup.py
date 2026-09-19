"""Back up the server's BookOrbit folder to this PC: one rsync pull and nothing else.

``SERVER_BOOKORBIT_DIR`` is copied, folder and all, into ``BACKUP_DIR``: a
server folder ``/srv/bookorbit`` becomes ``BACKUP_DIR/bookorbit``. That mirror
is then zipped, every time, into ``BACKUP_DIR/bookorbit-<time>.zip``.

Nothing is deleted on either side, and no zip is ever replaced or removed: a
person purges old ones. The zips are what make this a history rather than a
mirror, since a file replaced, or corrupted, on the server replaces the
mirror's copy on the next run but never an earlier zip's. There is no database
dump and no copying onward: taking the backup to a drive or a cloud folder is
left to the person, on purpose.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
import logging
import os
from pathlib import Path
import shlex
import shutil
import time
import zipfile

from ao3archiver import prompts
from ao3archiver.common import ArchiverError
from ao3archiver.credentials import DEFAULT_DOTENV_PATH
from ao3archiver.run_log import configure_logging, format_duration, log_banner
from ao3archiver.server import (
    BACKUP_DIR,
    default_runner,
    load_server_settings,
    remote,
    remote_dir_exists,
    rsync_command,
    Runner,
    SERVER,
    SERVER_BOOKORBIT_DIR,
    ServerError,
    ServerSettings,
    transfer,
)

LOGGER = logging.getLogger("ao3.backup")

COMMAND = "backup_server.py"
REQUIRED = (SERVER, SERVER_BOOKORBIT_DIR, BACKUP_DIR)
ZIP_PROGRESS_EVERY = 5000
# EPUBs, PDFs and comic archives are compressed already: storing them as they
# are makes the zip barely larger and many times faster to write.
ZIP_METHOD = zipfile.ZIP_STORED
# Room for the zip's own headers on top of the files' bytes.
ZIP_OVERHEAD_BYTES = 64 * 1024 * 1024
ZIP_ENTRY_OVERHEAD_BYTES = 256


@dataclass(frozen=True)
class ZipSummary:
    path: Path
    files: int
    folders: int
    size_bytes: int


def backup_folder(settings: ServerSettings) -> Path:
    """Where the copy lands: a folder in ``BACKUP_DIR`` named like the server's."""

    return settings.backup_dir / settings.bookorbit_dir.name


def backup_command(settings: ServerSettings) -> list[str]:
    """The one pull. No slash after the source, so rsync copies the folder itself, not
    just what is in it: its contents never spill loose into ``BACKUP_DIR``."""

    return rsync_command(
        remote(settings.server, settings.bookorbit_dir),
        f"{settings.backup_dir}/",
        "--stats",
        "--info=progress2",
    )


def new_zip_path(settings: ServerSettings, started: datetime) -> Path:
    """A zip name no earlier backup has used: every zip is kept."""

    stem = f"{settings.bookorbit_dir.name}-{started.strftime('%Y%m%dT%H%M%S')}"
    candidate = settings.backup_dir / f"{stem}.zip"
    suffix = 2
    while candidate.exists():
        candidate = settings.backup_dir / f"{stem}-{suffix}.zip"
        suffix += 1
    return candidate


def _entries(folder: Path) -> list[tuple[Path, bool]]:
    """Every folder and regular file under ``folder``, itself included, in a stable order."""

    entries: list[tuple[Path, bool]] = []
    for directory, subdirectories, files in os.walk(folder, followlinks=False):
        subdirectories.sort()
        entries.append((Path(directory), True))
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_file() and not path.is_symlink():
                entries.append((path, False))
    return entries


def zip_backup(
    folder: Path,
    destination: Path,
    *,
    disk_free: Callable[[Path], int],
    logger: logging.Logger,
) -> ZipSummary:
    """Zip ``folder``, as a folder, and keep the zip only once every file in it checks out.

    It is written under a temporary name and renamed at the end, so a zip that
    exists is always a whole one; an interrupted or failed zip leaves nothing.
    """

    entries = _entries(folder)
    files = [path for path, is_folder in entries if not is_folder]
    size = sum(path.stat().st_size for path in files)
    needed = size + ZIP_OVERHEAD_BYTES + ZIP_ENTRY_OVERHEAD_BYTES * len(entries)
    free = disk_free(destination.parent)
    if free < needed:
        raise ServerError(
            f"Not enough free space for the zip: it needs about {needed / 1e9:.1f} GB in "
            f"{destination.parent}, which has {free / 1e9:.1f} GB. The backup folder itself is complete; "
            "free some space (old zips can go) and run the backup again to zip it."
        )

    temporary = destination.with_name(f".{destination.name}.tmp")
    started = time.monotonic()
    try:
        with zipfile.ZipFile(temporary, "w", compression=ZIP_METHOD, allowZip64=True, strict_timestamps=False) as archive:
            for done, (path, _) in enumerate(entries, start=1):
                archive.write(path, path.relative_to(folder.parent).as_posix())
                if done % ZIP_PROGRESS_EVERY == 0:
                    logger.info(f"  zipped {done}/{len(entries)} entries")
        logger.info("checking every file in the zip")
        with zipfile.ZipFile(temporary) as archive:
            broken = archive.testzip()
            count = len(archive.infolist())
        if broken is not None:
            raise ServerError(f"The zip failed its check at {broken}; it was not kept")
        if count != len(entries):
            raise ServerError(f"The zip holds {count} entries, not {len(entries)}; it was not kept")
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    logger.info(f"zipped in {format_duration(time.monotonic() - started)}")
    return ZipSummary(destination, len(files), len(entries) - len(files), destination.stat().st_size)


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Copy BookOrbit's folder on the server (SERVER_BOOKORBIT_DIR) into BACKUP_DIR on this PC, "
            "then zip it into a new, dated zip beside it. Takes no options: it shows what it will do "
            "and asks first. See docs/SERVER.md."
        ),
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    runner: Runner = default_runner,
    environ: Mapping[str, str] | None = None,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
    input_fn: Callable[[str], str] = input,
    terminal_fn: Callable[[], bool] = prompts.at_a_terminal,
    log_dir: Path | None = None,
    now_fn: Callable[[], datetime] = datetime.now,
    disk_free: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
) -> int:
    build_parser().parse_args(argv)
    started = now_fn()
    run = configure_logging("backup-server", log_dir=log_dir)
    LOGGER.info(f"backup server · log file: {run.log_path}")
    prompts.require_terminal(COMMAND, terminal_fn=terminal_fn)

    settings = load_server_settings(REQUIRED, environ, dotenv_path=dotenv_path, command=COMMAND)
    source, destination = settings.bookorbit_dir, settings.backup_dir
    if not source.name:
        raise ServerError(f"{SERVER_BOOKORBIT_DIR} must name a folder, not the server's root")
    command = backup_command(settings)
    log_banner(LOGGER, "Back up the server", [
        *settings.rows(),
        ("backup goes into", backup_folder(settings)),
        ("then zipped to", new_zip_path(settings, started)),
        ("command", shlex.join(command)),
    ])

    LOGGER.info(f"checking that {source} exists on {settings.server}")
    if not remote_dir_exists(settings.server, source, runner):
        raise ServerError(
            f"{source} does not exist on {settings.server}, so there is nothing to back up. "
            f"Check {SERVER_BOOKORBIT_DIR} in {dotenv_path}."
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

    LOGGER.info("copying; the first backup copies everything and can take hours")
    transfer(command, runner, LOGGER)
    folder = backup_folder(settings)
    if not folder.is_dir():
        raise ServerError(f"rsync reported success but left no folder at {folder}")
    LOGGER.info(f"backup folder complete: {folder}")

    zip_path = new_zip_path(settings, started)
    LOGGER.info(f"zipping it into {zip_path}")
    summary = zip_backup(folder, zip_path, disk_free=disk_free, logger=LOGGER)
    LOGGER.info(
        f"zip complete: {summary.path} ({summary.files} files, {summary.folders} folders, "
        f"{summary.size_bytes / 1e9:.2f} GB)"
    )
    return 0


def run() -> None:
    """Command-line entry point used by ``backup_server.py``."""

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        LOGGER.warning(
            "stopped; the server was not changed, no unfinished zip was kept, and the next backup "
            "carries on from what was copied"
        )
        raise SystemExit(130) from None
    except ArchiverError as error:
        LOGGER.error(str(error))
        raise SystemExit(2) from error

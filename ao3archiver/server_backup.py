"""Back up the server's BookOrbit folder to this PC: one rsync pull and nothing else.

Every file under ``SERVER_BOOKORBIT_DIR`` is copied into ``BACKUP_DIR``, and
nothing is deleted on either side. There is no archive, no database dump and no
copying onward: taking the backup to a drive or a cloud folder is left to the
person, on purpose.

The accepted cost of a plain mirror: a file replaced, or corrupted, on the
server replaces the backup's copy on the next run.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
import logging
from pathlib import Path
import shlex

from ao3archiver import prompts
from ao3archiver.common import ArchiverError
from ao3archiver.credentials import DEFAULT_DOTENV_PATH
from ao3archiver.run_log import configure_logging, log_banner
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


def backup_command(settings: ServerSettings) -> list[str]:
    """The one pull. The trailing slashes copy the folder's contents, not the folder."""

    return rsync_command(
        f"{remote(settings.server, settings.bookorbit_dir)}/",
        f"{settings.backup_dir}/",
        "--stats",
        "--info=progress2",
    )


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        description=(
            "Copy BookOrbit's folder on the server (SERVER_BOOKORBIT_DIR) into BACKUP_DIR on this PC. "
            "Takes no options: it shows what it will do and asks first. See docs/SERVER.md."
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
) -> int:
    build_parser().parse_args(argv)
    run = configure_logging("backup-server", log_dir=log_dir)
    LOGGER.info(f"backup server · log file: {run.log_path}")
    prompts.require_terminal(COMMAND, terminal_fn=terminal_fn)

    settings = load_server_settings(REQUIRED, environ, dotenv_path=dotenv_path, command=COMMAND)
    source, destination = settings.bookorbit_dir, settings.backup_dir
    command = backup_command(settings)
    log_banner(LOGGER, "Back up the server", [*settings.rows(), ("command", shlex.join(command))])

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
    LOGGER.info(f"backup complete: {destination}")
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

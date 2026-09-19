"""The server the library lives on: its settings, and the ssh and rsync commands.

Every value here is an explicit ``.env`` key that only the user writes. Nothing
is guessed and nothing has a default, because rsync pointed at a mistyped path
does not fail: it creates the folder and fills it. So a missing key stops the
run with the exact line to add, and every run prints the values it is using.

Nothing built here deletes anything, locally or on the server: rsync runs with
``-rt`` and never with ``-a`` or any ``--delete``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import io
import logging
import os
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
from typing import Literal, Protocol

from ao3archiver.common import ArchiverError
from ao3archiver.credentials import DEFAULT_DOTENV_PATH, load_dotenv_values

SERVER = "SERVER"
SERVER_BOOKORBIT_DIR = "SERVER_BOOKORBIT_DIR"
SERVER_LIBRARY_DIR = "SERVER_LIBRARY_DIR"
BACKUP_DIR = "BACKUP_DIR"
LIBRARY_IN_CONTAINER = "BOOKORBIT_LIBRARY_PATH_IN_CONTAINER"

SSH_OPTIONS = ("-o", "BatchMode=yes", "-o", "ConnectTimeout=10")
SSH_UNREACHABLE = 255
# -a would copy owners and permissions, which exFAT cannot hold and which could
# leave a file unreadable to the library's container; --delete is never wanted.
RSYNC_BASE = ("rsync", "-rt", "--protect-args")
FORBIDDEN_RSYNC_OPTIONS = ("-a", "--archive", "--delete", "--del", "--remove-source-files")
RSYNC_SUMMARY_START = "Number of files:"
# rsync's "some files vanished before they could be transferred": something
# wrote while the copy ran. Everything else was copied.
RSYNC_VANISHED = 24


class ServerError(ArchiverError):
    """The server settings are unusable, or the server did not do what was asked."""


@dataclass(frozen=True)
class Setting:
    """One ``.env`` key: what it is for, and a placeholder showing its shape."""

    key: str
    meaning: str
    example: str
    # How the value is checked: an ssh alias, an absolute path on the server
    # or inside BookOrbit's container, or a folder on this PC (where ~ is expanded).
    kind: Literal["alias", "server", "container", "local"]


SETTINGS: tuple[Setting, ...] = (
    Setting(SERVER, "The server's alias in ~/.ssh/config", "myserver", "alias"),
    Setting(
        SERVER_BOOKORBIT_DIR,
        "BookOrbit's folder on the server; backup_server.py copies all of it",
        "/path/to/bookorbit",
        "server",
    ),
    Setting(
        SERVER_LIBRARY_DIR,
        "The folder on the server that send_to_server.py adds books to, inside SERVER_BOOKORBIT_DIR",
        "/path/to/bookorbit/fanfiction",
        "server",
    ),
    Setting(
        LIBRARY_IN_CONTAINER,
        "SERVER_LIBRARY_DIR as BookOrbit's container sees it, the start of the file paths BookOrbit reports",
        "/books/fanfiction",
        "container",
    ),
    Setting(BACKUP_DIR, "The folder on this PC the backup goes into", "~/Backups/bookorbit", "local"),
)
SETTINGS_BY_KEY = {setting.key: setting for setting in SETTINGS}
ALL_KEYS = tuple(setting.key for setting in SETTINGS)


@dataclass(frozen=True)
class ServerSettings:
    """The settings one command runs with, already checked and normalised."""

    values: Mapping[str, str]
    source: str

    def value(self, key: str) -> str:
        if key not in self.values:
            raise KeyError(f"{key} was not loaded; pass it to load_server_settings")
        return self.values[key]

    @property
    def server(self) -> str:
        return self.value(SERVER)

    @property
    def bookorbit_dir(self) -> PurePosixPath:
        return PurePosixPath(self.value(SERVER_BOOKORBIT_DIR))

    @property
    def library_dir(self) -> PurePosixPath:
        return PurePosixPath(self.value(SERVER_LIBRARY_DIR))

    @property
    def library_in_container(self) -> PurePosixPath:
        return PurePosixPath(self.value(LIBRARY_IN_CONTAINER))

    @property
    def backup_dir(self) -> Path:
        return Path(self.value(BACKUP_DIR))

    def rows(self) -> tuple[tuple[str, str], ...]:
        """The values in use, for ``log_banner``. None of these keys holds a secret."""

        loaded = [(setting.key, self.values[setting.key]) for setting in SETTINGS if setting.key in self.values]
        return (*loaded, ("settings from", self.source))


def missing_lines(keys: Sequence[str]) -> str:
    """The lines to add to ``.env``, each under a comment saying what it is."""

    lines = []
    for key in keys:
        setting = SETTINGS_BY_KEY[key]
        lines += [f"    # {setting.meaning}, for example: {setting.example}", f"    {key}="]
    return "\n".join(lines)


def _normalise(setting: Setting, raw: str) -> str:
    value = raw.strip()
    if setting.kind == "alias":
        # The alias lands in front of ssh's own options and in rsync's
        # host:path, so anything that could be read as either is refused.
        if value.startswith("-") or any(character in value for character in " \t:/"):
            raise ServerError(f"{setting.key} must be an ssh alias such as {setting.example}, not {value!r}")
        return value
    if setting.kind in ("server", "container"):
        if not value.startswith("/"):
            raise ServerError(f"{setting.key} must be an absolute path, such as {setting.example}, not {value!r}")
        return str(PurePosixPath(value))
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ServerError(f"{setting.key} must be an absolute path or start with ~, not {value!r}")
    return str(path)


def _read_values(
    required: Sequence[str], environ: Mapping[str, str] | None, dotenv_path: Path
) -> tuple[dict[str, str], list[str]]:
    values = load_dotenv_values(dotenv_path, keys=required)
    process_values = os.environ if environ is None else environ
    from_environment = [key for key in required if key in process_values]
    for key in from_environment:
        values[key] = process_values[key]
    return values, from_environment


def missing_settings(
    required: Sequence[str] = ALL_KEYS,
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
) -> tuple[str, ...]:
    """The keys that are unset or empty, for a setup check that must not fail or stop."""

    values, _ = _read_values(required, environ, dotenv_path)
    return tuple(key for key in required if not (values.get(key) or "").strip())


def load_server_settings(
    required: Sequence[str],
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
    command: str = "This command",
) -> ServerSettings:
    """Read the named keys, the environment winning over ``.env``, as the BookOrbit settings do.

    Every key in ``required`` must be set and non-empty. Nothing is written to
    ``.env`` and nothing is filled in on the user's behalf.
    """

    values, from_environment = _read_values(required, environ, dotenv_path)
    missing = [key for key in required if not (values.get(key) or "").strip()]
    if missing:
        raise ServerError(
            f"{command} needs settings that are not in {dotenv_path}. It never writes that file; "
            f"add these lines to it yourself:\n\n{missing_lines(missing)}"
        )

    normalised = {key: _normalise(SETTINGS_BY_KEY[key], values[key]) for key in required}
    if SERVER_LIBRARY_DIR in normalised and SERVER_BOOKORBIT_DIR in normalised:
        # A book sent outside the backed-up folder would be a book with no backup.
        if not PurePosixPath(normalised[SERVER_LIBRARY_DIR]).is_relative_to(normalised[SERVER_BOOKORBIT_DIR]):
            raise ServerError(
                f"{SERVER_LIBRARY_DIR} ({normalised[SERVER_LIBRARY_DIR]}) must be inside "
                f"{SERVER_BOOKORBIT_DIR} ({normalised[SERVER_BOOKORBIT_DIR]}), so the backup covers what is sent"
            )
    if from_environment:
        source = f"process environment ({', '.join(from_environment)}) over {dotenv_path}"
    else:
        source = str(dotenv_path)
    return ServerSettings(values=normalised, source=source)


def library_relative(container_path: str, settings: ServerSettings) -> PurePosixPath:
    """Where a file BookOrbit reports sits, relative to the library folder.

    BookOrbit reports paths inside its container (``/books/fanfiction/1.epub``),
    and the server knows the same file by another root; the explicit
    ``BOOKORBIT_LIBRARY_PATH_IN_CONTAINER`` is what joins the two.
    """

    path = PurePosixPath(container_path)
    if not path.is_relative_to(settings.library_in_container) or path == settings.library_in_container:
        raise ServerError(f"{container_path} is not inside {LIBRARY_IN_CONTAINER} ({settings.library_in_container})")
    return path.relative_to(settings.library_in_container)


def host_path(container_path: str, settings: ServerSettings) -> PurePosixPath:
    """The server's own path for a file BookOrbit reports."""

    return settings.library_dir / library_relative(container_path, settings)


# --- running commands ----------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    """How a command ended, and everything it printed, stdout and stderr together."""

    returncode: int
    output: str


class Runner(Protocol):
    def __call__(self, args: Sequence[str], *, echo: Callable[[str], None] | None = None) -> RunResult: ...


def default_runner(args: Sequence[str], *, echo: Callable[[str], None] | None = None) -> RunResult:
    """Run a command to completion, handing each line to ``echo`` as it arrives.

    Lines keep their own endings, and a carriage return ends a line too, so a
    progress display that redraws itself still redraws when echoed. There is
    no timeout: a first backup can take hours.
    """

    try:
        process = subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    except FileNotFoundError as error:
        raise ServerError(f"{args[0]} is not installed, or not on PATH") from error
    lines: list[str] = []
    with process:
        assert process.stdout is not None
        stream = io.TextIOWrapper(process.stdout, encoding="utf-8", errors="replace", newline="")
        for line in stream:
            lines.append(line)
            if echo is not None:
                echo(line)
    return RunResult(process.returncode, "".join(lines))


def last_lines(output: str, count: int = 5) -> str:
    """The end of a command's output, for an error message; progress redraws count as lines."""

    lines = [line.strip() for line in output.replace("\r", "\n").splitlines() if line.strip()]
    return "\n".join(lines[-count:])


def ssh_command(server: str, *remote: str) -> list[str]:
    """``ssh`` hands the server one command line for its shell, so quote it here.

    Without the quoting, a path with a space in it would reach ``test`` as two
    arguments.
    """

    return ["ssh", *SSH_OPTIONS, server, shlex.join(remote)]


def remote_dir_exists(server: str, path: PurePosixPath, runner: Runner) -> bool:
    """Whether ``path`` is a folder on the server. An unreachable server is an error, not a no."""

    result = runner(ssh_command(server, "test", "-d", str(path)))
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = last_lines(result.output)
    if result.returncode == SSH_UNREACHABLE:
        raise ServerError(
            f"Could not reach {server} over ssh. Check that `ssh {server}` works from this terminal"
            + (f":\n{detail}" if detail else ".")
        )
    raise ServerError(
        f"Checking for {path} on {server} failed (exit {result.returncode})" + (f":\n{detail}" if detail else "")
    )


def rsync_summary(output: str) -> list[str]:
    """rsync's ``--stats`` block, from its first line to the end."""

    lines = output.replace("\r", "\n").splitlines()
    for index, line in enumerate(lines):
        if line.startswith(RSYNC_SUMMARY_START):
            return [line.strip() for line in lines[index:] if line.strip()]
    return []


class ProgressEcho:
    """Show rsync's progress on the terminal; its summary goes to the run log instead.

    The progress redraws itself many times a second, which would bury a log
    file, and the summary would appear twice if it were echoed as well as
    logged.
    """

    def __init__(self) -> None:
        self.in_summary = False
        self.mid_line = False

    def __call__(self, line: str) -> None:
        if line.startswith(RSYNC_SUMMARY_START):
            self.in_summary = True
        if self.in_summary:
            return
        sys.stdout.write(line)
        sys.stdout.flush()
        self.mid_line = line.endswith("\r")

    def finish(self) -> None:
        """End a progress line left open, so the next log line starts clean."""

        if self.mid_line:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.mid_line = False


def transfer(command: Sequence[str], runner: Runner, logger: logging.Logger) -> RunResult:
    """Run an rsync copy with its progress on the terminal and its summary in the log."""

    echo = ProgressEcho()
    result = runner(command, echo=echo)
    echo.finish()
    summary = rsync_summary(result.output)
    if summary:
        logger.info("rsync summary:")
        for line in summary:
            logger.info(f"  {line}")
    if result.returncode == RSYNC_VANISHED:
        logger.warning("some files changed while they were being copied; the next run picks them up")
    elif result.returncode != 0:
        raise ServerError(f"rsync stopped with exit code {result.returncode}:\n{last_lines(result.output)}")
    return result


def remote(server: str, path: PurePosixPath) -> str:
    """rsync's ``host:path``; ``--protect-args`` keeps the server's shell from splitting it."""

    return f"{server}:{path}"


def rsync_command(source: str, destination: str, *options: str) -> list[str]:
    """An rsync that copies names and times, and can never delete anything."""

    for option in options:
        if option in FORBIDDEN_RSYNC_OPTIONS or option.startswith("--delete"):
            raise ValueError(f"rsync {option} is never used here: nothing is ever deleted")
    return [*RSYNC_BASE, *options, source, destination]

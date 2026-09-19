"""The front door: what this project does, whether it is set up, and what to run.

Deliberately a teacher rather than a wrapper. Every flow prints the exact
command it would run before offering to run it, because the commands are what
the guides in ``docs/`` document and what an unattended run needs. Each of the
three scripts it offers shows its own plan and asks before it changes
anything, so running one from here is as safe as running it by hand. Nothing
here is required: the scripts behave the same with or without this screen.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import subprocess

from ao3archiver import prompts
from ao3archiver.bookorbit import BookOrbitError, load_bookorbit_config
from ao3archiver.common import ArchiverError, REPO_ROOT
from ao3archiver.credentials import CredentialError, DEFAULT_DOTENV_PATH, load_ao3_credentials
from ao3archiver.download import DEFAULT_LINKS_DIR, DEFAULT_OUTPUT_DIR, load_link_inventory
from ao3archiver.server import ALL_KEYS, load_server_settings, missing_settings, ServerError

SUMMARY = (
    "Downloads Archive of Our Own works as EPUBs with their AO3 statistics baked in,",
    "sends them to your library server, and backs that server up.",
)

DOCS = (
    ("docs/TUTORIAL.md", "the whole pipeline, end to end"),
    ("docs/DOWNLOADING.md", "download_fics.py: the plan, owned works, unattended runs"),
    ("docs/SERVER.md", "send_to_server.py and backup_server.py"),
    ("docs/BOOKORBIT.md", "BookOrbit's custom fields and folder watch"),
    ("docs/CONFIGURATION.md", ".env keys, paths, and the SSH alias"),
    ("docs/BACKFILL.md", "the retired Calibre backfill"),
)

MARKS = {True: "ok", False: "--", None: "  "}


@dataclass(frozen=True)
class Check:
    """One line of the setup check. ``ok`` is None for an optional part."""

    label: str
    ok: bool | None
    detail: str


@dataclass(frozen=True)
class Flow:
    """One thing someone might want to do, and the commands that do it."""

    title: str
    explanation: tuple[str, ...]
    commands: tuple[str, ...] = ()
    # The script offered to run from here. Each one prints its plan and asks
    # before it changes anything, so offering it is safe.
    offer: str | None = None


def server_check(
    environ: Mapping[str, str] | None = None,
    *,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
) -> Check:
    """Whether the server settings are all there and well formed; offline, like every check here."""

    missing = missing_settings(ALL_KEYS, environ, dotenv_path=dotenv_path)
    if missing:
        return Check("Server settings", False, f"not set: {', '.join(missing)}")
    try:
        settings = load_server_settings(ALL_KEYS, environ, dotenv_path=dotenv_path)
    except ServerError as error:
        return Check("Server settings", False, str(error).splitlines()[0])
    return Check("Server settings", True, f"{settings.server}:{settings.library_dir}")


def setup_checks(
    *,
    dotenv_path: Path = DEFAULT_DOTENV_PATH,
    environ: Mapping[str, str] | None = None,
    links_dir: Path = DEFAULT_LINKS_DIR,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Check, ...]:
    """Answer, one line each, every question someone asks before their first run."""

    checks = [Check(".env", dotenv_path.is_file(), str(dotenv_path) if dotenv_path.is_file() else "copy .env.example to .env")]

    try:
        credentials = load_ao3_credentials(environ, dotenv_path=dotenv_path)
        checks.append(Check("AO3 credentials", True, f"from {credentials.source}"))
    except (CredentialError, OSError) as error:
        checks.append(Check("AO3 credentials", False, str(error).split(".")[0]))

    try:
        config = load_bookorbit_config(environ, dotenv_path=dotenv_path)
        checks.append(
            Check("BookOrbit", None, "not configured (send_to_server.py needs it)")
            if config is None
            else Check("BookOrbit", True, config.base_url)
        )
    except (BookOrbitError, OSError) as error:
        checks.append(Check("BookOrbit", False, str(error)))

    checks.append(server_check(environ, dotenv_path=dotenv_path))

    if links_dir.is_dir():
        inventory = load_link_inventory(links_dir)
        checks.append(
            Check(
                "links/",
                bool(inventory.targets),
                f"{len(inventory.targets)} works in {inventory.files} file(s)"
                if inventory.targets
                else f"no work links in {links_dir}",
            )
        )
    else:
        checks.append(Check("links/", False, f"no {links_dir} yet"))

    downloaded = len(list(output_dir.glob("*.epub"))) if output_dir.is_dir() else 0
    checks.append(Check("downloaded/", downloaded > 0, f"{downloaded} EPUB(s)"))
    return tuple(checks)


def flows() -> tuple[Flow, ...]:
    """The things this project does, in the order someone does them."""

    return (
        Flow(
            "Harvest links from AO3",
            (
                "ao3downloader (github.com/nianeyna/ao3downloader) saves every work link",
                "from an AO3 search as a .txt file. Copy those files into links/.",
                "Nothing in this project needs to run for this step.",
            ),
        ),
        Flow(
            "Download the works",
            (
                "Reads each link and asks BookOrbit which works are already on the server.",
                "New works are downloaded; for unfinished ones it offers to check AO3 for new",
                "chapters. Each EPUB gets its AO3 statistics, a local word count and a",
                "readability score written into it. It shows the plan and asks before starting;",
                "under nohup it takes the defaults, for runs that last days.",
            ),
            commands=("python3 download_fics.py", "nohup python3 download_fics.py &"),
            offer="python3 download_fics.py",
        ),
        Flow(
            "Send them to the server",
            (
                "Adds new works to the library folder on the server and replaces the files of",
                "works BookOrbit already holds, in place. Adding and replacing are asked",
                "separately. Then it waits for BookOrbit to pick up the new books and fills in",
                "their custom fields. Deletes nothing; empty downloaded/ yourself.",
            ),
            commands=("python3 send_to_server.py",),
            offer="python3 send_to_server.py",
        ),
        Flow(
            "Back up the server",
            (
                "Copies BookOrbit's whole folder on the server into a folder of the same name",
                "in BACKUP_DIR on this PC, with one rsync. The first run copies everything;",
                "later runs only what changed.",
            ),
            commands=("python3 backup_server.py",),
            offer="python3 backup_server.py",
        ),
        Flow(
            "Read the documentation",
            tuple(f"{path:<24} {what}" for path, what in DOCS),
        ),
    )


def render(lines: Sequence[str], emit: Callable[[str], None]) -> None:
    for line in lines:
        emit(line)


def screen(checks: Sequence[Check]) -> tuple[str, ...]:
    """The banner and the setup check, as printed lines."""

    width = max(len(check.label) for check in checks)
    return (
        "",
        "ao3Archiver",
        *(f"  {line}" for line in SUMMARY),
        "",
        "Setup",
        *(f"  [{MARKS[check.ok]}]  {check.label.ljust(width)}  {check.detail}" for check in checks),
    )


def describe(flow: Flow) -> tuple[str, ...]:
    lines = ["", flow.title, *(f"  {line}" for line in flow.explanation)]
    if flow.commands:
        lines.append("")
        lines += [f"  $ {command}" for command in flow.commands]
    return tuple(lines)


def run_command(command: str, *, runner: Callable[[Sequence[str]], int] | None = None) -> int:
    """Run a printed command from the repository root, exactly as it was shown."""

    if runner is None:
        return subprocess.call(command.split(), cwd=REPO_ROOT)
    return runner(command.split())


def main(
    argv: Sequence[str] | None = None,
    *,
    emit: Callable[[str], None] = print,
    runner: Callable[[Sequence[str]], int] | None = None,
    assume_yes: bool = False,
) -> int:
    """Show the setup check and the menu, and keep showing it until asked to stop."""

    render(screen(setup_checks()), emit)
    available = flows()
    titles = tuple(flow.title for flow in available)

    while True:
        chosen = prompts.choose("What would you like to do?", titles, assume_yes=assume_yes)
        if chosen is None:
            if not prompts.at_a_terminal() or assume_yes:
                # Nobody is choosing: list what there is and leave.
                render(("", "What this project does:", *(f"  - {title}" for title in titles), ""), emit)
            return 0
        flow = available[chosen]
        render(describe(flow), emit)
        emit("")
        if flow.offer is None:
            continue
        if prompts.confirm(f"Run `{flow.offer}` now?", default=False, assume_yes=assume_yes):
            run_command(flow.offer, runner=runner)


def run() -> None:
    """Command-line entry point used by ``start_here.py``."""

    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except ArchiverError as error:
        print(error)
        raise SystemExit(2) from error

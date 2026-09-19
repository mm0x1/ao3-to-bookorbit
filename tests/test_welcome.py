"""Tests for the front door: the setup check, the menu, and the commands it prints."""

from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import requests

from ao3archiver import welcome


def marks(checks):
    return {check.label: check.ok for check in checks}


def details(checks):
    return {check.label: check.detail for check in checks}


SERVER_SETTINGS = (
    "SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/bookorbit\nSERVER_LIBRARY_DIR=/srv/bookorbit/fanfiction\n"
    "BOOKORBIT_LIBRARY_PATH_IN_CONTAINER=/books/fanfiction\nBACKUP_DIR=/backups/bookorbit\n"
)


class ServerCheckTest(unittest.TestCase):
    def check(self, text):
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(text, encoding="utf-8")
            return welcome.server_check({}, dotenv_path=dotenv)

    def test_every_setting_present_is_ok(self):
        check = self.check(SERVER_SETTINGS)

        self.assertTrue(check.ok)
        self.assertEqual(check.detail, "myserver:/srv/bookorbit/fanfiction")

    def test_the_missing_settings_are_named(self):
        check = self.check("SERVER=myserver\n")

        self.assertFalse(check.ok)
        self.assertIn("SERVER_BOOKORBIT_DIR", check.detail)
        self.assertIn("BACKUP_DIR", check.detail)
        self.assertNotIn("SERVER,", check.detail)

    def test_a_setting_a_command_would_refuse_is_flagged_here_first(self):
        check = self.check(
            SERVER_SETTINGS.replace("SERVER_BOOKORBIT_DIR=/srv/bookorbit", "SERVER_BOOKORBIT_DIR=bookorbit")
        )

        self.assertFalse(check.ok)
        self.assertIn("absolute path", check.detail)


class SetupCheckTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self):
        self._directory.cleanup()

    def checks(self, **options):
        defaults = {
            "dotenv_path": self.root / ".env",
            "ini_path": self.root / "personal.ini",
            "environ": {},
            "links_dir": self.root / "links",
            "output_dir": self.root / "downloaded",
        }
        return welcome.setup_checks(**{**defaults, **options})

    def test_a_bare_clone_says_what_is_missing_without_failing(self):
        checks = self.checks()

        self.assertEqual(marks(checks)[".env"], False)
        self.assertEqual(marks(checks)["AO3 credentials"], False)
        self.assertEqual(marks(checks)["links/"], False)
        self.assertIn(".env.example", details(checks)[".env"])

    def test_credentials_are_reported_by_source_and_never_by_value(self):
        (self.root / ".env").write_text("AO3_USERNAME=reader\nAO3_PASSWORD=hunter2\n", encoding="utf-8")

        checks = self.checks()

        self.assertEqual(marks(checks)["AO3 credentials"], True)
        self.assertNotIn("hunter2", "".join(details(checks).values()))

    def test_an_unused_bookorbit_is_neither_right_nor_wrong(self):
        checks = self.checks()

        self.assertIsNone(marks(checks)["BookOrbit"], "the download works without it")
        self.assertIn("send_to_server.py", details(checks)["BookOrbit"])

    def test_calibre_is_no_longer_part_of_the_setup(self):
        self.assertNotIn("Calibre library", marks(self.checks()))

    def test_missing_server_settings_are_reported_not_fatal(self):
        self.assertEqual(marks(self.checks())["Server settings"], False)

    def test_the_check_stays_offline_with_everything_configured(self):
        """The screen must be instant even when the server is down."""

        (self.root / ".env").write_text(
            SERVER_SETTINGS + "BOOKORBIT_URL=http://books.example\nBOOKORBIT_USERNAME=a\nBOOKORBIT_PASSWORD=b\n",
            encoding="utf-8",
        )

        def offline(*args, **options):
            raise AssertionError("the setup check went to the network")

        with patch.object(subprocess, "Popen", offline), patch.object(requests.Session, "request", offline):
            checks = self.checks()

        self.assertEqual(marks(checks)["Server settings"], True)
        self.assertEqual(marks(checks)["BookOrbit"], True)

    def test_links_and_downloads_are_counted(self):
        links = self.root / "links"
        links.mkdir()
        (links / "a.txt").write_text(
            "https://archiveofourown.org/works/111\nhttps://archiveofourown.org/works/222\n",
            encoding="utf-8",
        )
        downloaded = self.root / "downloaded"
        downloaded.mkdir()
        (downloaded / "111.epub").write_bytes(b"")

        checks = self.checks()

        self.assertEqual(marks(checks)["links/"], True)
        self.assertIn("2 works", details(checks)["links/"])
        self.assertIn("1 EPUB", details(checks)["downloaded/"])


class FlowTest(unittest.TestCase):
    def test_the_menu_is_the_pipeline_in_order(self):
        self.assertEqual(
            [flow.title for flow in welcome.flows()],
            [
                "Harvest links from AO3",
                "Download the works",
                "Send them to the server",
                "Back up the server",
                "Read the documentation",
            ],
        )

    def test_each_of_the_three_scripts_is_offered_since_each_asks_first(self):
        offered = [flow.offer for flow in welcome.flows() if flow.offer is not None]

        self.assertEqual(
            offered, ["python3 download_fics.py", "python3 send_to_server.py", "python3 backup_server.py"]
        )
        for command in offered:
            self.assertTrue((welcome.REPO_ROOT / command.split()[1]).is_file(), command)

    def test_an_offered_command_is_one_of_the_printed_ones(self):
        for flow in welcome.flows():
            if flow.offer is not None:
                self.assertIn(flow.offer, flow.commands, flow.title)

    def test_the_retired_backfill_is_documented_not_offered(self):
        text = "\n".join(line for flow in welcome.flows() for line in (*flow.explanation, *flow.commands))

        self.assertNotIn("backfill.py", "\n".join(command for flow in welcome.flows() for command in flow.commands))
        self.assertIn("docs/BACKFILL.md", text)


class MenuTest(unittest.TestCase):
    """The menu is tested on its own; the machine's real .env never takes part."""

    CHECKS = (welcome.Check(".env", True, "/somewhere/.env"),)

    def setUp(self):
        self.lines: list[str] = []
        self.ran: list[list[str]] = []
        patcher = patch.object(welcome, "setup_checks", lambda **options: self.CHECKS)
        patcher.start()
        self.addCleanup(patcher.stop)

    def emit(self, line):
        self.lines.append(line)

    def runner(self, command):
        self.ran.append(list(command))
        return 0

    def main(self, *, choices=(), confirmations=()):
        chosen = iter(choices)
        answers = iter(confirmations)
        with patch.object(welcome.prompts, "choose", lambda *args, **options: next(chosen, None)), patch.object(
            welcome.prompts, "confirm", lambda *args, **options: next(answers, False)
        ), patch.object(welcome.prompts, "at_a_terminal", lambda: True):
            return welcome.main(emit=self.emit, runner=self.runner)

    def text(self):
        return "\n".join(self.lines)

    def test_quitting_straight_away_runs_nothing(self):
        self.assertEqual(self.main(), 0)
        self.assertEqual(self.ran, [])
        self.assertIn("Setup", self.text())

    def test_a_chosen_flow_prints_the_exact_command_before_offering_to_run_it(self):
        self.main(choices=(1,), confirmations=(False,))

        self.assertIn("$ python3 download_fics.py\n", self.text() + "\n")
        self.assertIn("$ nohup python3 download_fics.py &", self.text())
        self.assertEqual(self.ran, [], "a declined offer runs nothing")

    def test_accepting_runs_exactly_the_command_that_was_printed(self):
        self.main(choices=(1,), confirmations=(True,))

        self.assertEqual(self.ran, [["python3", "download_fics.py"]])

    def test_a_flow_with_nothing_to_run_just_explains_itself(self):
        self.main(choices=(0,))

        self.assertIn("ao3downloader", self.text())
        self.assertEqual(self.ran, [])

    def test_off_a_terminal_it_prints_what_it_can_and_stops(self):
        with patch.object(welcome.prompts, "at_a_terminal", lambda: False):
            exit_code = welcome.main(emit=self.emit, runner=self.runner)

        self.assertEqual(exit_code, 0)
        self.assertIn("Download the works", self.text())
        self.assertEqual(self.ran, [])

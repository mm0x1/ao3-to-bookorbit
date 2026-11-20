"""Tests for backup_server.py: one rsync pull, asked for at a terminal and never anywhere else."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path
import shlex
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ao3archiver import server, server_backup
from ao3archiver.common import ArchiverError
from tests.support import Answers, close_run_logs, FakeServer, local_runner, local_ssh, RSYNC_STATS


DUMP = b"-- PostgreSQL database dump\nCREATE TABLE book ();\n-- PostgreSQL database dump complete\n\\unrestrict x\n"


class FakeDumper:
    """Stands in for the ssh that streams pg_dump: writes the given bytes and records the command."""

    def __init__(self, content: bytes = DUMP, returncode: int = 0, errors: str = "") -> None:
        self.content = content
        self.returncode = returncode
        self.errors = errors
        self.calls: list[list[str]] = []

    def __call__(self, args, into):
        self.calls.append(list(args))
        into.write_bytes(self.content)
        return server_backup.DumpResult(self.returncode, self.errors)


class BackupTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.backup = self.root / "backup"
        self.dotenv = self.root / ".env"
        self.dotenv.write_text(
            f"SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/bookorbit\n"
            f"SERVER_LIBRARY_DIR=/srv/bookorbit/fanfiction\nBACKUP_DIR={self.backup}\n",
            encoding="utf-8",
        )
        self.output = ""
        self.dumper = FakeDumper()

    def tearDown(self):
        close_run_logs()
        self._directory.cleanup()

    def main(self, runner, *answers, terminal=True):
        self.answers = Answers(*answers)
        with redirect_stdout(io.StringIO()) as stream:
            try:
                return server_backup.main(
                    [],
                    runner=runner,
                    dumper=self.dumper,
                    environ={},
                    dotenv_path=self.dotenv,
                    input_fn=self.answers,
                    terminal_fn=lambda: terminal,
                    log_dir=self.root / "logs",
                )
            finally:
                self.output = stream.getvalue()

    def use_library(self, library):
        text = self.dotenv.read_text(encoding="utf-8")
        self.dotenv.write_text(
            text.replace("SERVER_LIBRARY_DIR=/srv/bookorbit/fanfiction", f"SERVER_LIBRARY_DIR={library}"),
            encoding="utf-8",
        )

    def log_text(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in (self.root / "logs").glob("*.log"))

    def test_off_a_terminal_it_refuses_before_touching_the_server(self):
        fake = FakeServer(folders=["/srv/bookorbit"])

        with self.assertRaises(ArchiverError):
            self.main(fake, terminal=False)

        self.assertEqual(fake.calls, [])

    def test_answering_yes_runs_exactly_one_pull_of_the_whole_folder_but_the_database_files(self):
        self.backup.mkdir()
        fake = FakeServer(folders=["/srv/bookorbit"])

        self.assertEqual(self.main(fake, "y"), 0)

        self.assertEqual(fake.rsync_calls, [[
            "rsync", "-rt", "--protect-args", "--stats", "--info=progress2",
            "--exclude=/bookorbit/data/postgres",
            "myserver:/srv/bookorbit", f"{self.backup}/",
        ]], "no slash after the source: the folder itself is copied, not just its contents")

    def test_the_database_is_dumped_inside_its_container_and_streamed_here(self):
        self.backup.mkdir()

        self.main(FakeServer(folders=["/srv/bookorbit"]), "y")

        [command] = self.dumper.calls
        self.assertEqual(command[:6], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "myserver"])
        self.assertEqual(shlex.split(command[-1]), [
            "docker", "compose", "--project-directory", "/srv/bookorbit", "exec", "-T", "postgres",
            "sh", "-c", 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"',
        ])
        self.assertEqual((self.backup / "bookorbit-database.sql").read_bytes(), DUMP)
        self.assertEqual(sorted(path.name for path in self.backup.iterdir()), ["bookorbit-database.sql"],
                         "no half-written file is left behind")

    def test_a_failed_dump_keeps_the_last_one_and_copies_nothing(self):
        self.backup.mkdir()
        (self.backup / "bookorbit-database.sql").write_bytes(b"last good dump")
        self.dumper = FakeDumper(b"", returncode=1, errors="service \"postgres\" is not running\n")
        fake = FakeServer(folders=["/srv/bookorbit"])

        with self.assertRaises(server.ServerError) as caught:
            self.main(fake, "y")

        self.assertIn("is not running", str(caught.exception))
        self.assertEqual((self.backup / "bookorbit-database.sql").read_bytes(), b"last good dump")
        self.assertEqual(sorted(path.name for path in self.backup.iterdir()), ["bookorbit-database.sql"])
        self.assertEqual(fake.rsync_calls, [])

    def test_a_dump_cut_short_is_refused_even_when_ssh_says_it_succeeded(self):
        self.backup.mkdir()
        (self.backup / "bookorbit-database.sql").write_bytes(b"last good dump")
        self.dumper = FakeDumper(b"-- PostgreSQL database dump\nCREATE TABLE bo")

        with self.assertRaises(server.ServerError) as caught:
            self.main(FakeServer(folders=["/srv/bookorbit"]), "y")

        self.assertIn("ended early", str(caught.exception))
        self.assertEqual((self.backup / "bookorbit-database.sql").read_bytes(), b"last good dump")

    def test_answering_no_dumps_nothing(self):
        self.backup.mkdir()

        self.main(FakeServer(folders=["/srv/bookorbit"]), "n")

        self.assertEqual(self.dumper.calls, [])

    def test_a_library_on_another_drive_is_pulled_too_into_a_folder_of_its_own(self):
        self.use_library("/mnt/disk/books/fanfiction")
        self.backup.mkdir()
        fake = FakeServer(folders=["/srv/bookorbit", "/mnt/disk/books/fanfiction"])

        self.assertEqual(self.main(fake, "y"), 0)

        self.assertNotIn("--exclude=/fanfiction/data/postgres", fake.rsync_calls[1])
        self.assertEqual([call[-2:] for call in fake.rsync_calls], [
            ["myserver:/srv/bookorbit", f"{self.backup}/"],
            ["myserver:/mnt/disk/books/fanfiction", f"{self.backup}/"],
        ])
        self.assertEqual(len(self.answers.questions), 1, "one question covers both pulls")
        self.assertRegex(self.output, rf"backup goes into\s+{self.backup}/fanfiction\n")

    def test_a_library_on_another_drive_missing_on_the_server_is_refused_by_name(self):
        self.use_library("/mnt/disk/books/fanfiction")
        self.backup.mkdir()
        fake = FakeServer(folders=["/srv/bookorbit"])

        with self.assertRaises(server.ServerError) as caught:
            self.main(fake, "y")

        self.assertIn("SERVER_LIBRARY_DIR", str(caught.exception))
        self.assertEqual(fake.rsync_calls, [])

    def test_two_folders_with_one_name_are_refused_since_their_backups_would_mix(self):
        self.use_library("/mnt/disk/bookorbit")
        fake = FakeServer(folders=["/srv/bookorbit", "/mnt/disk/bookorbit"])

        with self.assertRaises(server.ServerError) as caught:
            self.main(fake, "y")

        self.assertIn("mix", str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_answering_no_copies_nothing(self):
        self.backup.mkdir()
        fake = FakeServer(folders=["/srv/bookorbit"])

        self.main(fake, "n")

        self.assertEqual(fake.rsync_calls, [])

    def test_the_values_in_use_and_the_command_are_shown_before_the_question(self):
        self.backup.mkdir()

        self.main(FakeServer(folders=["/srv/bookorbit"]), "n")

        self.assertIn("SERVER_BOOKORBIT_DIR", self.output)
        self.assertIn("/srv/bookorbit", self.output)
        self.assertIn(
            "rsync -rt --protect-args --stats --info=progress2 --exclude=/bookorbit/data/postgres "
            f"myserver:/srv/bookorbit {self.backup}/",
            self.output,
        )
        self.assertRegex(self.output, rf"database dump goes to\s+{self.backup}/bookorbit-database.sql\n")
        self.assertRegex(self.output, rf"backup goes into\s+{self.backup}/bookorbit\n")

    def test_a_folder_missing_on_the_server_is_refused(self):
        self.backup.mkdir()
        fake = FakeServer(folders=[])

        with self.assertRaises(server.ServerError) as caught:
            self.main(fake, "y")

        self.assertIn("SERVER_BOOKORBIT_DIR", str(caught.exception))
        self.assertEqual(fake.rsync_calls, [])
        self.assertEqual(self.answers.questions, [], "nothing is asked about a backup that cannot run")

    def test_the_servers_root_is_refused_since_it_names_no_folder(self):
        self.dotenv.write_text(f"SERVER=myserver\nSERVER_BOOKORBIT_DIR=/\nSERVER_LIBRARY_DIR=/srv/fanfiction\nBACKUP_DIR={self.backup}\n", encoding="utf-8")
        fake = FakeServer(folders=["/"])

        with self.assertRaises(server.ServerError):
            self.main(fake, "y")

        self.assertEqual(fake.calls, [])

    def test_an_unreachable_server_is_a_clear_error(self):
        self.backup.mkdir()

        with self.assertRaises(server.ServerError) as caught:
            self.main(FakeServer(ssh_exit=255), "y")

        self.assertIn("Could not reach myserver", str(caught.exception))

    def test_a_missing_backup_folder_is_created_only_when_asked(self):
        fake = FakeServer(folders=["/srv/bookorbit"])

        self.main(fake, "")  # the default is no

        self.assertFalse(self.backup.exists())
        self.assertEqual(fake.rsync_calls, [])

        self.main(fake, "y", "y")

        self.assertTrue(self.backup.is_dir())
        self.assertEqual(len(fake.rsync_calls), 1)

    def test_declining_the_backup_after_agreeing_to_the_folder_creates_nothing(self):
        self.main(FakeServer(folders=["/srv/bookorbit"]), "y", "n")

        self.assertFalse(self.backup.exists())

    def test_a_missing_setting_stops_with_the_line_to_add(self):
        self.dotenv.write_text("SERVER=myserver\n", encoding="utf-8")
        fake = FakeServer()

        with self.assertRaises(server.ServerError) as caught:
            self.main(fake)

        self.assertIn("BACKUP_DIR=", str(caught.exception))
        self.assertEqual(fake.calls, [])

    def test_the_summary_goes_to_the_log_once_and_the_progress_does_not(self):
        self.backup.mkdir()
        progress = "\r      1,024  50%  1.00MB/s  0:00:01\r      2,048 100%  1.00MB/s  0:00:02 (xfr#1)\n"
        fake = FakeServer(folders=["/srv/bookorbit"], rsync_output=progress + RSYNC_STATS)

        self.main(fake, "y")

        log = self.log_text()
        self.assertIn("Number of files: 3", log)
        self.assertNotIn("50%", log)
        self.assertIn("50%", self.output, "the terminal shows the progress")
        self.assertEqual(self.output.count("Number of files: 3"), 1)

    def test_a_failed_pull_is_an_error_naming_rsyncs_complaint(self):
        self.backup.mkdir()
        fake = FakeServer(
            folders=["/srv/bookorbit"],
            rsync_exit=23,
            rsync_output="rsync: [sender] send_files failed to open \"x.epub\": Permission denied (13)\n",
        )

        with self.assertRaises(server.ServerError) as caught:
            self.main(fake, "y")

        self.assertIn("exit code 23", str(caught.exception))
        self.assertIn("Permission denied", str(caught.exception))

    def test_files_that_changed_mid_copy_are_a_warning_not_a_failure(self):
        self.backup.mkdir()

        self.assertEqual(self.main(FakeServer(folders=["/srv/bookorbit"], rsync_exit=24), "y"), 0)


class DefaultDumperTest(unittest.TestCase):
    def test_only_stdout_reaches_the_file_so_a_warning_cannot_corrupt_the_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            into = Path(directory) / "dump.sql"

            result = server_backup.default_dumper(["sh", "-c", "echo data; echo warning >&2; exit 3"], into)

            self.assertEqual(into.read_text(encoding="utf-8"), "data\n")
            self.assertEqual(result, server_backup.DumpResult(3, "warning\n"))


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")
class RealRsyncTest(unittest.TestCase):
    """The real rsync, through a stand-in ssh, into folders with spaces in their names."""

    def test_the_folder_lands_whole_in_the_backup_and_nothing_there_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "server side" / "book orbit"
            (source / "fan fic").mkdir(parents=True)
            (source / "fan fic" / "111.epub").write_bytes(b"epub bytes")
            (source / "data" / "postgres" / "pgdata").mkdir(parents=True)
            (source / "data" / "postgres" / "pgdata" / "PG_VERSION").write_text("18\n", encoding="utf-8")
            (source / "data" / "app").mkdir(parents=True)
            (source / "data" / "app" / "settings.json").write_text("{}", encoding="utf-8")
            os.utime(source / "fan fic" / "111.epub", (1_600_000_000, 1_600_000_000))
            backup = root / "backup copy"
            backup.mkdir()
            (backup / "only in the backup.epub").write_bytes(b"kept")
            dotenv = root / ".env"
            dotenv.write_text(
                f"SERVER=myserver\nSERVER_BOOKORBIT_DIR={source}\nSERVER_LIBRARY_DIR={source / 'fan fic'}\n"
                f"BACKUP_DIR={backup}\n",
                encoding="utf-8",
            )
            fake_ssh = local_ssh(root)

            with patch.dict(os.environ, {"RSYNC_RSH": str(fake_ssh)}), redirect_stdout(io.StringIO()):
                exit_code = server_backup.main(
                    [],
                    runner=local_runner(fake_ssh),
                    dumper=FakeDumper(),
                    environ={},
                    dotenv_path=dotenv,
                    input_fn=Answers("y"),
                    terminal_fn=lambda: True,
                    log_dir=root / "logs",
                )
            close_run_logs()

            copied = backup / "book orbit" / "fan fic" / "111.epub"
            self.assertEqual(exit_code, 0)
            self.assertTrue((backup / "book orbit" / "data" / "app" / "settings.json").is_file())
            self.assertFalse((backup / "book orbit" / "data" / "postgres").exists(),
                             "the database's own files are left to the dump")
            self.assertEqual(sorted(path.name for path in backup.iterdir()),
                             ["book orbit", "book orbit-database.sql", "only in the backup.epub"],
                             "the server's folders stay inside a folder named like the server's")
            self.assertEqual(copied.read_bytes(), b"epub bytes")
            self.assertEqual(copied.stat().st_mtime, 1_600_000_000, "-t keeps the time")
            self.assertEqual((backup / "only in the backup.epub").read_bytes(), b"kept")
            log = next((root / "logs").glob("*.log")).read_text(encoding="utf-8")
            self.assertIn("Number of files:", log)


if __name__ == "__main__":
    unittest.main()

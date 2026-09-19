"""Tests for backup_server.py: one rsync pull, asked for at a terminal and never anywhere else."""

from contextlib import redirect_stdout
from datetime import datetime
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from ao3archiver import server, server_backup
from ao3archiver.common import ArchiverError
from tests.support import Answers, close_run_logs, FakeServer, local_runner, local_ssh, RSYNC_STATS


class BackupTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.backup = self.root / "backup"
        self.dotenv = self.root / ".env"
        self.dotenv.write_text(
            f"SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/bookorbit\nBACKUP_DIR={self.backup}\n",
            encoding="utf-8",
        )
        self.output = ""

    def tearDown(self):
        close_run_logs()
        self._directory.cleanup()

    STARTED = datetime(2026, 9, 19, 14, 12, 5)

    def main(self, runner, *answers, terminal=True, disk_free=lambda path: 10**12):
        self.answers = Answers(*answers)
        with redirect_stdout(io.StringIO()) as stream:
            try:
                return server_backup.main(
                    [],
                    runner=runner,
                    environ={},
                    dotenv_path=self.dotenv,
                    input_fn=self.answers,
                    terminal_fn=lambda: terminal,
                    log_dir=self.root / "logs",
                    now_fn=lambda: self.STARTED,
                    disk_free=disk_free,
                )
            finally:
                self.output = stream.getvalue()

    def log_text(self):
        return "\n".join(path.read_text(encoding="utf-8") for path in (self.root / "logs").glob("*.log"))

    def test_off_a_terminal_it_refuses_before_touching_the_server(self):
        fake = FakeServer(folders=["/srv/bookorbit"])

        with self.assertRaises(ArchiverError):
            self.main(fake, terminal=False)

        self.assertEqual(fake.calls, [])

    def test_answering_yes_runs_exactly_one_pull_of_the_whole_folder(self):
        self.backup.mkdir()
        fake = FakeServer(folders=["/srv/bookorbit"])

        self.assertEqual(self.main(fake, "y"), 0)

        self.assertEqual(fake.rsync_calls, [[
            "rsync", "-rt", "--protect-args", "--stats", "--info=progress2",
            "myserver:/srv/bookorbit", f"{self.backup}/",
        ]], "no slash after the source: the folder itself is copied, not just its contents")

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
        self.assertIn(f"rsync -rt --protect-args --stats --info=progress2 myserver:/srv/bookorbit {self.backup}/",
                      self.output)
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
        self.dotenv.write_text(f"SERVER=myserver\nSERVER_BOOKORBIT_DIR=/\nBACKUP_DIR={self.backup}\n", encoding="utf-8")
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
        self.assertTrue((self.backup / "bookorbit-20260919T141205.zip").is_file(), "and the backup is still zipped")

    def zips(self):
        return sorted(path.name for path in self.backup.glob("*.zip"))

    def test_every_backup_is_zipped_beside_the_folder_with_the_folder_inside(self):
        self.backup.mkdir()

        self.main(FakeServer(folders=["/srv/bookorbit"]), "y")

        self.assertEqual(self.zips(), ["bookorbit-20260919T141205.zip"])
        with zipfile.ZipFile(self.backup / "bookorbit-20260919T141205.zip") as archive:
            names = archive.namelist()
            self.assertEqual(archive.read("bookorbit/fanfiction/111.epub"), b"epub bytes")
            self.assertEqual({info.compress_type for info in archive.infolist()}, {zipfile.ZIP_STORED})
        self.assertIn("bookorbit/comics/", names, "an empty folder is kept too")
        self.assertTrue(all(name.startswith("bookorbit/") for name in names), "unzipping recreates the folder")
        self.assertEqual(list(self.backup.glob(".*.tmp")), [])
        self.assertIn("then zipped to", self.output)

    def test_every_zip_is_kept_and_none_is_ever_replaced(self):
        self.backup.mkdir()
        fake = FakeServer(folders=["/srv/bookorbit"])

        self.main(fake, "y")
        first = (self.backup / "bookorbit-20260919T141205.zip").read_bytes()
        self.main(fake, "y")

        self.assertEqual(self.zips(), ["bookorbit-20260919T141205-2.zip", "bookorbit-20260919T141205.zip"])
        self.assertEqual((self.backup / "bookorbit-20260919T141205.zip").read_bytes(), first)

    def test_a_failed_pull_is_not_zipped(self):
        self.backup.mkdir()

        with self.assertRaises(server.ServerError):
            self.main(FakeServer(folders=["/srv/bookorbit"], rsync_exit=23, rsync_output="error\n"), "y")

        self.assertEqual(self.zips(), [])

    def test_too_little_space_stops_the_zip_but_keeps_the_backup_folder(self):
        self.backup.mkdir()

        with self.assertRaises(server.ServerError) as caught:
            self.main(FakeServer(folders=["/srv/bookorbit"]), "y", disk_free=lambda path: 1000)

        self.assertIn("Not enough free space", str(caught.exception))
        self.assertEqual(self.zips(), [])
        self.assertEqual((self.backup / "bookorbit" / "fanfiction" / "111.epub").read_bytes(), b"epub bytes")

    def test_an_interrupted_zip_leaves_nothing_half_written(self):
        self.backup.mkdir()
        original = zipfile.ZipFile.write

        def interrupted(archive, filename, *args, **options):
            if str(filename).endswith(".epub"):
                raise KeyboardInterrupt
            return original(archive, filename, *args, **options)

        with patch.object(zipfile.ZipFile, "write", interrupted), self.assertRaises(KeyboardInterrupt):
            self.main(FakeServer(folders=["/srv/bookorbit"]), "y")

        self.assertEqual(self.zips(), [])
        self.assertEqual([path.name for path in self.backup.iterdir() if path.name.startswith(".")], [])


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")
class RealRsyncTest(unittest.TestCase):
    """The real rsync, through a stand-in ssh, into folders with spaces in their names."""

    def test_the_folder_lands_whole_in_the_backup_and_nothing_there_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "server side" / "book orbit"
            (source / "fan fic").mkdir(parents=True)
            (source / "fan fic" / "111.epub").write_bytes(b"epub bytes")
            os.utime(source / "fan fic" / "111.epub", (1_600_000_000, 1_600_000_000))
            backup = root / "backup copy"
            backup.mkdir()
            (backup / "only in the backup.epub").write_bytes(b"kept")
            dotenv = root / ".env"
            dotenv.write_text(
                f"SERVER=myserver\nSERVER_BOOKORBIT_DIR={source}\nBACKUP_DIR={backup}\n", encoding="utf-8"
            )
            fake_ssh = local_ssh(root)

            with patch.dict(os.environ, {"RSYNC_RSH": str(fake_ssh)}), redirect_stdout(io.StringIO()):
                exit_code = server_backup.main(
                    [],
                    runner=local_runner(fake_ssh),
                    environ={},
                    dotenv_path=dotenv,
                    input_fn=Answers("y"),
                    terminal_fn=lambda: True,
                    log_dir=root / "logs",
                )
            close_run_logs()

            copied = backup / "book orbit" / "fan fic" / "111.epub"
            self.assertEqual(exit_code, 0)
            self.assertEqual(sorted(path.name for path in backup.iterdir() if path.suffix != ".zip"),
                             ["book orbit", "only in the backup.epub"],
                             "the server's folders stay inside a folder named like the server's")
            [zipped] = backup.glob("book orbit-*.zip")
            with zipfile.ZipFile(zipped) as archive:
                self.assertEqual(archive.read("book orbit/fan fic/111.epub"), b"epub bytes")
            self.assertEqual(copied.read_bytes(), b"epub bytes")
            self.assertEqual(copied.stat().st_mtime, 1_600_000_000, "-t keeps the time")
            self.assertEqual((backup / "only in the backup.epub").read_bytes(), b"kept")
            log = next((root / "logs").glob("*.log")).read_text(encoding="utf-8")
            self.assertIn("Number of files:", log)


if __name__ == "__main__":
    unittest.main()

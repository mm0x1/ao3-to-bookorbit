"""Tests for the server settings and the ssh and rsync commands built from them."""

from pathlib import Path, PurePosixPath
import shlex
import tempfile
import unittest

from ao3archiver import server
from tests.support import FakeServer


REQUIRED = (server.SERVER, server.SERVER_BOOKORBIT_DIR, server.BACKUP_DIR)


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.dotenv = Path(self._directory.name) / ".env"

    def tearDown(self):
        self._directory.cleanup()

    def load(self, text="", environ=None, required=REQUIRED):
        self.dotenv.write_text(text, encoding="utf-8")
        return server.load_server_settings(required, environ or {}, dotenv_path=self.dotenv)

    def test_the_environment_wins_over_the_dotenv_file(self):
        settings = self.load(
            "SERVER=fromfile\nSERVER_BOOKORBIT_DIR=/srv/books\nBACKUP_DIR=/backups\n",
            environ={"SERVER": "fromenv"},
        )

        self.assertEqual(settings.server, "fromenv")
        self.assertIn("process environment (SERVER)", settings.source)

    def test_a_missing_key_names_the_exact_line_to_add(self):
        with self.assertRaises(server.ServerError) as caught:
            self.load("SERVER=myserver\nSERVER_BOOKORBIT_DIR=\n")

        message = str(caught.exception)
        self.assertIn("\n    SERVER_BOOKORBIT_DIR=\n", message + "\n")
        self.assertIn("\n    BACKUP_DIR=", message)
        self.assertNotIn("    SERVER=\n", message + "\n", "a key that is set is not asked for")
        self.assertIn("never writes", message)

    def test_nothing_is_written_to_the_dotenv_file(self):
        with self.assertRaises(server.ServerError):
            self.load("SERVER=myserver\n")

        self.assertEqual(self.dotenv.read_text(encoding="utf-8"), "SERVER=myserver\n")

    def test_a_backup_folder_under_home_is_expanded(self):
        settings = self.load("SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/books\nBACKUP_DIR=~/Backups/books\n")

        self.assertEqual(settings.backup_dir, Path.home() / "Backups" / "books")

    def test_a_relative_path_is_refused_rather_than_resolved_somewhere(self):
        for text in (
            "SERVER=myserver\nSERVER_BOOKORBIT_DIR=books\nBACKUP_DIR=/backups\n",
            "SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/books\nBACKUP_DIR=backups\n",
        ):
            with self.assertRaises(server.ServerError, msg=text):
                self.load(text)

    def test_an_alias_that_ssh_or_rsync_would_misread_is_refused(self):
        for alias in ("-oProxyCommand=evil", "my server", "host:path"):
            with self.assertRaises(server.ServerError, msg=alias):
                self.load(f"SERVER={alias}\nSERVER_BOOKORBIT_DIR=/srv/books\nBACKUP_DIR=/backups\n")

    def test_a_trailing_slash_on_a_server_path_is_dropped(self):
        settings = self.load("SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/books/\nBACKUP_DIR=/backups\n")

        self.assertEqual(settings.bookorbit_dir, PurePosixPath("/srv/books"))

    def test_the_printed_values_are_only_the_server_settings(self):
        settings = self.load(
            "AO3_PASSWORD=hunter22\nBOOKORBIT_PASSWORD=sekrit\n"
            "SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/books\nBACKUP_DIR=/backups\n"
        )

        printed = "\n".join(f"{label} {value}" for label, value in settings.rows())
        self.assertIn("SERVER myserver", printed)
        self.assertIn("SERVER_BOOKORBIT_DIR /srv/books", printed)
        self.assertNotIn("hunter22", printed)
        self.assertNotIn("sekrit", printed)

    def test_a_key_the_command_did_not_ask_for_is_not_read(self):
        settings = self.load("SERVER=myserver\nBACKUP_DIR=/backups\n", required=(server.SERVER,))

        self.assertEqual([label for label, _ in settings.rows()], ["SERVER", "settings from"])
        with self.assertRaises(KeyError):
            settings.backup_dir


class LibraryPathTest(unittest.TestCase):
    """BookOrbit reports paths inside its container; the server knows them by another root."""

    def settings(self, library="/srv/bookorbit/fanfiction"):
        with tempfile.TemporaryDirectory() as directory:
            dotenv = Path(directory) / ".env"
            dotenv.write_text(
                "SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/bookorbit\n"
                f"SERVER_LIBRARY_DIR={library}\nBOOKORBIT_LIBRARY_PATH_IN_CONTAINER=/books/fanfiction\n",
                encoding="utf-8",
            )
            return server.load_server_settings(
                (server.SERVER, server.SERVER_BOOKORBIT_DIR, server.SERVER_LIBRARY_DIR, server.LIBRARY_IN_CONTAINER),
                {},
                dotenv_path=dotenv,
            )

    def test_a_container_path_maps_to_the_servers_own_path(self):
        found = server.host_path("/books/fanfiction/Author/Title (7)/Title - Author.epub", self.settings())

        self.assertEqual(found, PurePosixPath("/srv/bookorbit/fanfiction/Author/Title (7)/Title - Author.epub"))

    def test_a_path_outside_the_mapped_folder_is_refused(self):
        for path in ("/books/other/1.epub", "/books/fanfiction-old/1.epub", "/books/fanfiction"):
            with self.assertRaises(server.ServerError, msg=path):
                server.host_path(path, self.settings())

    def test_the_library_may_sit_outside_bookorbits_folder_since_the_backup_copies_it_too(self):
        found = server.host_path("/books/fanfiction/1.epub", self.settings(library="/mnt/disk/books/fanfiction"))

        self.assertEqual(found, PurePosixPath("/mnt/disk/books/fanfiction/1.epub"))


class CommandTest(unittest.TestCase):
    def test_the_remote_shell_sees_the_path_as_one_argument(self):
        command = server.ssh_command("myserver", "test", "-d", "/srv/my books")

        self.assertEqual(command[:-1], ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "myserver"])
        self.assertEqual(shlex.split(command[-1]), ["test", "-d", "/srv/my books"])

    def test_rsync_copies_names_and_times_and_never_deletes(self):
        command = server.rsync_command("myserver:/srv/books/", "/backups/", "--stats")

        self.assertEqual(command[:3], ["rsync", "-rt", "--protect-args"])
        self.assertNotIn("-a", command)
        self.assertFalse(any(part.startswith("--delete") for part in command))

    def test_a_deleting_option_cannot_be_added(self):
        for option in ("--delete", "--delete-after", "-a", "--remove-source-files"):
            with self.assertRaises(ValueError, msg=option):
                server.rsync_command("a/", "b/", option)


class RemoteFolderTest(unittest.TestCase):
    def test_a_folder_that_exists_is_found(self):
        self.assertTrue(server.remote_dir_exists("myserver", PurePosixPath("/srv/my books"),
                                                 FakeServer(folders=["/srv/my books"])))

    def test_a_folder_that_does_not_is_reported_as_missing(self):
        self.assertFalse(server.remote_dir_exists("myserver", PurePosixPath("/srv/typo"), FakeServer()))

    def test_an_unreachable_server_is_an_error_not_a_missing_folder(self):
        with self.assertRaises(server.ServerError) as caught:
            server.remote_dir_exists("myserver", PurePosixPath("/srv/books"), FakeServer(ssh_exit=255))

        self.assertIn("ssh myserver", str(caught.exception))
        self.assertIn("No route to host", str(caught.exception))

    def test_a_missing_program_is_named(self):
        with self.assertRaises(server.ServerError) as caught:
            server.default_runner(["definitely-not-a-real-program-ao3"])

        self.assertIn("not installed", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

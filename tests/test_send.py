"""Tests for send_to_server.py: add new works, replace owned ones in place, then fill in BookOrbit."""

from contextlib import redirect_stdout
import io
import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import unittest
from unittest.mock import patch

from ao3archiver import bookorbit, send, server
from ao3archiver.common import ArchiverError
from ao3archiver.metadata import AO3Metadata, enrich_epub
from tests.support import Answers, close_run_logs, create_epub, FakeServer, front_page, local_runner, local_ssh
from tests.test_bookorbit import FakeResponse, FakeSession


# Every field the project fills, so no run stops to ask about a missing one.
ALL_FIELDS = [
    {"id": number, "label": label, "type": kind, "archivedAt": None}
    for number, (label, kind, _) in enumerate(bookorbit.FIELD_SOURCES, start=1)
]
FIELD_KEYS = {field["id"]: bookorbit.slugify_label(field["label"]) for field in ALL_FIELDS}
LINK_FIELD = next(field["id"] for field in ALL_FIELDS if field["label"] == "AO3 Link")
KUDOS_FIELD = next(field["id"] for field in ALL_FIELDS if field["label"] == "AO3 Kudos")
CHAPTERS_FIELD = next(field["id"] for field in ALL_FIELDS if field["label"] == "AO3 Chapters")

OLD_LAYOUT = "Author/Title (7)/Title - Author.epub"


class BookOrbit(FakeSession):
    """A fake BookOrbit that keeps what it is sent, so a second run sees the first one's writes."""

    def __init__(self, books):
        super().__init__(books=books, fields=ALL_FIELDS)

    def request(self, method, url, json=None, headers=None, timeout=None):
        response = super().request(method, url, json=json, headers=headers, timeout=timeout)
        if method == "PATCH":
            book_id = int(url.rsplit("/books/", 1)[1].split("/")[0])
            row = next(book for book in self.books if book["bookId"] == book_id)
            for entry in json["customMetadata"]:
                row[f"custom.{FIELD_KEYS[entry['fieldId']]}"] = entry["value"]
        return response


def write_work(path: Path, work_id: str, *, kudos: int = 68, chapters: str = "3/3") -> Path:
    create_epub(path, "<html/>", rating_page=front_page("Mature", work_id))
    enrich_epub(
        path,
        AO3Metadata(
            work_id=work_id,
            work_url=f"https://archiveofourown.org/works/{work_id}",
            kudos=kudos,
            chapters=chapters,
            rating="Mature",
        ),
    )
    return path


def settings_for(library: str = "/srv/bookorbit/fanfiction") -> server.ServerSettings:
    return server.ServerSettings(
        values={
            server.SERVER: "myserver",
            server.SERVER_BOOKORBIT_DIR: "/srv/bookorbit",
            server.SERVER_LIBRARY_DIR: library,
            server.LIBRARY_IN_CONTAINER: "/books/fanfiction",
        },
        source="test",
    )


def owned(*rows):
    fields = [bookorbit.CustomField(LINK_FIELD, "AO3 Link", "url", "ao3_link")]
    return bookorbit.owned_from_rows(rows, fields)


class PlaceTest(unittest.TestCase):
    def batch(self, *work_ids):
        return send.Batch(
            tuple(send.LocalEpub(Path(f"/downloaded/{work_id}.epub"), work_id) for work_id in work_ids), (), ()
        )

    def test_a_new_work_goes_flat_under_its_work_id(self):
        placements, skipped = send.place(self.batch("111"), owned(), settings_for())

        self.assertEqual([placement.target for placement in placements], [PurePosixPath("111.epub")])
        self.assertIsNone(placements[0].book)
        self.assertEqual(skipped, ())

    def test_an_owned_work_goes_to_its_books_own_path(self):
        library = owned({
            "bookId": 42, "filePaths": [f"/books/fanfiction/{OLD_LAYOUT}"],
            "custom.ao3_link": "https://archiveofourown.org/works/222",
        })

        placements, _ = send.place(self.batch("222"), library, settings_for())

        self.assertEqual(placements[0].target, PurePosixPath(OLD_LAYOUT))
        self.assertEqual(placements[0].book.book_id, 42)

    def test_a_book_outside_the_mapped_folder_is_not_sent(self):
        library = owned({"bookId": 9, "filePaths": ["/books/other/222.epub"]})

        placements, skipped = send.place(self.batch("222"), library, settings_for())

        self.assertEqual(placements, ())
        self.assertIn("outside BOOKORBIT_LIBRARY_PATH_IN_CONTAINER", skipped[0].reason)

    def test_a_work_bookorbit_holds_twice_is_skipped_and_named(self):
        library = owned(
            {"bookId": 1, "filePaths": ["/books/fanfiction/222.epub"]},
            {"bookId": 2, "filePaths": [f"/books/fanfiction/{OLD_LAYOUT}"],
             "custom.ao3_link": "https://archiveofourown.org/works/222"},
        )

        placements, skipped = send.place(self.batch("222"), library, settings_for())

        self.assertEqual(placements, ())
        self.assertIn("ids 1, 2", skipped[0].reason)

    def test_a_new_work_never_lands_on_another_works_file(self):
        library = owned({
            "bookId": 5, "filePaths": ["/books/fanfiction/111.epub"],
            "custom.ao3_link": "https://archiveofourown.org/works/999",
        })

        placements, skipped = send.place(self.batch("111"), library, settings_for())

        self.assertEqual(placements, ())
        self.assertIn("work 999", skipped[0].reason)


class ItemizeTest(unittest.TestCase):
    """rsync's own output, captured from a dry run of a push to a server and of a local copy."""

    PUSH = (
        "<f+++++++++ 111.epub\n"
        "<f.st...... Author/Title (7)/Title - Author.epub\n"
        "cd+++++++++ New Folder/\n"
    )

    def test_a_new_file_is_an_add_and_a_different_one_a_replace(self):
        self.assertEqual(send.parse_itemized(self.PUSH), {
            PurePosixPath("111.epub"): "add",
            PurePosixPath(OLD_LAYOUT): "replace",
        })

    def test_the_local_form_reads_the_same(self):
        self.assertEqual(
            send.parse_itemized(">f+++++++++ 111.epub\n>f.s....... 222.epub\n"),
            {PurePosixPath("111.epub"): "add", PurePosixPath("222.epub"): "replace"},
        )

    def test_an_identical_file_is_not_listed_at_all(self):
        self.assertEqual(send.parse_itemized(""), {})


class ReadBatchTest(unittest.TestCase):
    def test_each_epub_is_known_by_the_work_id_inside_it(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            write_work(folder / "renamed.epub", "111")
            write_work(folder / "111 (copy).epub", "111")
            (folder / "broken.epub").write_bytes(b"not an epub")
            (folder / ".222.epub.x1y2.tmp").write_bytes(b"half written")

            batch = send.read_batch(folder)

        self.assertEqual([(epub.path.name, epub.work_id) for epub in batch.epubs], [("111 (copy).epub", "111")])
        self.assertEqual([path.name for path in batch.duplicates], ["renamed.epub"])
        self.assertEqual([path.name for path in batch.unreadable], ["broken.epub"])


class RefusalTest(unittest.TestCase):
    """What stops a send before it touches the server or BookOrbit."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.dotenv = self.root / ".env"
        self.fake = FakeServer(folders=["/srv/bookorbit/fanfiction"])

    def tearDown(self):
        close_run_logs()
        self._directory.cleanup()

    def main(self, dotenv_text, *, terminal=True):
        self.dotenv.write_text(dotenv_text, encoding="utf-8")
        downloaded = self.root / "downloaded"
        downloaded.mkdir(exist_ok=True)
        write_work(downloaded / "111.epub", "111")

        def no_bookorbit(config):
            raise AssertionError("BookOrbit was contacted")

        with redirect_stdout(io.StringIO()):
            return send.main(
                [], runner=self.fake, environ={}, dotenv_path=self.dotenv, downloaded=downloaded,
                client_factory=no_bookorbit, input_fn=Answers(), terminal_fn=lambda: terminal,
                log_dir=self.root / "logs",
            )

    SETTINGS = (
        "SERVER=myserver\nSERVER_BOOKORBIT_DIR=/srv/bookorbit\nSERVER_LIBRARY_DIR=/srv/bookorbit/fanfiction\n"
        "BOOKORBIT_LIBRARY_PATH_IN_CONTAINER=/books/fanfiction\n"
    )
    BOOKORBIT = "BOOKORBIT_URL=http://books.example\nBOOKORBIT_USERNAME=archiver\nBOOKORBIT_PASSWORD=secret\n"

    def test_off_a_terminal_it_refuses_before_touching_anything(self):
        with self.assertRaises(ArchiverError):
            self.main(self.SETTINGS + self.BOOKORBIT, terminal=False)

        self.assertEqual(self.fake.calls, [])

    def test_a_missing_setting_stops_with_the_line_to_add(self):
        with self.assertRaises(server.ServerError) as caught:
            self.main(self.SETTINGS.replace("BOOKORBIT_LIBRARY_PATH_IN_CONTAINER=/books/fanfiction\n", "")
                      + self.BOOKORBIT)

        self.assertIn("\n    BOOKORBIT_LIBRARY_PATH_IN_CONTAINER=", str(caught.exception))
        self.assertEqual(self.fake.calls, [])

    def test_without_bookorbit_it_stops_and_says_why(self):
        with self.assertRaises(send.SendError) as caught:
            self.main(self.SETTINGS)

        self.assertIn("needs BookOrbit", str(caught.exception))
        self.assertIn("BOOKORBIT_URL=", str(caught.exception))
        self.assertEqual(self.fake.calls, [])

    def test_a_library_folder_missing_on_the_server_is_refused(self):
        self.fake = FakeServer(folders=[])

        with self.assertRaises(send.SendError) as caught:
            self.main(self.SETTINGS + self.BOOKORBIT)

        self.assertIn("SERVER_LIBRARY_DIR", str(caught.exception))
        self.assertEqual(self.fake.rsync_calls, [])


@unittest.skipUnless(shutil.which("rsync"), "rsync is not installed")
class SendTest(unittest.TestCase):
    """The whole command: real rsync into a temporary "server", and a fake BookOrbit."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.bookorbit_dir = self.root / "the server" / "book orbit"
        self.library = self.bookorbit_dir / "fan fiction"
        (self.library / "Author" / "Title (7)").mkdir(parents=True)
        self.old_file = self.library / OLD_LAYOUT
        self.old_file.write_bytes(b"the old copy")
        self.downloaded = self.root / "downloaded"
        self.downloaded.mkdir()
        self.new_epub = write_work(self.downloaded / "111.epub", "111", kudos=5)
        self.owned_epub = write_work(self.downloaded / "222.epub", "222", kudos=90, chapters="4/?")
        self.dotenv = self.root / ".env"
        self.dotenv.write_text(
            "SERVER=myserver\n"
            f"SERVER_BOOKORBIT_DIR={self.bookorbit_dir}\n"
            f"SERVER_LIBRARY_DIR={self.library}\n"
            "BOOKORBIT_LIBRARY_PATH_IN_CONTAINER=/books/fanfiction\n"
            "BOOKORBIT_URL=http://books.example\nBOOKORBIT_USERNAME=archiver\nBOOKORBIT_PASSWORD=secret\n",
            encoding="utf-8",
        )
        self.bookorbit = BookOrbit(books=[{
            "bookId": 42,
            "filePaths": [f"/books/fanfiction/{OLD_LAYOUT}"],
            "custom.ao3_link": "https://archiveofourown.org/works/222",
            "custom.ao3_chapters": "3/?",
        }])
        fake_ssh = local_ssh(self.root)
        self.real_runner = local_runner(fake_ssh)
        self.commands: list[list[str]] = []
        environment = patch.dict(os.environ, {"RSYNC_RSH": str(fake_ssh)})
        environment.start()
        self.addCleanup(environment.stop)
        self.sleeps = 0
        # BookOrbit's folder watch: a new file shows up after this many polls.
        self.appears_after = 2

    def tearDown(self):
        close_run_logs()
        self._directory.cleanup()

    def runner(self, args, *, echo=None):
        self.commands.append(list(args))
        return self.real_runner(args, echo=echo)

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps == self.appears_after and (self.library / "111.epub").exists():
            self.bookorbit.books.append({"bookId": 50, "filePaths": ["/books/fanfiction/111.epub"]})

    def main(self, *answers):
        self.answers = Answers(*answers)
        with redirect_stdout(io.StringIO()) as stream:
            try:
                return send.main(
                    [],
                    runner=self.runner,
                    environ={},
                    dotenv_path=self.dotenv,
                    downloaded=self.downloaded,
                    client_factory=lambda config: bookorbit.BookOrbitClient(config, session=self.bookorbit),
                    input_fn=self.answers,
                    terminal_fn=lambda: True,
                    sleep_fn=self.sleep,
                    log_dir=self.root / "logs",
                )
            finally:
                self.output = stream.getvalue()

    def transfers(self):
        return [command for command in self.commands if command[0] == "rsync" and "--dry-run" not in command]

    def written(self, book_id):
        return {entry["fieldId"]: entry["value"] for entry in self.bookorbit.patches.get(book_id, [])}

    def test_a_new_work_is_added_flat_and_an_owned_one_replaced_in_place(self):
        self.assertEqual(self.main("y", "y"), 0)

        self.assertEqual((self.library / "111.epub").read_bytes(), self.new_epub.read_bytes())
        self.assertEqual(self.old_file.read_bytes(), self.owned_epub.read_bytes())
        self.assertFalse((self.library / "222.epub").exists(), "the owned work is not added a second time")
        self.assertEqual(len(self.transfers()), 1, "one rsync sends everything")
        self.assertEqual(
            self.answers.questions,
            ["Add 1 new book(s) to the server? [Y/n] ", "Replace 1 book(s) on the server? [y/N] "],
        )

    def test_fields_go_to_the_right_books_including_one_bookorbit_found_later(self):
        self.main("y", "y")

        self.assertEqual(self.written(50)[KUDOS_FIELD], 5)
        self.assertEqual(self.written(50)[LINK_FIELD], "https://archiveofourown.org/works/111")
        self.assertEqual(self.written(42)[KUDOS_FIELD], 90)
        self.assertEqual(self.written(42)[CHAPTERS_FIELD], "4/?")
        self.assertEqual(self.sleeps, 2, "it stopped waiting once the new book appeared")

    def test_declining_the_replacement_leaves_the_old_file_and_its_fields_alone(self):
        self.main("y", "n")

        self.assertEqual(self.old_file.read_bytes(), b"the old copy")
        self.assertEqual(self.written(42), {}, "the old file keeps its old chapter count")
        self.assertTrue((self.library / "111.epub").exists())

    def test_replacing_is_asked_with_a_warning_and_defaults_to_no(self):
        self.main("y", "")

        self.assertEqual(self.old_file.read_bytes(), b"the old copy")
        self.assertIn("backup_server.py", self.output)

    def test_a_rerun_asks_nothing_and_sends_nothing(self):
        self.main("y", "y")
        patched = {book_id: list(entries) for book_id, entries in self.bookorbit.patches.items()}
        self.commands.clear()

        self.assertEqual(self.main(), 0)

        self.assertEqual(self.answers.questions, [])
        self.assertEqual(self.transfers(), [])
        self.assertEqual(self.bookorbit.patches, patched, "no field is written twice")

    def test_a_book_bookorbit_never_shows_is_named_for_the_next_run(self):
        self.appears_after = 0  # never

        self.assertEqual(self.main("y", "y"), 0)

        self.assertEqual(self.sleeps, send.POLL_ATTEMPTS)
        self.assertIn("111.epub", self.output)
        self.assertIn("Rerun send_to_server.py in a minute", self.output)
        self.assertEqual(self.written(50), {})
        self.assertEqual(self.written(42)[KUDOS_FIELD], 90, "the books it can see are still filled in")

    def test_an_unreadable_epub_is_reported_and_not_sent(self):
        (self.downloaded / "broken.epub").write_bytes(b"not an epub")

        self.main("y", "y")

        self.assertFalse((self.library / "broken.epub").exists())
        self.assertIn("not sent: broken.epub", self.output)

    def test_an_empty_download_folder_sends_nothing_and_asks_nothing(self):
        for path in self.downloaded.iterdir():
            path.unlink()

        self.assertEqual(self.main(), 0)

        self.assertEqual(self.commands, [])
        self.assertEqual(self.answers.questions, [])

    def test_nothing_is_ever_deleted_on_either_side(self):
        (self.library / "someone else's.epub").write_bytes(b"not ours")

        self.main("y", "y")

        self.assertEqual((self.library / "someone else's.epub").read_bytes(), b"not ours")
        self.assertTrue(self.new_epub.exists())
        self.assertTrue(self.owned_epub.exists())
        for command in self.commands:
            self.assertFalse(any(part.startswith("--delete") for part in command), command)

    def test_a_bookorbit_that_cannot_be_read_stops_before_anything_is_sent(self):
        class Refusing(BookOrbit):
            def request(self, method, url, json=None, headers=None, timeout=None):
                if "metadata-export" in url:
                    return FakeResponse(status_code=500, text="down")
                return super().request(method, url, json=json, headers=headers, timeout=timeout)

        self.bookorbit = Refusing(books=self.bookorbit.books)

        with self.assertRaises(bookorbit.BookOrbitError):
            self.main("y", "y")

        self.assertEqual(self.transfers(), [])
        self.assertEqual(self.old_file.read_bytes(), b"the old copy")


if __name__ == "__main__":
    unittest.main()

# Tutorial: from an AO3 search to your library server

The whole pipeline, end to end, once. Every step links to the reference guide
that holds its details. Nothing here needs the step before it to have gone
perfectly: each command can be rerun, and each one shows what it is about to do
and asks before it does it.

1. [Before you start](#before-you-start)
2. [Harvest links](#1-harvest-links)
3. [Download](#2-download)
4. [Send to the server](#3-send-to-the-server)
5. [Back up the server](#4-back-up-the-server)
6. [Without a server: Calibre](#without-a-server-calibre)
7. [What goes into each EPUB](#what-goes-into-each-epub)

## Before you start

```sh
git clone https://github.com/mm0x1/ao3-to-bookorbit.git && cd ao3-to-bookorbit
python3 -m venv venv && source venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env
```

Fill in `.env`. It is ignored by Git, and no command here ever writes it — you
do, once — and no tool prints, logs or stores its passwords.
[docs/CONFIGURATION.md](CONFIGURATION.md) lists every key; in short:

- `AO3_USERNAME`, `AO3_PASSWORD` — an ordinary AO3 account, for downloading.
- `BOOKORBIT_URL`, `BOOKORBIT_USERNAME`, `BOOKORBIT_PASSWORD`, and
  `BOOKORBIT_LIBRARY_ID` — so the download knows what you already own, and the
  send can fill in BookOrbit's fields.
- `SERVER`, `SERVER_BOOKORBIT_DIR`, `SERVER_LIBRARY_DIR`,
  `BOOKORBIT_LIBRARY_PATH_IN_CONTAINER` and `BACKUP_DIR` — where the library is
  on the server, how BookOrbit sees it, and where the backup goes.

`SERVER` is an alias from `~/.ssh/config`, so no host, user or key ever appears
in a command ([the SSH alias](CONFIGURATION.md#the-ssh-alias)). Check that
`ssh myserver` works on its own before going on.

In BookOrbit, once: create the custom fields, and turn on the library's folder
watch ([docs/BOOKORBIT.md](BOOKORBIT.md#one-time-setup-the-custom-fields)).

Then run the front door. It checks all of the above without touching the
network, and offers each step below:

```sh
python3 start_here.py
```

## 1. Harvest links

1. Copy `searches.example.toml` to `searches.toml`.
2. For each fandom, open AO3, filter and sort its works the way you like (by
   hits, say), and add a `[[search]]` with that page's address and how many
   works you want that you do not have yet.
3. Run it:

```sh
python3 harvest_links.py
```

It shows the plan, asks once, and then turns each listing's pages until it has
found that many works that are not on the server, not in `downloaded/` and not
already in `links/`. A work in two fandoms is counted once. Unfinished works on
the server whose listing shows more chapters come along as updates. Each
search's links go to `links/<name>.txt`. [docs/HARVESTING.md](HARVESTING.md)
has the details.

Link files from anywhere else, such as
[ao3downloader](https://github.com/nianeyna/ao3downloader), still work: drop
them into `links/`. Duplicate links across files are counted and collapsed.

## 2. Download

```sh
python3 download_fics.py
```

First it asks BookOrbit which of the linked works are already on the server, by
AO3 work ID, and shows the plan: how many works are new, how many you already
have unfinished, how many finished, and an estimated finish time. Then it asks
at most three questions, all before the first request to AO3:

- *Check AO3 for new chapters of the unfinished ones?* Yes by default: one
  request each, and the EPUB is downloaded only if AO3 has more chapters.
- *Download the finished ones again anyway?* No by default.
- *Start this download now?*

For each work it fetches, the script reads the AO3 work page, skips and records
anything AO3 reports as deleted or unrevealed, downloads the EPUB from the
page's own Download link, calculates local Words and Gunning Fog from the file,
then writes everything into the EPUB and reads it back.

A run of tens of thousands of works takes days. Start it in the background and
follow the log; off a terminal every question takes its default:

```sh
nohup python3 download_fics.py &
tail -f "$(ls -t ~/.local/share/ao3-calibre-backfill/logs/download-*.log | head -1)"
```

Ctrl-C (or `kill`) stops at the next safe point, prints a summary, and leaves
the run resumable: run the same command again. `--dry-run` shows the plan and
stops. [docs/DOWNLOADING.md](DOWNLOADING.md) covers every option and what
happens when AO3 has a bad day.

## 3. Send to the server

```sh
python3 send_to_server.py
```

It knows each EPUB in `downloaded/` by the work ID inside it. A work BookOrbit
does not hold is added to `SERVER_LIBRARY_DIR` as `<work id>.epub`; a work it
already holds replaces that book's file where it is, so BookOrbit keeps it as the
same book; a file already on the server is left alone. An rsync dry run decides
which is which, and the plan lists every replacement before asking:

```text
Add 9 new book(s) to the server? [Y/n]
Replace 2 book(s) on the server? [y/N]
```

Replacing defaults to no. BookOrbit re-reads a replaced file at once and
replaces the book's unlocked metadata from it, so back up first (the next step)
and export BookOrbit's metadata as a snapshot.

Then it waits for BookOrbit's folder watch to pick up the new books — about half
a minute — and fills in their custom fields: kudos, hits, word counts and the
rest, which BookOrbit never reads out of a file. Running it again sends nothing
twice. It deletes nothing: empty `downloaded/` yourself once you are happy.
[docs/SERVER.md](SERVER.md#sending) has the details.

## 4. Back up the server

```sh
python3 backup_server.py
```

One rsync pull of `SERVER_BOOKORBIT_DIR` into a folder of the same name in
`BACKUP_DIR`: `/path/to/bookorbit` becomes `BACKUP_DIR/bookorbit`.
The first run copies the whole library and can take hours; later runs copy only
what changed. It deletes nothing on either side, and makes no archive: taking
the backup on to a drive or a cloud folder is up to you.
[docs/SERVER.md](SERVER.md#backing-up) has the details.

## Without a server: Calibre

The EPUBs are Calibre-ready on their own: every file carries Calibre column
values, so importing it fills Calibre's custom columns. Without BookOrbit the
download cannot tell what you already own, says so, and downloads every work.

**Create the custom columns before you import anything.** Calibre silently
drops values for columns a library does not have. Create them once in
*Preferences → Add your own columns*:

| Lookup name | Heading | Type |
| --- | --- | --- |
| `ao3_kudos` | AO3 Kudos | Integers |
| `ao3_hits` | AO3 Hits | Integers |
| `ao3_bookmarks` | AO3 Bookmarks | Integers |
| `ao3_comments` | AO3 Comments | Integers |
| `ao3_words` | AO3 Words | Integers |
| `ao3_chapters` | AO3 Chapters | Text |
| `ao3_status` | AO3 Status | Text |
| `ao3_category` | AO3 Category | Text |
| `words` | Words | Integers |
| `gfog` | Gfog | Floating point numbers |

Then drag the EPUBs from `downloaded/` into Calibre, or add them with
`calibredb`:

```sh
calibredb add --library-path "/path/to/Calibre Library" downloaded/
```

Sort by **AO3 Kudos**, or search numerically: `#ao3_kudos:>1000`,
`#words:<20000`, `#gfog:<8`. `#ao3_chapters:"~[?]"` finds works whose final
chapter count is still unknown (`3/?`), which covers most works in progress; a
work with a declared total, such as `3/10`, does not match. (A plain `"?"`
matches every book.)

**Calibre-Web** reads the same columns from Calibre's database. Restart it or
rescan after importing, and check that its setting for ignored custom columns
does not match `ao3_`.

**A Calibre library you already have** is a different job, done once, by the
retired backfill: [docs/BACKFILL.md](BACKFILL.md). It fills the same columns for
books already in Calibre, bakes each book's metadata into its file, and repairs
files some readers cannot open.

## What goes into each EPUB

AO3's EPUB export does not include the work's statistics, so they are read from
the work page and written into the file in three forms:

- **Calibre column values** (`calibre:user_metadata`), which fill Calibre's
  custom columns on import;
- **portable `ao3:*` metadata** and an `ao3` identifier, which travel with the
  file anywhere — they are what `send_to_server.py` sends to BookOrbit's fields;
- **a visible "AO3 statistics" block** in the description.

| Column | From | Notes |
| --- | --- | --- |
| `#ao3_kudos`, `#ao3_hits`, `#ao3_bookmarks`, `#ao3_comments` | AO3 work page | AO3 omits a Kudos, Comments, or Bookmarks row when the count is zero, so a missing row is recorded as 0. |
| `#ao3_words` | AO3 work page | AO3's own count. |
| `#ao3_chapters` | AO3 work page | For example `12/12`, or `3/?` while in progress. |
| `#ao3_status` | AO3 work page | The date AO3 shows as *Completed* or *Updated*. One-chapter works have none. |
| `#ao3_category` | AO3 work page | For example `F/F` or `Gen, M/M`. |
| `#words` | the EPUB | Every spine body, so it includes AO3's preface and notes. |
| `#gfog` | the EPUB | Gunning Fog index, two decimals (`count-pages-compatible-pure-python-v1`). |

Nothing is written for a value AO3 does not show, so a missing statistic never
blanks a column.

The work's **rating** is a tag rather than a column: it is written as a
`dc:subject`, beside the tags AO3's own export already puts there, so Calibre
shows it as a Tag and BookOrbit as a Genre with nothing to configure. AO3's
exports usually carry it already; when one does not, it is taken from the work
page that was fetched anyway, or from the EPUB's own front page. A work whose
rating cannot be found anywhere is still saved, with a warning and a count in
the run summary.

EPUB metadata is written with unprefixed element names under a default
namespace, the form AO3 and Calibre both produce. `<opf:package>` means the same
thing, but readers that look elements up by their literal name disagree:
Calibre's importer skips a prefixed custom column, and some readers show an
empty book. That is what [`repair-epubs`](BACKFILL.md#repair-epubs) fixes in
files from elsewhere.

---

Built with [Claude Code](https://claude.com/claude-code). The code, tests and
documentation in this repository were written by Claude under human direction
and review.

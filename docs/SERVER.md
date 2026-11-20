# The server

The library lives on a server, and BookOrbit serves it from there. This page
covers the two commands that talk to that server over ssh and rsync:

```sh
python3 send_to_server.py    # send downloaded/ to the server, then fill in BookOrbit
python3 backup_server.py     # dump BookOrbit's database, copy its folder and the library to this PC
```

Neither takes options. Each prints the settings it is using and what it is about
to do, then asks before it does anything. Because nobody could answer those
questions off a terminal, both refuse to run under `nohup`, cron or a pipe.

1. [Settings](#settings)
2. [Sending](#sending)
3. [Backing up](#backing-up)
4. [What they never do](#what-they-never-do)

## Settings

Everything comes from `.env`, which only you edit; see
[docs/CONFIGURATION.md](CONFIGURATION.md#the-server). A run that is missing a
setting stops before it touches anything and prints the exact lines to add:

```text
ERROR  backup_server.py needs settings that are not in /path/to/ao3-to-bookorbit/.env. It never writes that file; add these lines to it yourself:

    # The folder on this PC that holds the backup, which goes into a folder in it named like SERVER_BOOKORBIT_DIR's own, for example: ~/Backups
    BACKUP_DIR=
```

Nothing is guessed and nothing has a default. rsync pointed at a mistyped path
does not fail: it creates the folder and fills it. So every value is written
down once, in `.env`, and every run prints the values it is using.

## Sending

```sh
python3 send_to_server.py
```

It reads every EPUB in `downloaded/`, knows each one by the AO3 work ID baked
into it, and asks BookOrbit which of those works it already holds. That makes
three kinds of file:

| The work… | What happens |
| --- | --- |
| is not in BookOrbit | Added as `SERVER_LIBRARY_DIR/<work id>.epub`. |
| is in BookOrbit | Replaces that book's file **at the book's own path**, whatever its layout (`Author/Title (7)/Title - Author.epub` included), so BookOrbit keeps it as the same book. |
| is on the server already, same size and time | Left alone. |

rsync decides the last two with a dry run before anything is sent, so what the
plan says is exactly what will happen:

```text
========================================================================
The plan
  EPUBs read                 12
  new, to add                9
  on the server, to replace  2
  on the server, identical   1
  not sent                   0
========================================================================
  add      111.epub -> /path/to/bookorbit/fanfiction/111.epub
  ...
  replace  222.epub -> /path/to/bookorbit/fanfiction/Author/Title (7)/Title - Author.epub (BookOrbit book 42)
Add 9 new book(s) to the server? [Y/n]
Replace 2 book(s) on the server? [y/N]
```

Adding and replacing are asked separately, and replacing defaults to **no**.
With BookOrbit's folder watch on, a replaced file is re-read at once and the
book's unlocked metadata — title, authors, tags, description — is replaced from
the file. So before saying yes, run `backup_server.py` and export BookOrbit's
metadata as a snapshot. Every replacement is listed, never summarised.

Then it waits for BookOrbit. The folder watch picks a new book up in about half
a minute; the command checks every 10 seconds for up to three minutes, then
fills in the custom fields ([docs/BOOKORBIT.md](BOOKORBIT.md)) of every book
whose file on the server now matches the one in `downloaded/`, sending only the
values that changed. A replacement you declined keeps its old values: its file
is the old one, and so is its chapter count.

```text
Sent
  added                 9
  replaced              2
  identical, not sent   1
  declined              0
  not sent              0
  not in BookOrbit yet  0
  fields filled in      11 book(s); 1 already up to date
```

- **Running it again is always safe.** Files already on the server are not sent
  again, and values already in BookOrbit are not written again, so a second run
  asks nothing and sends nothing. A book BookOrbit had not picked up in time is
  named, and the next run fills in its fields.
- **What is not sent, and why, is listed:** an EPUB with no AO3 metadata in it,
  a second file for the same work, a work BookOrbit holds as two books (fix that
  in BookOrbit first), or a book kept outside `BOOKORBIT_LIBRARY_PATH_IN_CONTAINER`.
- **It needs BookOrbit.** BookOrbit is how it knows which books are already on
  the server and where, so without `BOOKORBIT_URL` it stops and says so.
- **A book BookOrbit cannot tell the work of is invisible to it.** A book with
  neither an AO3 Link value nor a `<work id>.epub` file name — one never
  enriched, say — is not recognised, so a new download of that work would be
  added as a second book. Each run says how many such books BookOrbit holds.
- **`downloaded/` is never emptied.** Empty it yourself once you are happy.

## Backing up

```sh
python3 backup_server.py
```

```text
========================================================================
Back up the server
  SERVER                myserver
  SERVER_BOOKORBIT_DIR  /path/to/bookorbit
  SERVER_LIBRARY_DIR    /path/to/books/fanfiction
  BACKUP_DIR            /home/me/Backups
  settings from         /path/to/ao3-to-bookorbit/.env
  database dump goes to /home/me/Backups/bookorbit-database.sql
  backup goes into      /home/me/Backups/bookorbit
  backup goes into      /home/me/Backups/fanfiction
  dump command          ssh -o BatchMode=yes -o ConnectTimeout=10 myserver 'docker compose --project-directory /path/to/bookorbit exec -T postgres sh -c '"'"'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"'"'"''
  command               rsync -rt --protect-args --stats --info=progress2 --exclude=/bookorbit/data/postgres myserver:/path/to/bookorbit /home/me/Backups/
  command               rsync -rt --protect-args --stats --info=progress2 myserver:/path/to/books/fanfiction /home/me/Backups/
========================================================================
checking that /path/to/bookorbit exists on myserver
checking that /path/to/books/fanfiction exists on myserver
Start the backup now? [Y/n]
```

That is the whole backup: a dump of BookOrbit's database, an `rsync` pull of
`SERVER_BOOKORBIT_DIR`, folder and all, into `BACKUP_DIR`, and a second of `SERVER_LIBRARY_DIR` when the books live
somewhere else, such as another drive. When the library is inside
`SERVER_BOOKORBIT_DIR` it is already in the first copy, so there is only one
pull. Each copy is a folder named like the server's — `/path/to/bookorbit`
becomes `BACKUP_DIR/bookorbit` — holding every folder the server's does, so
nothing lands loose in `BACKUP_DIR`. Two folders with the same last name would
mix in one copy, so that is refused. There is no archive and no copying onward —
taking the backup to an external drive or a cloud folder is up to you.

**The database is dumped, not copied.** Postgres's own files, in
`SERVER_BOOKORBIT_DIR/data/postgres`, are left out of the rsync: they belong to
the database's user, so the SSH user usually cannot read them, and a copy taken
while the database runs may not be one Postgres can start from. Instead
`pg_dump` runs inside BookOrbit's `postgres` service, and its output comes over
SSH straight into `BACKUP_DIR/bookorbit-database.sql`; nothing is written on
the server. The dump runs first, since it takes seconds. If it fails, or ends
without Postgres's "dump complete" line, nothing is copied and the previous dump
is kept. To restore, load the file into an empty database with
`psql -U <user> -d <database> -f bookorbit-database.sql` inside the container.

- **The first run copies everything**, which for a large library is many
  gigabytes and can take hours. Later runs copy only what changed. Nothing puts
  a time limit on the copy.
- **Progress shows on the terminal; the summary goes to the log.** rsync's
  `--stats` block, the numbers of files and bytes, is written to
  `~/.local/share/ao3-calibre-backfill/logs/backup-server-<time>.log`.
- **If `BACKUP_DIR` does not exist yet**, it asks before creating it, and the
  answer defaults to no: a folder that is not there is more often a typo than a
  first run.
- **If `SERVER_BOOKORBIT_DIR` or `SERVER_LIBRARY_DIR` does not exist on the
  server**, or the server cannot be reached, it says so and copies nothing.
- **Stopping it with Ctrl-C is safe.** The server is only ever read from, and
  the next run carries on from what was already copied.

One consequence of a plain mirror is worth knowing: a file that is replaced — or
damaged — on the server replaces the backup's copy on the next run. Keep an
older copy elsewhere if that matters to you.

## What they never do

- **Delete anything**, on the server or on this PC. rsync runs with `-rt` (names
  and modification times) and never with `-a` or any `--delete`; a file that is
  gone from the server stays in the backup, and a file sent stays in
  `downloaded/`.
- **Copy permissions or owners.** exFAT cannot hold them, and on another file
  system they could leave a book unreadable to BookOrbit's container.
- **Write to the server when backing up.** The backup only reads from it.
- **Write `.env`.**

---

Built with [Claude Code](https://claude.com/claude-code). The code, tests and
documentation in this repository were written by Claude under human direction
and review.

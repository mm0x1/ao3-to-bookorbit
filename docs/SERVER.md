# The server

The library lives on a server, and BookOrbit serves it from there. This page
covers the command that talks to that server over ssh and rsync:

```sh
python3 backup_server.py     # copy BookOrbit's folder from the server to this PC
```

It takes no options. It prints the settings it is using and the exact command it
will run, then asks before it does anything. Because nobody could answer that
question off a terminal, it refuses to run under `nohup`, cron or a pipe.

1. [Settings](#settings)
2. [Backing up](#backing-up)
3. [What it never does](#what-it-never-does)

## Settings

Everything comes from `.env`, which only you edit; see
[docs/CONFIGURATION.md](CONFIGURATION.md#the-server). A run that is missing a
setting stops before it touches anything and prints the exact lines to add:

```text
ERROR  backup_server.py needs settings that are not in /path/to/ao3-to-calibre/.env. It never writes that file; add these lines to it yourself:

    # The folder on this PC the backup goes into, for example: ~/Backups/bookorbit
    BACKUP_DIR=
```

Nothing is guessed and nothing has a default. rsync pointed at a mistyped path
does not fail: it creates the folder and fills it. So every value is written
down once, in `.env`, and every run prints the values it is using.

## Backing up

```sh
python3 backup_server.py
```

```text
========================================================================
Back up the server
  SERVER                myserver
  SERVER_BOOKORBIT_DIR  /path/to/bookorbit
  BACKUP_DIR            /home/me/Backups/bookorbit
  settings from         /path/to/ao3-to-calibre/.env
  command               rsync -rt --protect-args --stats --info=progress2 myserver:/path/to/bookorbit/ /home/me/Backups/bookorbit/
========================================================================
checking that /path/to/bookorbit exists on myserver
Start the backup now? [Y/n]
```

That is the whole backup: one `rsync` pull of everything under
`SERVER_BOOKORBIT_DIR` into `BACKUP_DIR`. There is no archive, no database dump
and no copying onward — taking the backup to an external drive or a cloud folder
is up to you.

- **The first run copies everything**, which for a large library is many
  gigabytes and can take hours. Later runs copy only what changed. Nothing puts
  a time limit on the copy.
- **Progress shows on the terminal; the summary goes to the log.** rsync's
  `--stats` block, the numbers of files and bytes, is written to
  `~/.local/share/ao3-calibre-backfill/logs/backup-server-<time>.log`.
- **If `BACKUP_DIR` does not exist yet**, it asks before creating it, and the
  answer defaults to no: a folder that is not there is more often a typo than a
  first run.
- **If `SERVER_BOOKORBIT_DIR` does not exist on the server**, or the server
  cannot be reached, it says so and copies nothing.
- **Stopping it with Ctrl-C is safe.** The server is only ever read from, and
  the next run carries on from what was already copied.

One consequence of a plain mirror is worth knowing: a file that is replaced — or
damaged — on the server replaces the backup's copy on the next run. Keep an
older copy elsewhere if that matters to you.

## What it never does

- **Delete anything**, on the server or on this PC. rsync runs with `-rt` (names
  and modification times) and never with `-a` or any `--delete`; a file that is
  gone from the server stays in the backup.
- **Write to the server.** The backup only reads from it.
- **Write `.env`.**

---

Built with [Claude Code](https://claude.com/claude-code). The code, tests and
documentation in this repository were written by Claude under human direction
and review.

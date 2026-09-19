# ao3Archiver

Bulk-download works from Archive of Our Own as EPUBs with their AO3 statistics
baked in — kudos, hits, bookmarks, comments, word count, chapters, status and
category, plus a local word count and Gunning Fog readability score calculated
from the file itself — then send them to a self-hosted library server and back
that server up.

It started as a substitute for Calibre's FanFicFare plugin, which cannot get
past AO3's Cloudflare protection. The files still carry Calibre's column values,
so a book dropped into Calibre arrives with its columns filled in; the pipeline
itself now ends on a [BookOrbit](https://github.com/bookorbit/bookorbit) server.

Downloads are built to be left alone: a run of tens of thousands of works takes
days, so a failed work is recorded and skipped, an AO3-wide problem pauses the
run instead of ending it, and rerunning the same command resumes from what is
already on disk.

## The pipeline

| # | Step | Command | From → to | Runs |
| --- | --- | --- | --- | --- |
| 1 | Harvest links | [ao3downloader](https://github.com/nianeyna/ao3downloader), a separate project | an AO3 search → `links/*.txt` | by hand |
| 2 | Download | `download_fics.py` | `links/` → `downloaded/` | asks at the start, then unattended |
| 3 | Send | `send_to_server.py` | `downloaded/` → the server, then BookOrbit's fields | at a terminal |
| 4 | Scan | BookOrbit's folder watch | — | automatic |
| 5 | Back up | `backup_server.py` | the server → `BACKUP_DIR` on this PC, then a dated zip | at a terminal |

No command needs a flag in everyday use. Each prints the values it is using and
its plan, and asks yes or no at the start — never once per book. Settings come
from `.env`, which only you edit: a missing one stops the run with the exact
line to add, and nothing is guessed. Nothing is ever deleted, locally or on the
server; you empty `downloaded/` yourself.

- **Identity is the AO3 work ID.** Before downloading, `download_fics.py` asks
  BookOrbit which works are already on the server. A work you own is downloaded
  again only when it is unfinished and AO3 has more chapters, or when you say so.
- **A send adds new works and replaces owned ones in place**, at the book's own
  path, so BookOrbit keeps it as the same book. Replacing is asked separately,
  and defaults to no.
- **The backup is one rsync pull** of BookOrbit's whole folder, then a dated zip
  of it. Every zip is kept, so the backups are a history; you purge old ones.

## What it looks like

```text
18:19:39  ========================================================================
18:19:39  AO3 download
18:19:39    link files                       1
18:19:39    link lines                       35596
18:19:39    unique works                     32525
18:19:39    duplicate lines                  3071
18:19:39    unparseable lines                0
18:19:39    already in downloaded/           0
18:19:39    unavailable on AO3               0
18:19:39    not on the server                32310
18:19:39    on the server, unfinished        180
18:19:39    on the server, complete          29
18:19:39    on the server, no chapter count  6
18:19:39  ========================================================================
180 of these works are unfinished on the server. Check AO3 for new chapters (one request each)? [Y/n]
35 of these works are on the server and finished, or have no chapter count. Download them again anyway? [y/N]
18:19:41  ========================================================================
18:19:41  Plan
18:19:41    to download                32310
18:19:41    to check for new chapters  180
18:19:41    to enrich only             0
18:19:41    output folder              /path/to/ao3-to-bookorbit/downloaded
18:19:41    delay                      10s between requests
18:19:41    estimated finish           7d12h (about 2026-09-25 07:02)
18:19:41    failure log                ~/.local/share/ao3-calibre-backfill/download-failures.jsonl
18:19:41  ========================================================================
Start this download now? [Y/n]
18:19:43  [    1/32490]   0.0% work=36776911 downloaded kudos=68 hits=1040 words=1315 gfog=7.42 | --/h | elapsed 2s | eta unknown
18:20:01  [    2/32490]   0.0% work=30033096 downloaded kudos=1204 hits=22194 words=48210 gfog=8.91 | 180/h | elapsed 20s | eta 09-25 07:02
```

Every line is also written to a timestamped log file as it happens, so the run
can be started with `nohup` and followed from anywhere. Off a terminal nobody is
asked anything: each question takes its default and the run starts.

## Quickstart

**Python 3** (developed and tested on 3.14) with `requests`, and `rsync` and
`ssh` for the server commands:

```sh
git clone https://github.com/mm0x1/ao3-to-bookorbit.git && cd ao3-to-bookorbit
python3 -m venv venv && source venv/bin/activate   # optional
pip install -r requirements.txt
cp .env.example .env        # then fill it in: see docs/CONFIGURATION.md
```

Then start at the front door, which checks the setup and offers each step:

```sh
python3 start_here.py
```

```text
ao3Archiver
  Downloads Archive of Our Own works as EPUBs with their AO3 statistics baked in,
  sends them to your library server, and backs that server up.

Setup
  [ok]  .env             /path/to/ao3-to-bookorbit/.env
  [ok]  AO3 credentials  from /path/to/ao3-to-bookorbit/.env
  [ok]  BookOrbit        http://myserver:8080/api/v1
  [ok]  Server settings  myserver:/path/to/bookorbit/fanfiction
  [ok]  links/           35 works in 1 file(s)
  [--]  downloaded/      0 EPUB(s)
```

The check is offline, so it is instant even when the server is down. From the
menu it prints each step's exact command and offers to run it; each command
asks before it changes anything. The same commands by hand:

```sh
python3 download_fics.py      # links/ -> downloaded/
python3 send_to_server.py     # downloaded/ -> the server, then BookOrbit's fields
python3 backup_server.py      # the server -> BACKUP_DIR
```

**Without a server**, `download_fics.py` works on its own: the plan says it
cannot tell what you already own, and downloads every work. Drag the EPUBs into
Calibre and its custom columns fill in on import —
[the tutorial](docs/TUTORIAL.md#without-a-server-calibre) has the columns to
create first.

## The guides

| Guide | What it covers |
| --- | --- |
| [docs/TUTORIAL.md](docs/TUTORIAL.md) | The whole pipeline end to end, once, and the Calibre route without a server |
| [docs/DOWNLOADING.md](docs/DOWNLOADING.md) | `download_fics.py`: the plan, works you already own, unattended runs, and what happens when a work or AO3 fails |
| [docs/SERVER.md](docs/SERVER.md) | `send_to_server.py` and `backup_server.py` |
| [docs/BOOKORBIT.md](docs/BOOKORBIT.md) | BookOrbit's custom fields, folder watch, and the things its own docs do not cover |
| [docs/CONFIGURATION.md](docs/CONFIGURATION.md) | Every `.env` key, the SSH alias, and the files kept outside the repository |
| [docs/BACKFILL.md](docs/BACKFILL.md) | The retired Calibre backfill, for a Calibre library that already holds AO3 EPUBs |

## What goes into each EPUB

AO3's EPUB export does not include the work's statistics, so they are read from
the work page and written into the file in three forms: Calibre column values
(`calibre:user_metadata`), which fill Calibre's custom columns on import;
portable `ao3:*` metadata and an `ao3` identifier, which travel with the file
anywhere; and a visible "AO3 statistics" block in the description. The work's
rating is written as a tag, so Calibre shows it as a Tag and BookOrbit as a
Genre, with nothing to configure. BookOrbit never reads custom values out of a
file, so `send_to_server.py` sends those to its API.

[docs/TUTORIAL.md](docs/TUTORIAL.md#what-goes-into-each-epub) lists every value
and where it comes from.

## Project layout

```text
start_here.py               entry point: the setup check and the menu
download_fics.py            entry point: download works
send_to_server.py           entry point: send downloaded works to the server
backup_server.py            entry point: back the server up to this PC
retired/
  calibre_backfill.py       entry point: the retired one-time Calibre backfill
ao3archiver/
  ao3_client.py             everything that talks to AO3: login, request policy,
                            page classification, EPUB downloads, run lock, work queue
  download.py               the download workflow
  send.py                   the send: placing each file, rsync, then BookOrbit's fields
  server_backup.py          the backup
  server.py                 server settings from .env, and the ssh and rsync commands
  bookorbit.py              what BookOrbit holds, and publishing the numbers to it
  metadata.py               AO3 page statistics and EPUB metadata
  metrics.py                local Words and Gunning Fog
  credentials.py            AO3 credentials from .env or the environment
  prompts.py                questions that never block an unattended run
  welcome.py                the front door behind start_here.py
  run_log.py                logging, progress and ETA, interrupts
  common.py                 shared paths, base error, small helpers
  backfill.py               the retired backfill workflow
  calibre_library.py        columns, calibredb, backups, process checks, verification
  calibre_scripts/
    bulk_write.py           run under calibre-debug, kept apart from the package:
    embed_metadata.py       column writes, and cover-free metadata embedding
docs/                       the guides above
tests/                      one test module per package module, fakes in support.py
```

## Development

```sh
python3 -m pip install pytest
python3 -m pytest
```

The suite runs offline against a fake AO3, a fake BookOrbit and a stand-in ssh,
and needs no credentials. The send and backup tests drive the real `rsync`
between temporary folders and skip themselves when it is not installed; a few
backfill tests do the same with `calibre-debug` and `calibredb`.
[CONTRIBUTING.md](CONTRIBUTING.md) has the house style.

## Being kind to AO3

One page at a time, with a delay between requests (10 seconds by default, never
below 5), `Retry-After` always honoured, and a long back-off when AO3 says it is
struggling. Please keep it that way.

## Licence

[MIT](LICENSE).

---

Built with [Claude Code](https://claude.com/claude-code). The code, tests and
documentation in this repository were written by Claude under human direction
and review.

# Downloading

Everything `download_fics.py` does, every option it takes, and what it does when
something goes wrong.

```sh
python3 download_fics.py --dry-run     # the plan; no requests to AO3, no credentials needed
python3 download_fics.py               # the run; rerun the same command to resume
```

It reads every `*.txt` file in `links/`, keeps the first link to each work, and
asks BookOrbit which of those works are already on the server
([below](#works-already-on-the-server)). Then, for each work it is to fetch: it
fetches the AO3 work page, downloads the EPUB from the page's own Download link,
calculates local Words and Gunning Fog from the file, writes the AO3 and Calibre
metadata into the EPUB, and reads it back to confirm it will be read correctly.

## Options

| Option | Effect |
| --- | --- |
| `--dry-run` | Show the plan and estimated finish time; make no requests to AO3. |
| `--limit N` | Process at most N works. Useful for a first run. |
| `--delay SECONDS` | Seconds between AO3 requests (default 10, minimum 5). A new download costs two paced requests; an enrich-only pass, or a check for new chapters, one. |
| `--links DIR` / `--output DIR` | Read links from, and save EPUBs to, somewhere other than `links/` and `downloaded/`. |
| `--metadata-only` | Enrich EPUBs already on disk; download nothing. |
| `--refresh-metadata` | Re-read AO3 statistics even for EPUBs that are already complete. |
| `--retry-unavailable` | Include works previously found deleted or unrevealed. |
| `--failure-log PATH` | Where failed and unavailable works are recorded. |
| `--log-dir DIR` | Where this run's log file goes. |
| `--yes` | Ask nothing: start at once, and take the default answer to every other question — so it never downloads finished works you already own. |

## The plan, before anything happens

Every run — dry or real — prints what it is about to do and stops there if
nothing needs doing:

```text
AO3 download
  link files                       1
  link lines                       35596
  unique works                     32525
  duplicate lines                  3071
  unparseable lines                0
  already in downloaded/           0
  unavailable on AO3               0
  not on the server                32310
  on the server, unfinished        180
  on the server, complete          29
  on the server, no chapter count  6
180 of these works are unfinished on the server. Check AO3 for new chapters (one request each)? [Y/n]
35 of these works are on the server and finished, or have no chapter count. Download them again anyway? [y/N]
Plan
  to download                32310
  to check for new chapters  180
  to enrich only             0
  output folder              /path/to/ao3-to-bookorbit/downloaded
  delay                      10s between requests
  estimated finish           7d12h (about 2026-09-25 07:02)
  failure log                ~/.local/share/ao3-calibre-backfill/download-failures.jsonl
```

*already in downloaded/* is a file that is a valid EPUB, carries `ao3:*`
metadata, and carries Calibre column values: it is waiting to be sent, so it is
not looked up or fetched again. A file that was enriched by an older downloader
has the first two and not the third, so it is enriched again rather than
trusted. A missing, truncated or corrupt file is downloaded again.

**At a terminal**, the real run then asks `Start this download now? [Y/n]` once,
after the plan and before the first request. Answering no makes no requests at
all. **Off a terminal** — `nohup`, cron, a pipe — there is nobody to ask, so
every question takes its default and the run starts. `--yes` does the same at a
terminal.

## Works already on the server

A work is known by its AO3 work ID, so a work you already own is recognised
whatever its file is called. Before any request to AO3, the run reads
BookOrbit's list of books (one export, a few seconds) and sorts every work it
would otherwise download:

| On the server… | What happens |
| --- | --- |
| not at all | Downloaded. |
| unfinished (`3/?`, `3/10` in its AO3 Chapters field) | One question: *check AO3 for new chapters?* Default **yes**. Each check is one request, and the EPUB is downloaded only if AO3 now has more chapters than your copy; otherwise the work is counted as `unchanged` and nothing is written. |
| finished (`12/12`), or with no chapter count at all | One question: *download them again anyway?* Default **no**. |

Only the works in the links given to this run are checked; nothing else on the
server is. To download particular works again, put their links in a file of
their own, run it, and answer yes.

- **Without BookOrbit** the plan says so plainly — `on the server  unknown:
  BookOrbit is not configured, so every work is new` — and every work is
  downloaded.
- **With BookOrbit configured but unreachable**, the run stops before signing in
  to AO3, rather than download works you may already own.
- A book BookOrbit holds but cannot tell the work of — neither an AO3 Link value
  nor a `<work id>.epub` file name — is not recognised, so its work counts as new.
- A work BookOrbit holds as two books is still owned: it is named in a warning
  and counted with the works that have no chapter count, since which copy's
  count to trust is unclear.

## Leaving it running

A download of tens of thousands of works takes days, so the script is built to
run unattended:

- **A work that fails** is recorded and skipped. Failed works get one more try at
  the end of the run, and the next run tries them again.
- **A problem that affects every work** — a Cloudflare challenge, repeated rate
  limiting, a dropped login, or five failures in a row — pauses the run for 5,
  15, 30, then 60 minutes, signs in again, and carries on. AO3's `Retry-After`
  is always honoured, and a passing AO3 error (HTTP 5xx, including Cloudflare's
  525) is simply retried.
- **Rejected credentials stop the run**, since retrying them cannot succeed.
- **Only one AO3 run can use the account at a time.** `download_fics.py` refuses to
  start while a backfill fetch, a harvest or another download is running. It
  cannot see ao3downloader, which signs in to the same account, so it warns when
  ao3downloader is running: let it finish first.

```sh
nohup python3 download_fics.py &
tail -f "$(ls -t ~/.local/share/ao3-calibre-backfill/logs/download-*.log | head -1)"
```

Press Ctrl-C (or `kill` the process) to stop at the next safe point; the run
prints a summary and exits. Run the same command again to resume: it re-plans
from what is on disk, so nothing is fetched twice.

## Progress

Each completed work is one line, and every 25 works a short summary:

```text
[ 1240/32524]   3.8% work=36776911 downloaded kudos=68 hits=1040 words=1315 gfog=7.42 | 179/h | elapsed 6h55m | eta 09-18 04:12
--- progress: 1240/32524 works ---
    downloaded 1238 · unavailable 2
    179.0 works/h · elapsed 6h55m · remaining 31284 · eta 2026-09-25 07:02
```

The rate and ETA are measured from this run, not assumed from the delay, so a
cool-down is reflected in them.

## When a work fails

Failures are appended to `~/.local/share/ao3-calibre-backfill/download-failures.jsonl`,
one JSON object per line, with the work id and URL, the stage it failed at, the
error, and whether it is permanent:

```json
{"error": "HTTP 404", "error_type": "Unavailable", "failed_at": "2026-09-17T18:21:22+00:00", "permanent": true, "stage": "page", "work_id": "111", "work_url": "https://archiveofourown.org/works/111"}
```

**Permanent** means AO3 reports the work as gone or hidden — deleted, or an
unrevealed Mystery Work in a challenge collection. Those are skipped by later
runs unless you pass `--retry-unavailable`. Everything else is retried
automatically: under this request policy a failure is far more likely to be a
passing problem than a lost work.

Credentials are scrubbed from anything written to that file, as they are from
every log line.

## Files that are not EPUBs

A downloaded file is validated before it replaces anything: the bytes are
written to a temporary file in the destination folder, checked as an EPUB, and
only then moved into place. A failed download therefore leaves nothing behind,
and never overwrites a good file with a truncated one. A saved file is readable
by others as your umask allows (normally `0644`), so a library server's container
can read it once it is sent.

## Rating tags

The rating is written as a tag rather than a column. AO3's own export usually
carries it; when it does not, it is taken from the work page that was fetched
anyway, or from the EPUB's own front page. A work whose rating cannot be found
anywhere is still saved — the file is worth more than the tag — with a warning
and a count in the run summary.

---

Built with [Claude Code](https://claude.com/claude-code). The code, tests and
documentation in this repository were written by Claude under human direction
and review.

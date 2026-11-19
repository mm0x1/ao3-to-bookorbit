# Harvesting links

Everything `harvest_links.py` does: the searches file, what counts as a work
you already have, and when a search stops.

```sh
python3 harvest_links.py --dry-run     # the plan; no requests to AO3
python3 harvest_links.py               # the run; then python3 download_fics.py
```

It reads the AO3 searches in `searches.toml` and, for each one, turns the
listing's pages until it has found as many works as you asked for **that you do
not have yet**. It writes their links to `links/<search>.txt`, where
`download_fics.py` reads them.

A listing sorted by hits barely changes from one month to the next, so its first
pages are mostly works you already own. Reading "the first 20 pages" would
mostly bring back duplicates. Asking for 400 works instead keeps the harvest
going past the pages you already own until it has found 400 you don't.

## The searches file

Copy `searches.example.toml` to `searches.toml`. Git ignores the real file.
Add one `[[search]]` table for each AO3 listing:

```toml
[[search]]
name = "DCU"
url = "https://archiveofourown.org/tags/DCU/works?work_search%5Bsort_column%5D=hits"
works = 400

[[search]]
name = "Star Wars"
url = "https://archiveofourown.org/works?work_search%5Bsort_column%5D=hits&tag_id=Star+Wars+-+All+Media+Types"
works = 200
max_pages = 100
```

| Key | Meaning |
| --- | --- |
| `name` | A short name. It names the link file, so `"Star Wars"` writes `links/star-wars.txt`, and no two searches may share one. |
| `url` | The address of the AO3 page that lists the works. Filter and sort it on AO3, then copy it from the address bar. This can be a tag's works, a filtered search, a collection or a user's bookmarks. A single work's page is refused. |
| `works` | How many works you do not have yet to bring back. |
| `max_pages` | Optional. The search stops after this many listing pages, even if it has found fewer works. The default is 5 times the pages `works` would need: 400 works → 20 pages → at most 100. |

Anything the harvest cannot use, such as a missing `works`, a misspelt key or a
link to some other site, stops the run before any request with the exact
problem. Nothing is guessed. `start_here.py` checks the file too.

## What counts as already had

A listed work is skipped, and does not count towards `works`, when it is:

| Where | How it is known |
| --- | --- |
| on the server | BookOrbit's AO3 Link field, or a `<work id>.epub` file name, as `download_fics.py` checks |
| in `downloaded/` | a `<work id>.epub` file waiting to be sent |
| already in `links/` | any link file, from an earlier harvest or added by hand |
| unavailable on AO3 | `download_fics.py`'s failure log says it was deleted or is hidden |
| found by an earlier search | in this run; crossovers sit in several fandoms and are counted once |

Without BookOrbit configured, the harvest cannot see what is on the server, says
so in the plan, and counts those works as new.

**Unfinished works with new chapters.** If the server has a work at, say, 6/10
chapters and the listing shows 7/10, its link is written too, so
`download_fics.py` updates it. It does not count towards `works`. The listing
already shows the chapter count, so this costs no extra request.

## When a search stops

A search stops at the first of:

- **It has found `works` new works.** Any new works left on that page are left
  for next time.
- **The listing has no next page.** The search has read everything AO3 lists.
- **It reaches `max_pages`.** The listing is mostly works you already have. The
  summary at the end says how many it found and suggests raising `max_pages` if
  you want it to look deeper.

## Requests, and being kind to AO3

Each listing page is one request. Pages are fetched one at a time, **30 seconds
apart** by default (`--delay`, minimum 5). Before the first request, the plan
shows the fewest and the most pages the run can take, and how long that is.

The harvest signs in with the AO3 account in `.env`, because AO3 leaves works
restricted to signed-in users out of a listing for anyone signed out. If AO3
serves a page as if signed out, the harvest signs in again once. It follows the
same rules as the download for rate limits, Cloudflare and AO3 errors, and it
holds the same one-run-per-account lock, so a harvest and a download never run
at once.

A search that fails, such as a listing that returns 404, is reported and the
next search runs.

## Interrupting and rerunning

Links are appended to the search's file after every page, so `Ctrl-C` keeps
everything found so far. A rerun skips those works because they are now in
`links/`. Each run looks for `works` more works, counting from where the
previous run left off.

## Options

| Option | Effect |
| --- | --- |
| `--dry-run` | Show the plan without any request to AO3. |
| `--searches FILE` | Read a different searches file. |
| `--links DIR` / `--output DIR` | Use other folders than `links/` and `downloaded/`. |
| `--delay SECONDS` | Seconds between AO3 requests (default 30, minimum 5). |
| `--failure-log FILE` | The download's failure log, if you moved it. |
| `--log-dir DIR` | Where this run's log file goes. |
| `--yes` | Start without asking. Off a terminal it starts anyway: a harvest only adds lines to `links/`. |

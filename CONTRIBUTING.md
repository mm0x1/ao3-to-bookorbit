# Contributing

This is a personal project, published because it might be useful to someone
else with a large Archive of Our Own library. Issues and pull requests are
welcome, and so is a bug report that goes no further than "this happened".
Nobody is on call for it.

## Running the tests

```sh
python3 -m pip install pytest
python3 -m pytest
```

The suite runs offline against a fake AO3, a fake BookOrbit and a stand-in
ssh: it makes no network requests and needs no credentials. The send and backup
tests drive the real `rsync` between temporary folders, and a few backfill tests
the real `calibre-debug` and `calibredb`; each skips itself when its program is
not installed.

Please run the whole suite before opening a pull request.

## House style

- **Small, focused changes.** One logical change per commit, and a pull request
  that can be read in one sitting.
- **One script per step.** Each pipeline step is an entry script at the root
  (`harvest_links.py`, `download_fics.py`, `send_to_server.py`,
  `backup_server.py`), a few lines
  that call into `ao3archiver/`; `start_here.py` is the front door, and the
  retired Calibre backfill lives in `retired/`. README.md has the full layout.
- **Tests alongside the code.** One test module per package module, functional
  rather than mock-heavy: the fakes in `tests/support.py` stand in for AO3, for
  EPUBs and for a scan report, so a test can exercise a real code path.
- **Say why in a comment, not what.** The code says what it does.
- **Nothing personal in the repository.** No real paths, hostnames, addresses or
  library contents, in code, tests or documentation. Logs, caches and backups
  live in `~/.local/share/ao3-calibre-backfill/` for that reason.
- **Do not break an unattended run.** The download is started with `nohup` and
  expected to survive for days. A question is asked only at a terminal and only
  before work starts, never once per book; off a terminal it takes its default,
  so the default must be the safe answer. See `ao3archiver/prompts.py`.
- **Ask, do not flag.** The everyday commands need no flags: each prints the
  values it is using and its plan, then asks yes or no. The two that change the
  server, `send_to_server.py` and `backup_server.py`, refuse to run off a
  terminal rather than guess. The retired backfill keeps its `--approve-…` flags.
- **Nothing guessed, nothing deleted.** Settings are explicit `.env` keys that
  only the user writes: a missing one stops the run with the exact line to add,
  and nothing has a hidden default. No command deletes anything, locally or on
  the server: rsync runs with `-rt`, never `-a` or `--delete`.

## Being kind to AO3

This project fetches one page at a time, with a delay between requests, and
backs off when AO3 says to. Please do not send a change that raises the request
rate, removes a delay, or works around a rate limit.

---

Built with [Claude Code](https://claude.com/claude-code). The code, tests and
documentation in this repository were written by Claude under human direction
and review.

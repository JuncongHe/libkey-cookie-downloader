# lkfetch

Unofficial LibKey PDF downloader for single DOIs or a sequential DOI file. Requires Python 3.11+ and an authorized Chrome session.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Set the library ID and Chrome cookie domain, or pass them with `--library-id` and `--cookie-domain` (CLI values take precedence):

```sh
export LKFETCH_LIBRARY_ID=example_library
export LKFETCH_COOKIE_DOMAIN=example.invalid
lkfetch download '10.1234/example' --output-dir ./pdfs
```

`python -m lkfetch download ...` works too. The command resolves one DOI through `https://libkey.io`, follows normal redirects, waits up to 60 seconds, and saves only responses identified as PDFs. Existing files are skipped. It reports an authentication error for 401/403 and a rate-limit error for 429. Use it only for content your account is licensed to access, and respect provider rate limits.

For a UTF-8 file with one DOI per line:

```sh
lkfetch batch dois.txt --library-id example_library --cookie-domain example.invalid --output-dir ./pdfs --delay 3
```

Blank lines and lines whose first non-whitespace character is `#` are ignored. CLI library and cookie-domain options override the environment variables as in `download`. Batch processes one DOI at a time in file order, waiting 3 seconds by default between valid DOI attempts; `--delay` accepts finite non-negative seconds (`0` is useful for tests). It skips existing PDFs, continues after invalid input and ordinary download errors, and stops after a rate limit or 401/403 authentication error. Item output contains only normalized DOI, target path, status, and error category; invalid lines are never echoed. The summary counts downloaded, skipped, and failed items and states the stop reason when stopped early. Batch exits nonzero if any item fails.

Cookies are read from Chrome for the specified domain and kept in this process; they are not written to disk or printed. The downloaded PDF is written through a temporary file in the output directory, which must support hard links. Manifests, MCP support, concurrency, and retry are unimplemented. This repository has not verified a live download.

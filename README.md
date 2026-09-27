# lkfetch

Unofficial single-DOI LibKey PDF downloader. Requires Python 3.11+ and an authorized Chrome session.

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

Cookies are read from Chrome for the specified domain and kept in this process; they are not written to disk or printed. The downloaded PDF is written through a temporary file in the output directory, which must support hard links. Batch downloads, manifests, and MCP support are future work. This repository has not verified a live download.

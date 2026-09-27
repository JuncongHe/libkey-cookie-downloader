# lkfetch

Unofficial LibKey PDF downloader for single DOIs or a sequential DOI file. Requires Python 3.11+ and an authorized Chrome or Dia session.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Set the library ID and browser cookie domain, or pass them with `--library-id` and `--cookie-domain` (CLI values take precedence):

```sh
export LKFETCH_LIBRARY_ID=example_library
export LKFETCH_COOKIE_DOMAIN=example.invalid
lkfetch download '10.1234/example' --output-dir ./pdfs
```

Chrome is the default. To use Dia, select it and optionally name a Dia profile:

```sh
lkfetch doctor --browser dia --profile "Profile 2"
lkfetch download '10.1234/example' --browser dia --profile "Profile 2" --output-dir ./pdfs
lkfetch batch dois.txt --browser dia --profile "Profile 2" --output-dir ./pdfs
```

All three commands accept `--browser chrome|dia` and `--profile NAME`. The environment equivalents are `LKFETCH_BROWSER` and `LKFETCH_BROWSER_PROFILE`; command-line values override them. Dia accepts `Default` or `Profile N`. Without `--profile`, Dia selects the unique profile with cookies for the requested domain; if several match, specify one with `--profile`. Chrome profile selection is unsupported, so omit `--profile` with Chrome. The library ID and cookie domain are still required through options or environment variables.

`python -m lkfetch download ...` works too. The command uses the selected browser session to request a temporary LibKey API token, resolves the DOI through the Third Iron articles API, and downloads its `fullTextFile` URL. Each request waits up to 60 seconds; only responses identified as PDFs are saved. Existing files are skipped. It reports an authentication error for 401/403 and a rate-limit error for 429. Use it only for content your account is licensed to access, and respect provider rate limits.

Check configuration and browser cookie access first:

```sh
lkfetch doctor --cookie-domain example.invalid --library-id example_library
```

`doctor` also accepts the environment variables above; CLI options take precedence. It makes no LibKey network request and prints no URL, cookie names, values, headers, or profile paths. It reports dependency, configuration, configured/selected browser and profile names, and reader status with a fixed hint for cookie failures. For Dia, `Profile: configured=auto` means no profile was specified. Exit 0 means matching cookies were found, 1 means the dependency or reader is unavailable, access timed out or was denied, or there are no matching cookies, and 2 means a library ID or cookie domain is missing.

Dia requires the installed `browser-cookie3` package and macOS `/usr/bin/security` to decrypt its local cookies. Unlock the macOS Passwords app if it is locked; macOS may then ask you to approve Keychain access, where **Allow** is required. Denying access or leaving the prompt unanswered until it times out makes the command fail with a nonzero exit status. Run `doctor --browser dia` to check access without downloading a PDF.

For a UTF-8 file with one DOI per line:

```sh
lkfetch batch dois.txt --library-id example_library --cookie-domain example.invalid --output-dir ./pdfs --delay 3
```

Blank lines and lines whose first non-whitespace character is `#` are ignored. CLI options override environment variables as in `download`. Batch processes one DOI at a time in file order, waiting 3 seconds by default between valid DOI attempts; `--delay` accepts finite non-negative seconds (`0` is useful for tests). It skips existing PDFs, continues after invalid input and ordinary download errors, and stops after a rate limit, 401/403 authentication error, cookie access timeout or denial, cookie read error, or invalid/ambiguous profile selection. Item output contains only normalized DOI, target path, status, and error category (plus a fixed hint for cookie failures); invalid lines are never echoed. The summary counts downloaded, skipped, and failed items and states the stop reason when stopped early. Batch exits nonzero if any item fails.

Cookies are read from the selected browser for the specified domain and kept in memory in this process; they are not written to disk or printed. The temporary API token also stays in memory. The downloaded PDF is written through a temporary file in the output directory, which must support hard links. Manifests, MCP support, concurrency, and retry are unimplemented. The current implementation has been verified with a live Dia browser session; provider and institution behavior can still vary by DOI.

# Agent instructions

Use the local `lkfetch` CLI for licensed paper retrieval. The CLI reads the
selected browser session inside the local process; never request, print, save,
or return cookie values, credentials, or browser profile files.

Configuration is user- and institution-specific. Obtain the library ID, the
authenticated cookie domain, and the browser/profile choice from the user's
local setup or environment; do not invent, hardcode, or commit them. The
cookie domain must be the domain that actually carries the library/proxy
session, not merely a DOI resolver or affiliation website.

Before downloading, run `lkfetch doctor` with those values and require a ready
result. Use `download` for one DOI and `batch` for a UTF-8 DOI list. Batch work
is intentionally sequential; do not add parallel requests.

Chrome is the default. For Dia, use `--browser dia` and, when needed,
`--profile "Profile N"`; select the browser that actually contains the
authenticated session rather than assuming the CLI default. If Dia reports a
Keychain timeout or denial, ask the user to unlock Passwords or approve access,
then retry. Do not bypass MFA, CAPTCHA, access-denied pages, publisher
restrictions, or rate limits. Batch is sequential and should continue after a
per-DOI failure, including `authentication_error` from an HTTP 401/403 and
`non_pdf`; stop only for HTTP 429 (`rate_limited`) or browser cookie,
Keychain, and configuration failures.

Only report the DOI, output path, and status returned by `lkfetch`. Do not
expose browser internals or session material in logs, prompts, or commits.

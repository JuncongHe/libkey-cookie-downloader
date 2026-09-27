# Agent instructions

Use the local `lkfetch` CLI for licensed paper retrieval. The CLI reads the
selected browser session inside the local process; never request, print, save,
or return cookie values, credentials, or browser profile files.

Before downloading, run `lkfetch doctor` with the configured library ID and
cookie domain. Use `download` for one DOI and `batch` for a UTF-8 DOI list.
Batch work is intentionally sequential; do not add parallel requests.

Chrome is the default. For Dia, use `--browser dia` and, when needed,
`--profile "Profile N"`. If Dia reports a Keychain timeout or denial, ask the
user to unlock Passwords or approve access, then retry. Do not bypass MFA,
CAPTCHA, access-denied pages, publisher restrictions, or rate limits; stop on
repeated authentication failures or HTTP 429.

Only report the DOI, output path, and status returned by `lkfetch`. Do not
expose browser internals or session material in logs, prompts, or commits.

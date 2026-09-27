# AI agent and Codex Skill integration

`lkfetch` is a local CLI, not an MCP server. An agent or Skill should call the
CLI and receive only the DOI, output path, status, and sanitized error
category. The browser session and the temporary API token stay inside the
local `lkfetch` process.

## 1. Install the CLI

From a checkout of this repository:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e .
```

The command is then available as `.venv/bin/lkfetch`. If the virtual
environment is activated, use `lkfetch` directly:

```sh
. .venv/bin/activate
lkfetch --help
```

The package can also be installed from GitHub into a dedicated virtual
environment:

```sh
python3 -m venv /path/to/lkfetch-venv
/path/to/lkfetch-venv/bin/python -m pip install \
  'git+https://github.com/JuncongHe/libkey-cookie-downloader.git'
```

## 2. Configure the local session

Set the non-secret institution settings in the agent's environment, or pass
them as CLI options:

```sh
export LKFETCH_LIBRARY_ID='your_library_id'
export LKFETCH_COOKIE_DOMAIN='proxy.library.example.edu'
export LKFETCH_BROWSER='chrome'       # default; or dia
export LKFETCH_BROWSER_PROFILE='Profile 2'  # optional for Dia
```

Check the session before asking the agent to download:

```sh
lkfetch doctor
```

`LKFETCH_COOKIE_DOMAIN` must be the domain that carries the authenticated
library/proxy session. It is usually an EZproxy or institutional proxy domain,
not `libkey.io`, which identifies the LibKey site or affiliation. Do not guess
the browser or domain after a failed check; run `doctor` with the browser and
profile that actually contain the logged-in session.

For the verified Rutgers/Dia setup used during development:

```sh
lkfetch doctor \
  --library-id 1726 \
  --cookie-domain proxy.libraries.rutgers.edu \
  --browser dia \
  --profile 'Profile 2'
```

For Dia on macOS, unlock Passwords and approve the Keychain prompt if macOS
asks. The agent should ask the user to do this through the normal browser and
macOS UI; it must not request or print the secret itself.

## 3. Add a thin Skill wrapper (optional)

The project-level [`AGENTS.md`](../AGENTS.md) already gives agents the safe
workflow when they work in this repository. A global Codex Skill can use the
same workflow without copying the downloader implementation.

Create a directory such as `~/.codex/skills/lkfetch/` and save this as
`SKILL.md`:

```markdown
# lkfetch paper retrieval

Use this skill when a user asks to retrieve a licensed paper PDF by DOI.

1. Run `lkfetch doctor` before the first download.
2. Run `lkfetch download <DOI> --output-dir <DIR>` for one DOI.
3. Put one DOI per UTF-8 line and run `lkfetch batch <FILE> --output-dir <DIR>`
   for multiple papers.
4. Report only the DOI, output path, status, and sanitized error category.
5. Never request, print, save, inspect, or return browser cookies, tokens,
   credentials, browser databases, or profile files.
6. Do not silently change browser, profile, or cookie domain after a failed
   `doctor`; ask the user to confirm the browser session and proxy domain.
7. If authentication, Keychain, MFA, CAPTCHA, access-denied, or rate-limit
   handling is required, stop and ask the user to use the normal browser flow.
```

Restart or reload the agent after installing a global Skill, then invoke it by
asking for a paper download by DOI. The Skill does not replace the `lkfetch`
executable; it only supplies the agent-facing instructions.

## 4. Typical agent calls

Single paper:

```sh
lkfetch doctor
lkfetch download '10.1234/example' --output-dir ./papers
```

Batch:

```sh
lkfetch batch dois.txt --output-dir ./papers --delay 3
```

The batch command is sequential by design. It stops on authentication, cookie
access, profile-selection, or rate-limit failures and does not bypass MFA,
CAPTCHA, publisher restrictions, or access controls.

## 5. What is not included

This repository does not currently install an MCP server or expose a cookie
tool. A future MCP wrapper should call the same CLI/library boundary and return
the same sanitized result fields; it must not move cookie or token handling
into the agent context.

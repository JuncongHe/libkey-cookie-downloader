"""Load domain-scoped cookies from Chrome or Dia on macOS."""

import re
import sqlite3
import subprocess
import threading
from http.cookiejar import CookieJar
from pathlib import Path


class BrowserError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        self.message = message
        super().__init__(message)


def _matches_domain(host: str, domain: str) -> bool:
    host = host.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


def _readonly_connection(path: Path):
    return sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)


def _dia_database(domain: str, profile: str | None) -> tuple[Path, str]:
    if profile is not None and not re.fullmatch(r"Default|Profile [0-9]+", profile):
        raise BrowserError("cookie_config", "invalid Dia profile name")

    root = Path.home() / "Library/Application Support/Dia/User Data"
    try:
        names = [profile] if profile else sorted(
            path.name for path in root.iterdir()
            if path.is_dir() and re.fullmatch(r"Default|Profile [0-9]+", path.name)
        )
        matches = []
        for name in names:
            directory = root / name
            if directory.is_symlink():
                continue
            for database in (directory / "Network/Cookies", directory / "Cookies"):
                if not database.is_file() or database.is_symlink():
                    continue
                connection = None
                try:
                    with _readonly_connection(database) as connection:
                        if any(_matches_domain(host, domain) for (host,) in connection.execute("SELECT host_key FROM cookies")):
                            matches.append((database, name))
                            break
                finally:
                    if connection is not None:
                        connection.close()
    except (OSError, sqlite3.Error, TypeError, AttributeError):
        raise BrowserError("cookie_error", "could not inspect Dia cookie profiles") from None

    if not matches:
        raise BrowserError("cookie_error", "no matching Dia cookie profile")
    if len(matches) > 1:
        raise BrowserError("cookie_config", "multiple Dia profiles match; select a profile")
    return matches[0]


def _dia_password() -> bytearray:
    try:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-w",
                "-a",
                "Dia",
                "-s",
                "Dia Safe Storage",
            ],
            timeout=10, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        raise BrowserError("cookie_timeout", "Dia Keychain request timed out") from None
    except OSError:
        raise BrowserError("cookie_error", "could not access Dia Keychain entry") from None

    if result.returncode:
        raise BrowserError("cookie_denied", "Dia Keychain access denied")
    password = bytearray(result.stdout.strip())
    if not password:
        raise BrowserError("cookie_error", "Dia Keychain entry is empty")
    return password


class _ReadOnlyConnection:
    """Keep browser_cookie3.load away from its copy-to-tempfile fallback."""

    def __init__(self, database_file):
        self.database_file = Path(database_file)

    def __enter__(self):
        self.connection = _readonly_connection(self.database_file)
        return self.connection

    def __exit__(self, *_):
        self.connection.close()


_DIA_LOAD_LOCK = threading.Lock()


def _load_dia(database: Path, domain: str) -> CookieJar:
    import browser_cookie3

    reader = object.__new__(browser_cookie3.ChromiumBased)
    reader.browser = "Dia"
    reader.cookie_file = database
    reader.domain_name = domain
    reader.salt = b"saltysalt"
    reader.iv = b" " * 16
    password = _dia_password()
    try:
        reader.v10_key = browser_cookie3.PBKDF2(password, reader.salt, 16, 1003)
    finally:
        password[:] = b"\x00" * len(password)
        password.clear()

    # ponytail: browser_cookie3 0.20.1 hardcodes this global; remove the swap if it gains a connection hook.
    with _DIA_LOAD_LOCK:
        original = browser_cookie3._DatabaseConnetion
        browser_cookie3._DatabaseConnetion = _ReadOnlyConnection
        try:
            cookies = browser_cookie3.ChromiumBased.load(reader)
        finally:
            browser_cookie3._DatabaseConnetion = original

    scoped = CookieJar()
    for cookie in cookies:
        if _matches_domain(cookie.domain, domain):
            scoped.set_cookie(cookie)
    return scoped


def load_browser_cookies(
    cookie_domain: str, *, browser: str = "chrome", profile: str | None = None,
    cookie_loader=None,
) -> tuple[CookieJar, str | None]:
    """Return cookies for one domain and the selected Dia profile, if any."""
    domain = cookie_domain.lstrip(".").lower()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]*", domain):
        raise BrowserError("cookie_config", "invalid cookie domain")

    if browser == "chrome":
        if profile is not None:
            raise BrowserError("cookie_config", "Chrome profile selection is unavailable")
        try:
            if cookie_loader is None:
                from browser_cookie3 import chrome as cookie_loader
            return cookie_loader(domain_name=cookie_domain), None
        except Exception:
            raise BrowserError("cookie_error", "could not load browser cookies") from None
    if browser != "dia":
        raise BrowserError("cookie_config", "unsupported browser")

    database, selected_profile = _dia_database(domain, profile)
    try:
        return _load_dia(database, domain), selected_profile
    except BrowserError:
        raise
    except Exception:
        raise BrowserError("cookie_error", "could not load Dia cookies") from None

"""Command-line interface for single and sequential DOI downloads."""

import argparse
import math
import os
import re
import sys
import time

from .browser import BrowserError, load_browser_cookies
from .download import DownloadError, chrome_cookie_loader, download_pdf, normalize_doi, target_for, valid_cookie_domain


_ERROR_MESSAGES = {
    "invalid_input": "check the DOI, library ID, and cookie domain",
    "cookie_config": "check the browser setting or select a Dia profile with --profile",
    "cookie_timeout": "Keychain request timed out; unlock Passwords or approve access and retry",
    "cookie_denied": "Keychain access denied; allow access and retry",
    "cookie_error": "could not read browser cookies; check browser access",
    "non_pdf": "service returned a non-PDF response",
    "file_error": "could not save PDF",
    "authentication_error": "browser session was not authorized",
    "proxy_login": "proxy login required; sign in through your library and retry",
    "api_token_error": "service did not provide a usable API token",
    "article_not_found": "article was not found",
    "article_error": "service did not provide a valid article PDF URL",
    "rate_limited": "service asked to slow down",
    "http_error": "PDF service returned an error",
    "network_error": "could not reach the PDF service",
    "download_error": "could not download PDF",
}
_COOKIE_ERRORS = {"cookie_timeout", "cookie_denied", "cookie_error", "cookie_config"}


def _delay(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("delay must be a finite non-negative number") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("delay must be a finite non-negative number")
    return seconds


def _browser_options(args: argparse.Namespace) -> tuple[str, str | None]:
    browser = args.browser if args.browser is not None else os.getenv("LKFETCH_BROWSER") or "chrome"
    profile = args.profile if args.profile is not None else os.getenv("LKFETCH_BROWSER_PROFILE")
    return browser, profile


def _profile_name(value: str | None, missing: str) -> str:
    if value is None:
        return missing
    return value if re.fullmatch(r"Default|Profile [0-9]+", value) else "invalid"


def _batch(args: argparse.Namespace) -> int:
    library_id = args.library_id if args.library_id is not None else os.getenv("LKFETCH_LIBRARY_ID")
    cookie_domain = args.cookie_domain if args.cookie_domain is not None else os.getenv("LKFETCH_COOKIE_DOMAIN")
    if not library_id or not library_id.strip():
        print("Status: failure (missing_config: set --library-id or LKFETCH_LIBRARY_ID)", file=sys.stderr)
        return 2
    if not cookie_domain or not cookie_domain.strip():
        print("Status: failure (missing_config: set --cookie-domain or LKFETCH_COOKIE_DOMAIN)", file=sys.stderr)
        return 2

    browser, profile = _browser_options(args)
    downloaded = skipped = failed = 0
    stopped = None
    attempted = False
    try:
        with open(args.doi_file, encoding="utf-8") as lines:
            for line in lines:
                value = line.rstrip("\r\n")
                if not value.strip() or value.lstrip().startswith("#"):
                    continue
                try:
                    doi = normalize_doi(value)
                except DownloadError:
                    print("DOI: invalid\nStatus: failure (invalid_input)")
                    failed += 1
                    continue
                target = target_for(doi, args.output_dir)
                if attempted:
                    time.sleep(args.delay)
                attempted = True
                try:
                    target, status = download_pdf(doi, library_id, cookie_domain, args.output_dir, browser=browser, profile=profile)
                except DownloadError as error:
                    category = error.category if error.category in _ERROR_MESSAGES else "download_error"
                except OSError:
                    category = "file_error"
                except Exception:
                    category = "download_error"
                else:
                    category = None
                    if status == "skipped_existing":
                        skipped += 1
                    else:
                        downloaded += 1
                if category is None:
                    print(f"DOI: {doi}\nPath: {target}\nStatus: {status}")
                else:
                    failed += 1
                    hint = f": {_ERROR_MESSAGES[category]}" if category in _COOKIE_ERRORS else ""
                    print(f"DOI: {doi}\nPath: {target}\nStatus: failure ({category}{hint})")
                    if category == "rate_limited" or category in _COOKIE_ERRORS:
                        stopped = category
                        break
    except (OSError, UnicodeError):
        print("Status: failure (file_error: could not read DOI file)", file=sys.stderr)
        return 2
    summary = f"Summary: downloaded={downloaded} skipped_existing={skipped} failed={failed}"
    print(f"{summary} stopped={stopped}" if stopped else summary)
    return 1 if failed else 0


def _doctor(args: argparse.Namespace) -> int:
    library_id = args.library_id if args.library_id is not None else os.getenv("LKFETCH_LIBRARY_ID")
    cookie_domain = args.cookie_domain if args.cookie_domain is not None else os.getenv("LKFETCH_COOKIE_DOMAIN")
    library_ready = bool(library_id and library_id.strip())
    domain_ready = bool(cookie_domain and cookie_domain.strip())
    browser, profile = _browser_options(args)
    dependency = False
    if browser in {"chrome", "dia"}:
        try:
            chrome_cookie_loader()
            dependency = browser == "chrome" or (os.path.isfile("/usr/bin/security") and os.access("/usr/bin/security", os.X_OK))
        except Exception:
            pass

    reader = "error"
    selected_browser = None
    selected_profile = None
    if dependency and library_ready and domain_ready and valid_cookie_domain(cookie_domain.strip()):
        try:
            cookies, selected_profile = load_browser_cookies(cookie_domain.strip(), browser=browser, profile=profile)
            selected_browser = browser
            reader = "ready" if len(cookies) else "no matching cookies"
        except BrowserError as error:
            reader = error.category if error.category in _COOKIE_ERRORS else "error"
        except Exception:
            pass

    print(f"Dependency: {'ready' if dependency else 'missing'}")
    print(f"library_id: {'configured' if library_ready else 'missing'}")
    print(f"cookie_domain: {'configured' if domain_ready else 'missing'}")
    print(f"Browser: configured={browser if browser in {'chrome', 'dia'} else 'invalid'} selected={selected_browser or 'none'}")
    print(f"Profile: configured={_profile_name(profile, 'auto' if browser == 'dia' else 'default')} selected={_profile_name(selected_profile, 'none')}")
    hint = f": {_ERROR_MESSAGES[reader]}" if reader in _COOKIE_ERRORS else ""
    print(f"{browser.capitalize() if browser in {'chrome', 'dia'} else 'Browser'} cookie reader: {reader}{hint}")
    return 2 if not (library_ready and domain_ready) else 0 if reader == "ready" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lkfetch", description="Download DOI PDFs using browser cookies.")
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="download one DOI PDF")
    download.add_argument("doi", metavar="DOI")
    download.add_argument("--library-id", metavar="ID")
    download.add_argument("--cookie-domain", metavar="DOMAIN")
    download.add_argument("--output-dir", default=".", metavar="DIR")
    download.add_argument("--browser", choices=("chrome", "dia"))
    download.add_argument("--profile", metavar="NAME")
    batch = commands.add_parser("batch", help="download DOI PDFs sequentially from a UTF-8 file")
    batch.add_argument("doi_file", metavar="DOI_FILE")
    batch.add_argument("--library-id", metavar="ID")
    batch.add_argument("--cookie-domain", metavar="DOMAIN")
    batch.add_argument("--output-dir", default=".", metavar="DIR")
    batch.add_argument("--delay", type=_delay, default=3.0, metavar="SECONDS")
    batch.add_argument("--browser", choices=("chrome", "dia"))
    batch.add_argument("--profile", metavar="NAME")
    doctor = commands.add_parser("doctor", help="check browser cookie access without downloading")
    doctor.add_argument("--cookie-domain", metavar="DOMAIN")
    doctor.add_argument("--library-id", metavar="ID")
    doctor.add_argument("--browser", choices=("chrome", "dia"))
    doctor.add_argument("--profile", metavar="NAME")
    args = parser.parse_args(argv)
    if args.command == "batch":
        return _batch(args)
    if args.command == "doctor":
        return _doctor(args)

    try:
        doi = normalize_doi(args.doi)
    except DownloadError as error:
        print(f"DOI: invalid\nStatus: failure ({error.category}: {error})", file=sys.stderr)
        return 2
    target = target_for(doi, args.output_dir)
    library_id = args.library_id if args.library_id is not None else os.getenv("LKFETCH_LIBRARY_ID")
    cookie_domain = args.cookie_domain if args.cookie_domain is not None else os.getenv("LKFETCH_COOKIE_DOMAIN")
    if not library_id or not library_id.strip():
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure (missing_config: set --library-id or LKFETCH_LIBRARY_ID)", file=sys.stderr)
        return 2
    if not cookie_domain or not cookie_domain.strip():
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure (missing_config: set --cookie-domain or LKFETCH_COOKIE_DOMAIN)", file=sys.stderr)
        return 2

    browser, profile = _browser_options(args)
    try:
        target, status = download_pdf(doi, library_id, cookie_domain, args.output_dir, browser=browser, profile=profile)
    except DownloadError as error:
        category = error.category if error.category in _ERROR_MESSAGES else "download_error"
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure ({category}: {_ERROR_MESSAGES[category]})", file=sys.stderr)
        return 1
    except OSError:
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure (file_error: could not save PDF)", file=sys.stderr)
        return 1
    except Exception:
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure (download_error: could not download PDF)", file=sys.stderr)
        return 1
    print(f"DOI: {doi}\nPath: {target}\nStatus: {status}")
    return 0

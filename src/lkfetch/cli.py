"""Command-line interface for single and sequential DOI downloads."""

import argparse
import math
import os
import sys
import time

from .download import DownloadError, download_pdf, normalize_doi, target_for


def _delay(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("delay must be a finite non-negative number") from None
    if not math.isfinite(seconds) or seconds < 0:
        raise argparse.ArgumentTypeError("delay must be a finite non-negative number")
    return seconds


def _batch(args: argparse.Namespace) -> int:
    library_id = args.library_id if args.library_id is not None else os.getenv("LKFETCH_LIBRARY_ID")
    cookie_domain = args.cookie_domain if args.cookie_domain is not None else os.getenv("LKFETCH_COOKIE_DOMAIN")
    if not library_id or not library_id.strip():
        print("Status: failure (missing_config: set --library-id or LKFETCH_LIBRARY_ID)", file=sys.stderr)
        return 2
    if not cookie_domain or not cookie_domain.strip():
        print("Status: failure (missing_config: set --cookie-domain or LKFETCH_COOKIE_DOMAIN)", file=sys.stderr)
        return 2

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
                    target, status = download_pdf(doi, library_id, cookie_domain, args.output_dir)
                except DownloadError as error:
                    category = error.category
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
                    print(f"DOI: {doi}\nPath: {target}\nStatus: failure ({category})")
                    if category in {"rate_limited", "authentication_error"}:
                        stopped = category
                        break
    except (OSError, UnicodeError):
        print("Status: failure (file_error: could not read DOI file)", file=sys.stderr)
        return 2
    summary = f"Summary: downloaded={downloaded} skipped_existing={skipped} failed={failed}"
    print(f"{summary} stopped={stopped}" if stopped else summary)
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lkfetch", description="Download DOI PDFs using Chrome cookies.")
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="download one DOI PDF")
    download.add_argument("doi", metavar="DOI")
    download.add_argument("--library-id", metavar="ID")
    download.add_argument("--cookie-domain", metavar="DOMAIN")
    download.add_argument("--output-dir", default=".", metavar="DIR")
    batch = commands.add_parser("batch", help="download DOI PDFs sequentially from a UTF-8 file")
    batch.add_argument("doi_file", metavar="DOI_FILE")
    batch.add_argument("--library-id", metavar="ID")
    batch.add_argument("--cookie-domain", metavar="DOMAIN")
    batch.add_argument("--output-dir", default=".", metavar="DIR")
    batch.add_argument("--delay", type=_delay, default=3.0, metavar="SECONDS")
    args = parser.parse_args(argv)
    if args.command == "batch":
        return _batch(args)

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

    try:
        target, status = download_pdf(doi, library_id, cookie_domain, args.output_dir)
    except DownloadError as error:
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure ({error.category}: {error})", file=sys.stderr)
        return 1
    except OSError:
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure (file_error: could not save PDF)", file=sys.stderr)
        return 1
    except Exception:
        print(f"DOI: {doi}\nPath: {target}\nStatus: failure (download_error: could not download PDF)", file=sys.stderr)
        return 1
    print(f"DOI: {doi}\nPath: {target}\nStatus: {status}")
    return 0

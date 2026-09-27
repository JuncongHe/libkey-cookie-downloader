"""Command-line interface for one DOI at a time."""

import argparse
import os
import sys

from .download import DownloadError, download_pdf, normalize_doi, target_for


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="lkfetch", description="Download one DOI PDF using Chrome cookies.")
    commands = parser.add_subparsers(dest="command", required=True)
    download = commands.add_parser("download", help="download one DOI PDF")
    download.add_argument("doi", metavar="DOI")
    download.add_argument("--library-id", metavar="ID")
    download.add_argument("--cookie-domain", metavar="DOMAIN")
    download.add_argument("--output-dir", default=".", metavar="DIR")
    args = parser.parse_args(argv)

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

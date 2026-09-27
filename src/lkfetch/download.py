"""Download one PDF using browser cookies kept in memory."""

import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .browser import BrowserError, load_browser_cookies


class DownloadError(Exception):
    def __init__(self, category: str, message: str):
        self.category = category
        super().__init__(message)


def normalize_doi(value: str) -> str:
    if any(unicodedata.category(char).startswith("C") for char in value):
        raise DownloadError("invalid_input", "DOI contains control characters")
    doi = value.strip()
    doi = re.sub(
        r"(?i)^(?:doi:\s*|https?://(?:www\.)?doi\.org/|(?:www\.)?doi\.org/)",
        "",
        doi,
    ).strip()
    decoded = urllib.parse.unquote(doi)
    if (
        len(doi) > 2048
        or not re.fullmatch(r"10\.\d{4,9}/\S+", doi)
        or any(char.isspace() or unicodedata.category(char).startswith("C") for char in decoded)
        or any(char in doi for char in "\\?#")
        or "\\" in decoded
        or any(part in {".", ".."} for part in decoded.split("/"))
    ):
        raise DownloadError("invalid_input", "enter a valid DOI")
    return doi


def target_for(doi: str, output_dir: str | Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", doi).strip("-")[:80]
    digest = hashlib.sha256(doi.encode()).hexdigest()[:12]
    return Path(output_dir) / f"{slug}-{digest}.pdf"


def chrome_cookie_loader():
    from browser_cookie3 import chrome

    return chrome


def load_chrome_cookies(cookie_domain: str, *, cookie_loader=None):
    if cookie_loader is None:
        cookie_loader = chrome_cookie_loader()
    return cookie_loader(domain_name=cookie_domain)


def valid_cookie_domain(value: str) -> bool:
    return re.fullmatch(r"\.?[A-Za-z0-9][A-Za-z0-9.-]*", value) is not None


def download_pdf(
    doi: str,
    library_id: str,
    cookie_domain: str,
    output_dir: str | Path = ".",
    *,
    browser: str = "chrome",
    profile: str | None = None,
    cookie_loader=None,
    opener_factory=None,
) -> tuple[Path, str]:
    doi = normalize_doi(doi)
    library_id = library_id.strip()
    cookie_domain = cookie_domain.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]+", library_id):
        raise DownloadError("invalid_input", "library ID must use letters, numbers, _ or -")
    if not valid_cookie_domain(cookie_domain):
        raise DownloadError("invalid_input", "enter a valid cookie domain")

    directory = Path(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = target_for(doi, directory)
    if target.exists():
        return target, "skipped_existing"

    try:
        cookies, _ = load_browser_cookies(
            cookie_domain, browser=browser, profile=profile, cookie_loader=cookie_loader,
        )
    except BrowserError as error:
        raise DownloadError(error.category, error.message) from None
    except Exception:
        raise DownloadError("cookie_error", "could not load browser cookies") from None

    if opener_factory is None:
        opener_factory = urllib.request.build_opener
    temp_path = None
    try:
        opener = opener_factory(urllib.request.HTTPCookieProcessor(cookies))
        token_request = urllib.request.Request(
            "https://api.thirdiron.com/v2/api-tokens",
            data=json.dumps({"libraryId": library_id, "returnPreproxy": True, "client": "bzweb"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(token_request, timeout=60) as response:
                token = json.load(response)["api-tokens"][0]["id"]
            if not isinstance(token, str) or not token or any(ord(char) < 32 for char in token):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise DownloadError("authentication_error", "browser session was not authorized") from None

        article_request = urllib.request.Request(
            f"https://api.thirdiron.com/v2/articles/{urllib.parse.quote('doi:' + doi, safe='')}?include=issue,journal",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with opener.open(article_request, timeout=60) as response:
                pdf_url = json.load(response)["data"]["attributes"].get("fullTextFile")
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DownloadError("non_pdf", "service did not provide a PDF URL") from None
        try:
            if not isinstance(pdf_url, str):
                raise ValueError
            parsed_url = urllib.parse.urlsplit(pdf_url)
            if parsed_url.scheme != "https" or not parsed_url.hostname or parsed_url.username or parsed_url.password or any(ord(char) < 32 for char in pdf_url):
                raise ValueError
            pdf_request = urllib.request.Request(pdf_url, headers={"Authorization": f"Bearer {token}"})
        except (TypeError, ValueError):
            raise DownloadError("non_pdf", "service did not provide a PDF URL")

        with opener.open(pdf_request, timeout=60) as response:
            first = response.read(1024)
            content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if not first.startswith(b"%PDF-") and content_type != "application/pdf":
                raise DownloadError("non_pdf", "server returned a non-PDF response")
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".lkfetch-", suffix=".part", delete=False) as temp:
                temp_path = Path(temp.name)
                temp.write(first)
                shutil.copyfileobj(response, temp)

        try:
            os.link(temp_path, target)
        except FileExistsError:
            return target, "skipped_existing"
        except OSError:
            raise DownloadError("file_error", "could not save PDF") from None
        try:
            if not target.samefile(temp_path):
                raise DownloadError("file_error", "target changed during download")
            temp_path.unlink()
        except DownloadError:
            raise
        except OSError:
            raise DownloadError("file_error", "could not save PDF") from None
        temp_path = None
        return target, "downloaded"
    except urllib.error.HTTPError as error:
        error.close()
        if error.code in (401, 403):
            raise DownloadError("authentication_error", "browser session was not authorized") from None
        if error.code == 429:
            raise DownloadError("rate_limited", "server asked to slow down") from None
        raise DownloadError("http_error", "server could not provide the PDF") from None
    except OSError:
        raise DownloadError("network_error", "could not reach the PDF service") from None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

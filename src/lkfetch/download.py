"""Download one PDF using browser cookies kept in memory."""

import hashlib
from html.parser import HTMLParser
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


def _is_resolver_host(host: str) -> bool:
    host = host.rstrip(".").lower()
    return host == "resolve.thirdiron.com" or host == "libkey.io" or host.endswith(".libkey.io")


def _libkey_resolver_url(url: str, token: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    if not _is_resolver_host(parsed.hostname or ""):
        return url
    query = [(key, value) for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True) if key != "access_token"]
    query.append(("access_token", token))
    return urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))


def _proxy_url(url: str, cookie_domain: str) -> str:
    parsed = urllib.parse.urlsplit(url)
    host = parsed.hostname
    if not host:
        return url
    proxy_host = f"{host.replace('.', '-')}.{cookie_domain.lstrip('.')}"
    if parsed.port:
        proxy_host = f"{proxy_host}:{parsed.port}"
    return urllib.parse.urlunsplit(parsed._replace(netloc=proxy_host))


def _valid_https_url(value: object) -> bool:
    if not isinstance(value, str):
        return False
    parsed = urllib.parse.urlsplit(value)
    return (
        parsed.scheme == "https"
        and bool(parsed.hostname)
        and not parsed.username
        and not parsed.password
        and not any(ord(char) < 32 for char in value)
    )


class _PDFLinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        for name, value in attrs:
            if name.lower() == "href" and value:
                self.links.append(value)


def _pdf_link_from_html(body: bytes, base_url: str, cookie_domain: str) -> str | None:
    parser = _PDFLinkParser()
    parser.feed(body.decode("utf-8", "replace"))
    candidates = []
    for href in parser.links:
        candidate = urllib.parse.urljoin(base_url, href)
        if not _valid_https_url(candidate):
            continue
        parsed = urllib.parse.urlsplit(candidate)
        if ".pdf" not in parsed.path.lower():
            continue
        if not _is_proxy_host(parsed.hostname or "", cookie_domain):
            candidate = _proxy_url(candidate, cookie_domain)
        priority = 0 if ("full.pdf" in parsed.path.lower() or "full-text.pdf" in parsed.path.lower()) else 1
        candidates.append((priority, candidate))
    if not candidates:
        return None
    return min(candidates)[1]


def _is_proxy_login_url(url: str, cookie_domain: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == f"login.{cookie_domain.lstrip('.').lower()}"
        and parsed.path == "/login"
    )


def _save_pdf_response(response, directory: Path, cookie_domain: str) -> tuple[Path | None, str | None]:
    first = response.read(1024)
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    if not first.startswith(b"%PDF-") and content_type != "application/pdf":
        if content_type == "text/html" and _is_proxy_login_url(response.geturl(), cookie_domain):
            raise DownloadError("proxy_login", "proxy login required; sign in through your library and retry")
        if content_type != "text/html":
            raise DownloadError("non_pdf", "server returned a non-PDF response")
        body = first + response.read(max(0, 2_000_000 - len(first)))
        return None, _pdf_link_from_html(body, response.geturl(), cookie_domain)
    with tempfile.NamedTemporaryFile(dir=directory, prefix=".lkfetch-", suffix=".part", delete=False) as temp:
        temp_path = Path(temp.name)
        temp.write(first)
        shutil.copyfileobj(response, temp)
    return temp_path, None


def _is_proxy_host(host: str, cookie_domain: str) -> bool:
    host = host.lstrip(".").lower()
    domain = cookie_domain.lstrip(".").lower()
    return host == domain or host.endswith("." + domain)


class _ProxyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, cookie_domain: str):
        self.cookie_domain = cookie_domain.lstrip(".").lower()

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        source_host = urllib.parse.urlsplit(request.full_url).hostname or ""
        target = urllib.parse.urlsplit(newurl)
        if (
            _is_resolver_host(source_host)
            and target.scheme == "https"
            and target.hostname
        ):
            login_host = f"login.{self.cookie_domain}"
            if target.hostname.lower() == login_host and target.path == "/login":
                values = urllib.parse.parse_qs(target.query).get("url", [])
                if len(values) == 1:
                    target_url = urllib.parse.urlsplit(values[0])
                    if (
                        target_url.scheme == "https"
                        and target_url.hostname
                        and not target_url.username
                        and not target_url.password
                    ):
                        newurl = (
                            values[0]
                            if _is_proxy_host(target_url.hostname, self.cookie_domain)
                            else _proxy_url(values[0], self.cookie_domain)
                        )
            elif not _is_proxy_host(target.hostname, self.cookie_domain):
                newurl = _proxy_url(newurl, self.cookie_domain)
        return super().redirect_request(request, fp, code, msg, headers, newurl)


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

    temp_path = None
    try:
        cookie_processor = urllib.request.HTTPCookieProcessor(cookies)
        if opener_factory is None:
            opener = urllib.request.build_opener(cookie_processor, _ProxyRedirectHandler(cookie_domain))
        else:
            opener = opener_factory(cookie_processor)
        token_request = urllib.request.Request(
            "https://api.thirdiron.com/v2/api-tokens",
            data=json.dumps({"libraryId": library_id, "returnPreproxy": True, "client": "bzweb"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener.open(token_request, timeout=60) as response:
                token = json.load(response)["api-tokens"][0]["id"]
            if not isinstance(token, str) or not token or any(not 0x21 <= ord(char) <= 0x7E for char in token):
                raise ValueError
        except (ValueError, KeyError, IndexError, TypeError):
            raise DownloadError("api_token_error", "service did not provide a usable API token") from None

        article_request = urllib.request.Request(
            f"https://api.thirdiron.com/v2/articles/{urllib.parse.quote('doi:' + doi, safe='')}?include=issue,journal",
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            with opener.open(article_request, timeout=60) as response:
                attributes = json.load(response)["data"]["attributes"]
                pdf_url = attributes.get("fullTextFile")
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
            raise DownloadError("article_not_found", "article was not found") from None
        except (ValueError, KeyError, TypeError, AttributeError):
            raise DownloadError("article_error", "service did not provide a valid article PDF URL") from None
        try:
            if not isinstance(pdf_url, str):
                raise ValueError
            if not _valid_https_url(pdf_url):
                raise ValueError
            pdf_request = urllib.request.Request(_libkey_resolver_url(pdf_url, token))
        except (TypeError, ValueError):
            raise DownloadError("article_error", "service did not provide a valid article PDF URL") from None

        candidates = [pdf_request.full_url]
        permalink = attributes.get("permalink")
        if _valid_https_url(permalink):
            permalink_host = urllib.parse.urlsplit(permalink).hostname or ""
            candidates.append(permalink if _is_proxy_host(permalink_host, cookie_domain) else _proxy_url(permalink, cookie_domain))
        for _ in range(3):
            if not candidates:
                break
            candidate = candidates.pop(0)
            with opener.open(urllib.request.Request(candidate), timeout=60) as response:
                temp_path, linked_pdf = _save_pdf_response(response, directory, cookie_domain)
            if temp_path is not None:
                break
            if linked_pdf is not None:
                candidates.insert(0, linked_pdf)
        if temp_path is None:
            raise DownloadError("non_pdf", "server returned a non-PDF response")

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

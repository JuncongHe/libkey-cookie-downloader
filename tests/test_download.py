import io
import os
import sys
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lkfetch.cli import main
from lkfetch.download import DownloadError, download_pdf, normalize_doi, target_for


class FakeResponse(io.BytesIO):
    def __init__(self, body, content_type="application/octet-stream"):
        super().__init__(body)
        self.headers = {"Content-Type": content_type}


class DownloadTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.cookies = CookieJar()
        self.calls = []

    def loader(self, **kwargs):
        self.calls.append(("cookies", kwargs))
        return self.cookies

    def factory(self, handler):
        self.assertIs(handler.cookiejar, self.cookies)
        return self

    def open(self, url, timeout):
        self.calls.append(("open", url, timeout))
        return FakeResponse(b"%PDF-1.7\nexample")

    def fetch(self, doi="10.1234/example", library_id="lib_1"):
        return download_pdf(
            doi, library_id, "example.invalid", self.directory,
            cookie_loader=self.loader, opener_factory=self.factory,
        )

    def test_url_cookie_scope_and_atomic_pdf(self):
        link = os.link
        def check_link(source, destination):
            self.assertEqual(Path(source).read_bytes(), b"%PDF-1.7\nexample")
            self.assertFalse(Path(destination).exists())
            return link(source, destination)
        with mock.patch("lkfetch.download.os.link", side_effect=check_link) as linked:
            target, status = self.fetch("  doi:10.1234/Example  ")
        self.assertEqual(status, "downloaded")
        self.assertEqual(target.read_bytes(), b"%PDF-1.7\nexample")
        self.assertEqual(target.parent, self.directory)
        self.assertEqual(self.calls[0], ("cookies", {"domain_name": "example.invalid"}))
        self.assertEqual(
            self.calls[1],
            ("open", "https://libkey.io/libraries/lib_1/pdfexpress/openurl?doi=10.1234%2FExample&sid=lkfetch", 60),
        )
        linked.assert_called_once()
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_content_type_can_identify_pdf(self):
        self.open = lambda url, timeout: FakeResponse(b"binary", "application/pdf; charset=binary")
        target, status = self.fetch()
        self.assertEqual(status, "downloaded")
        self.assertEqual(target.read_bytes(), b"binary")

    def test_html_rejected_without_target_or_temp(self):
        self.open = lambda url, timeout: FakeResponse(b"<html>sign in</html>", "text/html")
        with self.assertRaises(DownloadError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.category, "non_pdf")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_failed_link_leaves_no_target_or_temp(self):
        with mock.patch("lkfetch.download.os.link", side_effect=OSError("disk full")):
            with self.assertRaises(DownloadError) as caught:
                self.fetch()
        self.assertEqual(caught.exception.category, "file_error")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_target_replaced_after_link_is_not_overwritten(self):
        link = os.link

        def competing_writer(source, destination):
            link(source, destination)
            Path(destination).unlink()
            Path(destination).write_bytes(b"other process")

        with mock.patch("lkfetch.download.os.link", side_effect=competing_writer):
            with self.assertRaises(DownloadError):
                self.fetch()
        target = target_for("10.1234/example", self.directory)
        self.assertEqual(target.read_bytes(), b"other process")
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_existing_and_racing_target_are_never_overwritten(self):
        target = target_for("10.1234/example", self.directory)
        target.write_bytes(b"existing")
        self.assertEqual(self.fetch(), (target, "skipped_existing"))
        self.assertEqual(self.calls, [])
        target.unlink()

        def race(url, timeout):
            target.write_bytes(b"other process")
            return FakeResponse(b"%PDF-1.7\nnew")

        self.open = race
        self.assertEqual(self.fetch(), (target, "skipped_existing"))
        self.assertEqual(target.read_bytes(), b"other process")
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_validation_rejects_path_injection(self):
        self.assertEqual(normalize_doi(" https://doi.org/10.1234/abc "), "10.1234/abc")
        for doi in ("", "doi:", "10.1234/../secret", "10.1234/%2e%2e/secret", "10.1234/a\\b", "10.1234/a\nb"):
            with self.subTest(doi=doi), self.assertRaises(DownloadError):
                normalize_doi(doi)
        with self.assertRaises(DownloadError):
            self.fetch(library_id="../escape")
        with self.assertRaises(DownloadError):
            download_pdf("10.1234/example", "lib_1", ".", self.directory, cookie_loader=self.loader)

    def test_http_errors_are_sanitized(self):
        for code, category in ((401, "authentication_error"), (403, "authentication_error"), (429, "rate_limited")):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://secret.example/sso", code, "secret", {}, None)

                def fail(url, timeout):
                    raise error

                self.open = fail
                with self.assertRaises(DownloadError) as caught:
                    self.fetch()
                error.close()
                self.assertEqual(caught.exception.category, category)
                self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_network_error_is_sanitized(self):
        def fail(url, timeout):
            raise OSError("secret endpoint")

        self.open = fail
        with self.assertRaises(DownloadError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.category, "network_error")
        self.assertNotIn("secret", str(caught.exception))
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_cli_missing_config_and_precedence(self):
        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stderr(stderr):
            self.assertEqual(main(["download", "10.1234/example"]), 2)
        self.assertIn("LKFETCH_LIBRARY_ID", stderr.getvalue())

        stderr = io.StringIO()
        with mock.patch.dict(os.environ, {"LKFETCH_LIBRARY_ID": "lib_1"}, clear=True), redirect_stderr(stderr):
            self.assertEqual(main(["download", "10.1234/example"]), 2)
        self.assertIn("LKFETCH_COOKIE_DOMAIN", stderr.getvalue())

        stdout = io.StringIO()
        with mock.patch.dict(os.environ, {"LKFETCH_LIBRARY_ID": "from_env", "LKFETCH_COOKIE_DOMAIN": "env.invalid"}, clear=True), mock.patch("lkfetch.cli.download_pdf", return_value=(self.directory / "test.pdf", "downloaded")) as mocked, redirect_stdout(stdout):
            self.assertEqual(main(["download", "10.1234/example", "--library-id", "from_cli", "--cookie-domain", "cli.invalid"]), 0)
        mocked.assert_called_once_with("10.1234/example", "from_cli", "cli.invalid", ".")
        self.assertIn("Status: downloaded", stdout.getvalue())

        stderr = io.StringIO()
        with mock.patch("lkfetch.cli.download_pdf", side_effect=ValueError("https://secret.example/sso")), redirect_stderr(stderr):
            self.assertEqual(main(["download", "10.1234/example", "--library-id", "lib_1", "--cookie-domain", "example.invalid"]), 1)
        self.assertIn("download_error", stderr.getvalue())
        self.assertNotIn("secret.example", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

import io
import os
import sys
import tempfile
import types
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

    def test_doctor_ready_scopes_chrome_and_redacts_values(self):
        chrome = mock.Mock(return_value=["SECRET_COOKIE=SECRET_VALUE"])
        browser_cookie3 = types.ModuleType("browser_cookie3")
        browser_cookie3.chrome = chrome
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"LKFETCH_LIBRARY_ID": "env-secret", "LKFETCH_COOKIE_DOMAIN": "env-secret.invalid"}, clear=True),
            mock.patch.dict(sys.modules, {"browser_cookie3": browser_cookie3}),
            mock.patch("lkfetch.cli.download_pdf") as fetched,
            mock.patch("lkfetch.download.urllib.request.build_opener") as network,
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            self.assertEqual(main(["doctor", "--cookie-domain", "cli-secret.invalid", "--library-id", "cli-secret"]), 0)
        chrome.assert_called_once_with(domain_name="cli-secret.invalid")
        fetched.assert_not_called()
        network.assert_not_called()
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().splitlines(), [
            "Dependency: ready", "library_id: configured", "cookie_domain: configured", "Chrome cookie reader: ready",
        ])

    def test_doctor_empty_jar_and_loader_error(self):
        browser_cookie3 = types.ModuleType("browser_cookie3")
        for cookies, expected in ((CookieJar(), "no matching cookies"), (OSError("/secret/profile Cookie=PRIVATE https://secret.example"), "error")):
            with self.subTest(expected=expected):
                chrome = mock.Mock(side_effect=cookies) if isinstance(cookies, Exception) else mock.Mock(return_value=cookies)
                browser_cookie3.chrome = chrome
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch.dict(sys.modules, {"browser_cookie3": browser_cookie3}),
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(["doctor", "--library-id", "lib", "--cookie-domain", "example.invalid"]), 1)
                chrome.assert_called_once_with(domain_name="example.invalid")
                self.assertIn(f"Chrome cookie reader: {expected}", stdout.getvalue())
                self.assertNotIn("secret", stdout.getvalue())
                self.assertNotIn("PRIVATE", stdout.getvalue())
                self.assertNotIn("/", stdout.getvalue())

    def test_doctor_missing_config_and_dependency(self):
        browser_cookie3 = types.ModuleType("browser_cookie3")
        browser_cookie3.chrome = mock.Mock(return_value=[object()])
        for args, environment, missing in (
            (["doctor", "--cookie-domain", "example.invalid"], {}, "library_id"),
            (["doctor", "--library-id", "lib"], {}, "cookie_domain"),
            (["doctor", "--library-id", "lib", "--cookie-domain", " "], {"LKFETCH_COOKIE_DOMAIN": "env.invalid"}, "cookie_domain"),
        ):
            with self.subTest(missing=missing, args=args):
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch.dict(sys.modules, {"browser_cookie3": browser_cookie3}),
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(args), 2)
                self.assertIn(f"{missing}: missing", stdout.getvalue())
                self.assertIn("Chrome cookie reader: error", stdout.getvalue())
                browser_cookie3.chrome.assert_not_called()

        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.dict(sys.modules, {"browser_cookie3": None}),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["doctor", "--library-id", "lib", "--cookie-domain", "example.invalid"]), 1)
        self.assertIn("Dependency: missing", stdout.getvalue())
        self.assertIn("Chrome cookie reader: error", stdout.getvalue())

        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch.dict(sys.modules, {"browser_cookie3": browser_cookie3}),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["doctor", "--library-id", "lib", "--cookie-domain", "%"]), 1)
        browser_cookie3.chrome.assert_not_called()
        self.assertIn("cookie_domain: configured", stdout.getvalue())
        self.assertIn("Chrome cookie reader: error", stdout.getvalue())

    def test_batch_reads_utf8_in_order_without_echoing_invalid_lines(self):
        source = self.directory / "dois.txt"
        source.write_text(
            "  \n\t # comment\n doi:10.1234/first \n"
            "https://secret.example/sso?token=PRIVATE\n"
            "10.1234/trailing\t\n10.1234/second\n",
            encoding="utf-8",
        )
        output = self.directory / "pdfs"
        first = target_for("10.1234/first", output)
        second = target_for("10.1234/second", output)
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"LKFETCH_LIBRARY_ID": "env", "LKFETCH_COOKIE_DOMAIN": "env.invalid"}, clear=True),
            mock.patch("lkfetch.cli.download_pdf", side_effect=[(first, "downloaded"), (second, "skipped_existing")]) as fetched,
            mock.patch("lkfetch.cli.time.sleep") as sleep,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["batch", str(source), "--library-id", "cli", "--cookie-domain", "cli.invalid", "--output-dir", str(output), "--delay", "0"]), 1)
        self.assertEqual(fetched.call_args_list, [
            mock.call("10.1234/first", "cli", "cli.invalid", str(output)),
            mock.call("10.1234/second", "cli", "cli.invalid", str(output)),
        ])
        sleep.assert_called_once_with(0.0)
        self.assertLess(stdout.getvalue().index("DOI: 10.1234/first"), stdout.getvalue().index("DOI: 10.1234/second"))
        self.assertEqual(stdout.getvalue().count("failure (invalid_input)"), 2)
        self.assertNotIn("secret.example", stdout.getvalue())
        self.assertNotIn("PRIVATE", stdout.getvalue())
        self.assertIn("Status: skipped_existing", stdout.getvalue())
        self.assertIn("Summary: downloaded=1 skipped_existing=1 failed=2", stdout.getvalue())

    def test_batch_continues_ordinary_errors_then_stops_on_auth_or_rate_limit(self):
        source = self.directory / "dois.txt"
        source.write_text("".join(f"10.1234/item{i}\n" for i in range(5)), encoding="utf-8")
        for stop_category in ("rate_limited", "authentication_error"):
            with self.subTest(stop_category=stop_category):
                stdout = io.StringIO()
                errors = [DownloadError(category, "https://secret.example/sso") for category in
                          ("non_pdf", "http_error", "network_error", stop_category)]
                with (
                    mock.patch.dict(os.environ, {"LKFETCH_LIBRARY_ID": "lib", "LKFETCH_COOKIE_DOMAIN": "example.invalid"}, clear=True),
                    mock.patch("lkfetch.cli.download_pdf", side_effect=errors) as fetched,
                    mock.patch("lkfetch.cli.time.sleep") as sleep,
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(["batch", str(source)]), 1)
                self.assertEqual(fetched.call_count, 4)
                self.assertEqual(sleep.call_args_list, [mock.call(3.0)] * 3)
                self.assertNotIn("item4", stdout.getvalue())
                self.assertNotIn("secret.example", stdout.getvalue())
                self.assertIn(f"failure ({stop_category})", stdout.getvalue())
                self.assertIn(f"Summary: downloaded=0 skipped_existing=0 failed=4 stopped={stop_category}", stdout.getvalue())

    def test_batch_missing_config_and_invalid_delay(self):
        source = self.directory / "dois.txt"
        source.write_text("10.1234/example\n", encoding="utf-8")
        for environment, missing in (({}, "LKFETCH_LIBRARY_ID"), ({"LKFETCH_LIBRARY_ID": "lib"}, "LKFETCH_COOKIE_DOMAIN")):
            with self.subTest(missing=missing):
                stderr = io.StringIO()
                with mock.patch.dict(os.environ, environment, clear=True), mock.patch("lkfetch.cli.download_pdf") as fetched, redirect_stderr(stderr):
                    self.assertEqual(main(["batch", str(source)]), 2)
                self.assertIn(missing, stderr.getvalue())
                fetched.assert_not_called()
        for delay in ("-1", "nan", "inf"):
            with self.subTest(delay=delay), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                main(["batch", str(source), "--delay", delay])
            self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()

import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import types
import unittest
import urllib.error
from contextlib import closing, redirect_stderr, redirect_stdout
from http.cookiejar import CookieJar
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from lkfetch.cli import main
from lkfetch import browser as browser_module
from lkfetch.browser import BrowserError
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
        self.pdf_url = "https://files.example.invalid/article.pdf"
        self.token_response = {"api-tokens": [{"id": "test-token"}]}
        self.article_response = {"data": {"attributes": {"fullTextFile": self.pdf_url}}}
        self.pdf_response = lambda request, timeout: FakeResponse(b"%PDF-1.7\nexample")

    def loader(self, **kwargs):
        self.calls.append(("cookies", kwargs))
        return self.cookies

    def factory(self, handler):
        self.assertIs(handler.cookiejar, self.cookies)
        return self

    def open(self, request, timeout):
        self.calls.append(("open", request, timeout))
        if request.full_url.endswith("/api-tokens"):
            return FakeResponse(json.dumps(self.token_response).encode(), "application/json")
        if "/v2/articles/" in request.full_url:
            return FakeResponse(json.dumps(self.article_response).encode(), "application/json")
        return self.pdf_response(request, timeout)

    def fetch(self, doi="10.1234/example", library_id="lib_1"):
        return download_pdf(
            doi, library_id, "example.invalid", self.directory,
            cookie_loader=self.loader, opener_factory=self.factory,
        )

    def test_token_article_url_cookie_scope_and_atomic_pdf(self):
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
        token_request, article_request, pdf_request = [call[1] for call in self.calls[1:]]
        self.assertEqual([call[2] for call in self.calls[1:]], [60, 60, 60])
        self.assertEqual(token_request.full_url, "https://api.thirdiron.com/v2/api-tokens")
        self.assertEqual(token_request.get_method(), "POST")
        self.assertEqual(json.loads(token_request.data), {"libraryId": "lib_1", "returnPreproxy": True, "client": "bzweb"})
        self.assertEqual(token_request.get_header("Content-type"), "application/json")
        self.assertEqual(article_request.full_url, "https://api.thirdiron.com/v2/articles/doi%3A10.1234%2FExample?include=issue,journal")
        self.assertEqual(article_request.get_header("Authorization"), "Bearer test-token")
        self.assertEqual(pdf_request.full_url, self.pdf_url)
        self.assertEqual(pdf_request.get_header("Authorization"), "Bearer test-token")
        linked.assert_called_once()
        self.assertEqual(list(self.directory.iterdir()), [target])

    def test_content_type_can_identify_pdf(self):
        self.pdf_response = lambda request, timeout: FakeResponse(b"binary", "application/pdf; charset=binary")
        target, status = self.fetch()
        self.assertEqual(status, "downloaded")
        self.assertEqual(target.read_bytes(), b"binary")

    def test_html_rejected_without_target_or_temp(self):
        self.pdf_response = lambda request, timeout: FakeResponse(b"<html>sign in</html>", "text/html")
        with self.assertRaises(DownloadError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.category, "non_pdf")
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_missing_or_invalid_full_text_file_is_non_pdf(self):
        for value in (None, 42, "http://files.example.invalid/article.pdf", "https://", "https://[bad", "https://user:pass@files.example.invalid/article.pdf"):
            with self.subTest(value=value):
                self.article_response = {"data": {"attributes": {"fullTextFile": value}}}
                with self.assertRaises(DownloadError) as caught:
                    self.fetch()
                self.assertEqual(caught.exception.category, "non_pdf")
                self.assertEqual(len(self.calls), 3)
                self.assertEqual(list(self.directory.iterdir()), [])
                self.calls.clear()

    def test_missing_token_is_authentication_error(self):
        self.token_response = {"api-tokens": []}
        with self.assertRaises(DownloadError) as caught:
            self.fetch()
        self.assertEqual(caught.exception.category, "authentication_error")
        self.assertEqual(len(self.calls), 2)

    def test_malformed_utf8_api_responses_are_sanitized(self):
        for stage, category, message, request_count in (
            ("api-tokens", "authentication_error", "browser session was not authorized", 1),
            ("/v2/articles/", "non_pdf", "service did not provide a PDF URL", 2),
        ):
            with self.subTest(stage=stage):
                requested = []

                def open_response(request, timeout):
                    requested.append(request.full_url)
                    if stage in request.full_url:
                        return FakeResponse(b"\xffSECRET_BODY", "application/json")
                    return DownloadTest.open(self, request, timeout)

                self.open = open_response
                with self.assertRaises(DownloadError) as caught:
                    self.fetch()
                self.assertEqual(caught.exception.category, category)
                self.assertEqual(str(caught.exception), message)
                self.assertNotIn("SECRET_BODY", str(caught.exception))
                self.assertEqual(len(requested), request_count)
                self.assertEqual(list(self.directory.iterdir()), [])
                self.calls.clear()

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

        def race(request, timeout):
            target.write_bytes(b"other process")
            return FakeResponse(b"%PDF-1.7\nnew")

        self.pdf_response = race
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
        for stage in ("api-tokens", "articles", "article.pdf"):
            for code, category in ((401, "authentication_error"), (403, "authentication_error"), (429, "rate_limited"), (500, "http_error")):
                with self.subTest(stage=stage, code=code):
                    original_open = DownloadTest.open.__get__(self)

                    def fail(request, timeout):
                        if stage in request.full_url:
                            raise urllib.error.HTTPError("https://secret.example/sso?token=PRIVATE", code, "secret", {}, None)
                        return original_open(request, timeout)

                    self.open = fail
                    with self.assertRaises(DownloadError) as caught:
                        self.fetch()
                    self.assertEqual(caught.exception.category, category)
                    self.assertNotIn("secret", str(caught.exception))
                    self.assertNotIn("PRIVATE", str(caught.exception))
                    self.calls.clear()
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
        mocked.assert_called_once_with("10.1234/example", "from_cli", "cli.invalid", ".", browser="chrome", profile=None)
        self.assertIn("Status: downloaded", stdout.getvalue())

        stderr = io.StringIO()
        with mock.patch("lkfetch.cli.download_pdf", side_effect=ValueError("https://secret.example/sso")), redirect_stderr(stderr):
            self.assertEqual(main(["download", "10.1234/example", "--library-id", "lib_1", "--cookie-domain", "example.invalid"]), 1)
        self.assertIn("download_error", stderr.getvalue())
        self.assertNotIn("secret.example", stderr.getvalue())

    def test_browser_environment_and_cli_precedence(self):
        base = {"LKFETCH_LIBRARY_ID": "lib", "LKFETCH_COOKIE_DOMAIN": "example.invalid"}
        for environment, options, browser, profile in (
            ({**base, "LKFETCH_BROWSER": "dia", "LKFETCH_BROWSER_PROFILE": "Profile 2"}, [], "dia", "Profile 2"),
            ({**base, "LKFETCH_BROWSER": "chrome", "LKFETCH_BROWSER_PROFILE": "Default"}, ["--browser", "dia", "--profile", "Profile 2"], "dia", "Profile 2"),
        ):
            with self.subTest(options=options):
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch("lkfetch.cli.download_pdf", return_value=(self.directory / "test.pdf", "downloaded")) as fetched,
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(main(["download", "10.1234/example", *options]), 0)
                fetched.assert_called_once_with("10.1234/example", "lib", "example.invalid", ".", browser=browser, profile=profile)

    def test_download_error_output_redacts_browser_details(self):
        for category, expected in (
            ("cookie_denied", "Keychain access denied; allow access and retry"),
            ("cookie_config", "select a Dia profile with --profile"),
        ):
            with self.subTest(category=category):
                stderr = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {"LKFETCH_LIBRARY_ID": "lib", "LKFETCH_COOKIE_DOMAIN": "example.invalid"}, clear=True),
                    mock.patch("lkfetch.cli.download_pdf", side_effect=DownloadError(category, "/private/secret Cookie=PRIVATE")),
                    redirect_stderr(stderr),
                ):
                    self.assertEqual(main(["download", "10.1234/example", "--browser", "dia"]), 1)
                self.assertIn(f"failure ({category}:", stderr.getvalue())
                self.assertIn(expected, stderr.getvalue())
                self.assertNotIn("/private", stderr.getvalue())
                self.assertNotIn("PRIVATE", stderr.getvalue())

    def test_doctor_ready_scopes_chrome_and_redacts_values(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with (
            mock.patch.dict(os.environ, {"LKFETCH_LIBRARY_ID": "env-secret", "LKFETCH_COOKIE_DOMAIN": "env-secret.invalid"}, clear=True),
            mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
            mock.patch("lkfetch.cli.load_browser_cookies", return_value=(["SECRET_COOKIE=SECRET_VALUE"], "Default")) as loaded,
            mock.patch("lkfetch.cli.download_pdf") as fetched,
            mock.patch("lkfetch.download.urllib.request.build_opener") as network,
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            self.assertEqual(main(["doctor", "--cookie-domain", "cli-secret.invalid", "--library-id", "cli-secret"]), 0)
        loaded.assert_called_once_with("cli-secret.invalid", browser="chrome", profile=None)
        fetched.assert_not_called()
        network.assert_not_called()
        self.assertEqual(stderr.getvalue(), "")
        self.assertEqual(stdout.getvalue().splitlines(), [
            "Dependency: ready", "library_id: configured", "cookie_domain: configured",
            "Browser: configured=chrome selected=chrome", "Profile: configured=default selected=Default",
            "Chrome cookie reader: ready",
        ])

    def test_doctor_empty_jar_and_loader_error(self):
        for cookies, expected in ((CookieJar(), "no matching cookies"), (OSError("/secret/profile Cookie=PRIVATE https://secret.example"), "error")):
            with self.subTest(expected=expected):
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
                    mock.patch("lkfetch.cli.load_browser_cookies", side_effect=cookies) if isinstance(cookies, Exception) else mock.patch("lkfetch.cli.load_browser_cookies", return_value=(cookies, "Default")) as loaded,
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(["doctor", "--library-id", "lib", "--cookie-domain", "example.invalid"]), 1)
                loaded.assert_called_once_with("example.invalid", browser="chrome", profile=None)
                self.assertIn(f"Chrome cookie reader: {expected}", stdout.getvalue())
                self.assertNotIn("secret", stdout.getvalue())
                self.assertNotIn("PRIVATE", stdout.getvalue())
                self.assertNotIn("/", stdout.getvalue())

    def test_doctor_dia_reports_only_profile_names_without_network(self):
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {
                "LKFETCH_LIBRARY_ID": "lib", "LKFETCH_COOKIE_DOMAIN": "example.invalid",
                "LKFETCH_BROWSER": "chrome", "LKFETCH_BROWSER_PROFILE": "/private/env-secret",
            }, clear=True),
            mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
            mock.patch("lkfetch.cli.os.path.isfile", return_value=True),
            mock.patch("lkfetch.cli.os.access", return_value=True) as security,
            mock.patch("lkfetch.cli.load_browser_cookies", return_value=([object()], "Profile 2")) as loaded,
            mock.patch("lkfetch.cli.download_pdf") as fetched,
            mock.patch("lkfetch.download.urllib.request.build_opener") as network,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["doctor", "--browser", "dia", "--profile", "Profile 2"]), 0)
        security.assert_called_once_with("/usr/bin/security", os.X_OK)
        loaded.assert_called_once_with("example.invalid", browser="dia", profile="Profile 2")
        fetched.assert_not_called()
        network.assert_not_called()
        self.assertIn("Browser: configured=dia selected=dia", stdout.getvalue())
        self.assertIn("Profile: configured=Profile 2 selected=Profile 2", stdout.getvalue())
        self.assertIn("Dia cookie reader: ready", stdout.getvalue())
        self.assertNotIn("/private", stdout.getvalue())
        self.assertNotIn("profile-secret", stdout.getvalue())

    def test_doctor_dia_invalid_profile_values_are_not_echoed(self):
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {
                "LKFETCH_LIBRARY_ID": "lib", "LKFETCH_COOKIE_DOMAIN": "example.invalid",
                "LKFETCH_BROWSER": "dia", "LKFETCH_BROWSER_PROFILE": "/private/secret/Profile 2",
            }, clear=True),
            mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
            mock.patch("lkfetch.cli.os.path.isfile", return_value=True),
            mock.patch("lkfetch.cli.os.access", return_value=True),
            mock.patch("lkfetch.cli.load_browser_cookies", return_value=([object()], "/private/secret/Profile 2")),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["doctor"]), 0)
        self.assertIn("Profile: configured=invalid selected=invalid", stdout.getvalue())
        self.assertNotIn("Profile 2", stdout.getvalue())
        self.assertNotIn("/private", stdout.getvalue())

    def test_doctor_dia_requires_package_and_exact_security_binary(self):
        for loader, exists, executable in ((ImportError("missing"), True, True), (object(), False, True), (object(), True, False)):
            with self.subTest(loader=type(loader).__name__, exists=exists, executable=executable):
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("lkfetch.cli.chrome_cookie_loader", side_effect=loader) if isinstance(loader, Exception) else mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=loader),
                    mock.patch("lkfetch.cli.os.path.isfile", return_value=exists),
                    mock.patch("lkfetch.cli.os.access", return_value=executable),
                    mock.patch("lkfetch.cli.load_browser_cookies") as loaded,
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(["doctor", "--browser", "dia", "--library-id", "lib", "--cookie-domain", "example.invalid"]), 1)
                self.assertIn("Dependency: missing", stdout.getvalue())
                loaded.assert_not_called()

    def test_doctor_dia_reader_failure_is_finite_and_redacted(self):
        for failure in (TimeoutError("/private/secret Cookie=PRIVATE"), PermissionError("/private/secret Cookie=PRIVATE")):
            with self.subTest(failure=type(failure).__name__):
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
                    mock.patch("lkfetch.cli.os.path.isfile", return_value=True),
                    mock.patch("lkfetch.cli.os.access", return_value=True),
                    mock.patch("lkfetch.cli.load_browser_cookies", side_effect=failure),
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(["doctor", "--browser", "dia", "--library-id", "lib", "--cookie-domain", "example.invalid"]), 1)
                self.assertIn("Dia cookie reader: error", stdout.getvalue())
                self.assertNotIn("/private", stdout.getvalue())
                self.assertNotIn("PRIVATE", stdout.getvalue())

    def test_doctor_dia_ambiguous_profile_has_fixed_hint(self):
        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
            mock.patch("lkfetch.cli.os.path.isfile", return_value=True),
            mock.patch("lkfetch.cli.os.access", return_value=True),
            mock.patch("lkfetch.cli.load_browser_cookies", side_effect=BrowserError("cookie_config", "/private/secret Cookie=PRIVATE")),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["doctor", "--browser", "dia", "--library-id", "lib", "--cookie-domain", "example.invalid"]), 1)
        self.assertIn("Profile: configured=auto selected=none", stdout.getvalue())
        self.assertIn("Dia cookie reader: cookie_config:", stdout.getvalue())
        self.assertIn("--profile", stdout.getvalue())
        self.assertNotIn("PRIVATE", stdout.getvalue())
        self.assertNotIn("/private", stdout.getvalue())

    def test_doctor_dia_temp_sqlite_and_fake_keychain(self):
        database = self.directory / "Library/Application Support/Dia/User Data/Default/Network/Cookies"
        database.parent.mkdir(parents=True)
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE cookies (host_key TEXT)")
            connection.executemany("INSERT INTO cookies VALUES (?)", [(".sub.example.invalid",), ("evil-example.invalid",)])
            connection.commit()

        def fake_reader(path, domain):
            self.assertEqual(path, database)
            self.assertEqual(domain, "example.invalid")
            password = browser_module._dia_password()
            self.assertEqual(password, bytearray(b"fake-key"))
            password[:] = b"\x00" * len(password)
            return [object()]

        outcomes = (
            (subprocess.CompletedProcess([], 0, stdout=b"fake-key\n", stderr=b""), 0, "ready"),
            (subprocess.TimeoutExpired("security", 10), 1, "cookie_timeout"),
            (subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"User denied Cookie=PRIVATE /private/secret"), 1, "cookie_denied"),
            (subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"opaque failure"), 1, "cookie_denied"),
            (subprocess.CompletedProcess([], 0, stdout=b"", stderr=b""), 1, "cookie_error"),
        )
        open_readonly = browser_module._readonly_connection
        for outcome, exit_code, reader in outcomes:
            with self.subTest(reader=reader):
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {}, clear=True),
                    mock.patch("lkfetch.browser.Path.home", return_value=self.directory),
                    mock.patch("lkfetch.browser._readonly_connection", side_effect=lambda path: closing(open_readonly(path))),
                    mock.patch("lkfetch.browser._load_dia", side_effect=fake_reader) as loaded,
                    mock.patch("lkfetch.browser.subprocess.run", side_effect=[outcome]) as security,
                    mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
                    mock.patch("lkfetch.cli.os.path.isfile", return_value=True),
                    mock.patch("lkfetch.cli.os.access", return_value=True),
                    mock.patch("lkfetch.cli.download_pdf") as fetched,
                    mock.patch("lkfetch.download.urllib.request.build_opener") as network,
                    redirect_stdout(stdout),
                ):
                    result = main(["doctor", "--browser", "dia", "--library-id", "lib", "--cookie-domain", "example.invalid"])
                    loaded.assert_called_once()
                    self.assertEqual(result, exit_code, stdout.getvalue())
                security.assert_called_once()
                self.assertEqual(security.call_args.args[0][:2], ["/usr/bin/security", "find-generic-password"])
                self.assertEqual(security.call_args.args[0][2:], ["-w", "-a", "Dia", "-s", "Dia Safe Storage"])
                self.assertEqual(security.call_args.kwargs["timeout"], 10)
                fetched.assert_not_called()
                network.assert_not_called()
                self.assertIn(f"Dia cookie reader: {reader}", stdout.getvalue())
                self.assertNotIn(str(self.directory), stdout.getvalue())
                self.assertNotIn("fake-key", stdout.getvalue())
                self.assertNotIn("PRIVATE", stdout.getvalue())
                if exit_code == 0:
                    self.assertIn("Profile: configured=auto selected=Default", stdout.getvalue())
                elif reader == "cookie_timeout":
                    self.assertIn("approve access and retry", stdout.getvalue())
                elif reader == "cookie_denied":
                    self.assertIn("allow access and retry", stdout.getvalue())
                elif reader == "cookie_error":
                    self.assertIn("check browser access", stdout.getvalue())

    def test_dia_reader_uses_inherited_load_without_constructor_or_db_copy(self):
        database = self.directory / "Cookies"
        with closing(sqlite3.connect(database)) as connection:
            connection.execute("CREATE TABLE cookies (host_key TEXT)")
            connection.execute("INSERT INTO cookies VALUES ('example.invalid')")
            connection.commit()

        fake = types.ModuleType("browser_cookie3")
        calls = []
        captured = {}

        def derive(password, salt, length, rounds):
            self.assertEqual((bytes(password), salt, length, rounds), (b"fake-key", b"saltysalt", 16, 1003))
            captured["password"] = password
            return b"derived"

        class ChromiumBased:
            def __init__(self):
                raise AssertionError("constructor must not run")

            def load(reader):
                calls.append("load")
                self.assertEqual(reader.cookie_file, database)
                self.assertEqual(reader.v10_key, b"derived")
                self.assertIs(fake._DatabaseConnetion, browser_module._ReadOnlyConnection)
                with fake._DatabaseConnetion(reader.cookie_file) as connection:
                    self.assertEqual(connection.execute("SELECT host_key FROM cookies").fetchone(), ("example.invalid",))
                return CookieJar()

        fake.PBKDF2 = derive
        fake.ChromiumBased = ChromiumBased
        original_connection = object()
        fake._DatabaseConnetion = original_connection
        with (
            mock.patch.dict(sys.modules, {"browser_cookie3": fake}),
            mock.patch("lkfetch.browser.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout=b"fake-key\n", stderr=b"")) as security,
        ):
            cookies = browser_module._load_dia(database, "example.invalid")
        self.assertEqual(len(cookies), 0)
        self.assertEqual(calls, ["load"])
        self.assertIs(fake._DatabaseConnetion, original_connection)
        self.assertEqual(captured["password"], bytearray())
        security.assert_called_once()

    def test_doctor_missing_config_and_dependency(self):
        for args, environment, missing in (
            (["doctor", "--cookie-domain", "example.invalid"], {}, "library_id"),
            (["doctor", "--library-id", "lib"], {}, "cookie_domain"),
            (["doctor", "--library-id", "lib", "--cookie-domain", " "], {"LKFETCH_COOKIE_DOMAIN": "env.invalid"}, "cookie_domain"),
        ):
            with self.subTest(missing=missing, args=args):
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, environment, clear=True),
                    mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
                    mock.patch("lkfetch.cli.load_browser_cookies") as loaded,
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(args), 2)
                self.assertIn(f"{missing}: missing", stdout.getvalue())
                self.assertIn("Chrome cookie reader: error", stdout.getvalue())
                loaded.assert_not_called()

        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("lkfetch.cli.chrome_cookie_loader", side_effect=ImportError),
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["doctor", "--library-id", "lib", "--cookie-domain", "example.invalid"]), 1)
        self.assertIn("Dependency: missing", stdout.getvalue())
        self.assertIn("Chrome cookie reader: error", stdout.getvalue())

        stdout = io.StringIO()
        with (
            mock.patch.dict(os.environ, {}, clear=True),
            mock.patch("lkfetch.cli.chrome_cookie_loader", return_value=object()),
            mock.patch("lkfetch.cli.load_browser_cookies") as loaded,
            redirect_stdout(stdout),
        ):
            self.assertEqual(main(["doctor", "--library-id", "lib", "--cookie-domain", "%"]), 1)
        loaded.assert_not_called()
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
            mock.call("10.1234/first", "cli", "cli.invalid", str(output), browser="chrome", profile=None),
            mock.call("10.1234/second", "cli", "cli.invalid", str(output), browser="chrome", profile=None),
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

    def test_batch_stops_immediately_on_cookie_access_failure(self):
        source = self.directory / "dois.txt"
        source.write_text("10.1234/first\n10.1234/second\n", encoding="utf-8")
        for category in ("cookie_timeout", "cookie_denied", "cookie_error", "cookie_config"):
            with self.subTest(category=category):
                stdout = io.StringIO()
                with (
                    mock.patch.dict(os.environ, {
                        "LKFETCH_LIBRARY_ID": "lib", "LKFETCH_COOKIE_DOMAIN": "example.invalid",
                        "LKFETCH_BROWSER": "dia", "LKFETCH_BROWSER_PROFILE": "Profile 2",
                    }, clear=True),
                    mock.patch("lkfetch.cli.download_pdf", side_effect=DownloadError(category, "/private/secret Cookie=PRIVATE")) as fetched,
                    mock.patch("lkfetch.cli.time.sleep") as sleep,
                    redirect_stdout(stdout),
                ):
                    self.assertEqual(main(["batch", str(source)]), 1)
                fetched.assert_called_once_with("10.1234/first", "lib", "example.invalid", ".", browser="dia", profile="Profile 2")
                sleep.assert_not_called()
                self.assertNotIn("10.1234/second", stdout.getvalue())
                self.assertNotIn("PRIVATE", stdout.getvalue())
                self.assertIn(f"stopped={category}", stdout.getvalue())
                self.assertIn("Status: failure (" + category + ": ", stdout.getvalue())
                if category == "cookie_config":
                    self.assertIn("--profile", stdout.getvalue())

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

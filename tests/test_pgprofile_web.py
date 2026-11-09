import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import pgprofile_web


VALID_HTML = "<!doctype html><html><body>report</body></html>"


def make_config(cache_directory):
    return pgprofile_web.AppConfig(
        timezone=ZoneInfo("Europe/Madrid"),
        postgresql=pgprofile_web.PostgreSQLConfig(
            host="database.example",
            port=5432,
            database="postgres",
            user="reporter",
            sslmode="verify-full",
            connect_timeout=10,
            statement_timeout_seconds=120,
        ),
        reports=pgprofile_web.ReportsConfig(
            cache_directory=Path(cache_directory), max_interval_hours=24, recent_reports=10
        ),
        log_level="INFO",
    )


class ConfigurationTests(unittest.TestCase):
    def test_loads_example(self):
        config = pgprofile_web.load_config("config.example.yaml")
        self.assertEqual(config.postgresql.user, "pgprofile_web")
        self.assertEqual(config.timezone.key, "Europe/Madrid")
        self.assertEqual(config.reports.server_name, "local")
        self.assertEqual(config.reports.regular_function, ("public", "get_report"))
        self.assertEqual(
            config.reports.differential_function, ("public", "get_diffreport")
        )

    def test_rejects_password_in_yaml(self):
        content = """
postgresql:
  host: localhost
  database: postgres
  user: reporter
  password: secret
reports:
  cache_directory: /tmp/reports
"""
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            with self.assertRaises(pgprofile_web.ConfigError):
                pgprofile_web.load_config(stream.name)

    def test_rejects_unsafe_report_function_name(self):
        content = """
postgresql:
  host: localhost
  database: postgres
  user: reporter
reports:
  cache_directory: /tmp/reports
  differential_function: public.get_diffreport;drop_table
"""
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            with self.assertRaises(pgprofile_web.ConfigError):
                pgprofile_web.load_config(stream.name)


class DateTimeTests(unittest.TestCase):
    def test_default_intervals_are_consecutive_completed_hours(self):
        now = datetime(2026, 11, 9, 12, 34, tzinfo=ZoneInfo("Europe/Madrid"))
        self.assertEqual(
            pgprofile_web.default_intervals(ZoneInfo("Europe/Madrid"), now),
            (
                "2026-11-09T11:00",
                "2026-11-09T12:00",
                "2026-11-09T10:00",
                "2026-11-09T11:00",
            ),
        )

    def test_rejects_nonexistent_dst_time(self):
        with self.assertRaisesRegex(pgprofile_web.InputError, "does not exist"):
            pgprofile_web.parse_local_datetime("2026-03-29T02:30", ZoneInfo("Europe/Madrid"))

    def test_rejects_ambiguous_dst_time(self):
        with self.assertRaisesRegex(pgprofile_web.InputError, "ambiguous"):
            pgprofile_web.parse_local_datetime("2026-10-25T02:30", ZoneInfo("Europe/Madrid"))

    def test_rejects_excessive_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with self.assertRaisesRegex(pgprofile_web.InputError, "24 hours"):
                pgprofile_web.validate_interval(
                    "2026-11-04T09:00", "2026-11-05T10:00", config
                )


class DatabaseTests(unittest.TestCase):
    def test_report_query_uses_bound_parameters(self):
        executed = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def execute(self, query, parameters):
                executed.append((query, parameters))

            def fetchone(self):
                return (VALID_HTML,)

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def cursor(self):
                return Cursor()

        captured_kwargs = {}

        def connect(**kwargs):
            captured_kwargs.update(kwargs)
            return Connection()

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            start, end = pgprofile_web.validate_interval(
                "2026-11-04T09:30", "2026-11-04T10:00", config
            )
            comparison_start, comparison_end = pgprofile_web.validate_interval(
                "2026-11-03T09:30", "2026-11-03T10:00", config
            )
            result = pgprofile_web.generate_report(
                config,
                start,
                end,
                "differential",
                comparison_start,
                comparison_end,
                connect=connect,
            )

        self.assertEqual(result, VALID_HTML)
        self.assertNotIn("password", captured_kwargs)
        self.assertEqual(
            executed[1][1],
            ("local", start, end, comparison_start, comparison_end),
        )
        self.assertNotIn(start.isoformat(), str(executed[1][0]))
        self.assertIn("get_diffreport", str(executed[1][0]))

    def test_connection_check_is_read_only(self):
        executed = []

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def execute(self, query, parameters):
                executed.append((query, parameters))

            def fetchone(self):
                return (
                    "postgres",
                    "reporter",
                    "192.0.2.10",
                    5432,
                    "get_report(name,tstzrange)",
                    "get_diffreport(name,tstzrange,tstzrange)",
                )

        class Connection:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def cursor(self):
                return Cursor()

        with tempfile.TemporaryDirectory() as directory:
            result = pgprofile_web.check_connection(
                make_config(directory), connect=lambda **kwargs: Connection()
            )

        self.assertEqual(result[0], "postgres")
        self.assertEqual(len(executed), 1)
        self.assertTrue(executed[0][0].lstrip().upper().startswith("SELECT"))
        self.assertEqual(
            executed[0][1],
            (
                "public.get_report(name,tstzrange,text,boolean,name[])",
                "public.get_diffreport(name,tstzrange,tstzrange,text,boolean,name[])",
            ),
        )


class CacheTests(unittest.TestCase):
    def test_cache_hit_does_not_generate_again(self):
        calls = []

        def generator(
            config,
            start,
            end,
            report_type,
            comparison_start,
            comparison_end,
        ):
            calls.append(
                (start, end, report_type, comparison_start, comparison_end)
            )
            return VALID_HTML

        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            start, end = pgprofile_web.validate_interval(
                "2026-11-04T09:30", "2026-11-04T10:00", config
            )
            comparison_start, comparison_end = pgprofile_web.validate_interval(
                "2026-11-03T09:30", "2026-11-03T10:00", config
            )
            identifier, generated = pgprofile_web.get_or_create_report(
                config,
                start,
                end,
                "differential",
                comparison_start,
                comparison_end,
                generator,
            )
            second_identifier, second_generated = pgprofile_web.get_or_create_report(
                config,
                start,
                end,
                "differential",
                comparison_start,
                comparison_end,
                generator,
            )
            mode = stat.S_IMODE(os.stat(pgprofile_web.report_path(config, identifier)).st_mode)

        self.assertTrue(generated)
        self.assertFalse(second_generated)
        self.assertEqual(identifier, second_identifier)
        self.assertTrue(identifier.startswith("differential-"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(mode, 0o600)

    def test_report_types_have_different_cache_ids(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            start, end = pgprofile_web.validate_interval(
                "2026-11-04T09:30", "2026-11-04T10:00", config
            )
            comparison_start, comparison_end = pgprofile_web.validate_interval(
                "2026-11-03T09:30", "2026-11-03T10:00", config
            )
            regular = pgprofile_web.report_id(config, start, end, "regular")
            differential = pgprofile_web.report_id(
                config,
                start,
                end,
                "differential",
                comparison_start,
                comparison_end,
            )

        self.assertNotEqual(regular, differential)
        self.assertTrue(pgprofile_web.valid_report_id(regular))
        self.assertTrue(pgprofile_web.valid_report_id(differential))

    def test_differential_cache_id_requires_second_interval(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            start, end = pgprofile_web.validate_interval(
                "2026-11-04T09:30", "2026-11-04T10:00", config
            )
            with self.assertRaisesRegex(pgprofile_web.InputError, "second interval"):
                pgprofile_web.report_id(config, start, end, "differential")

    def test_legacy_cache_id_remains_valid(self):
        identifier = (
            "20261104T083000Z-20261104T090000Z-"
            "0123456789abcdef0123456789abcdef"
        )
        self.assertTrue(pgprofile_web.valid_report_id(identifier))

    def test_rejects_path_traversal(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(pgprofile_web.InputError):
                pgprofile_web.report_path(make_config(directory), "../../etc/passwd")


class RequestTests(unittest.TestCase):
    def test_get_renders_escaped_script_name(self):
        with tempfile.TemporaryDirectory() as directory:
            response = pgprofile_web.handle_request(
                {"REQUEST_METHOD": "GET", "SCRIPT_NAME": '\"<bad>'},
                b"",
                make_config(directory),
            )
        page = response.body.decode("utf-8")
        self.assertNotIn('action="\"<bad>"', page)
        self.assertIn('action="/pgprofile-web/"', page)
        self.assertIn('<option value="differential">Differential report</option>', page)
        self.assertIn("Interval 2 (differential reports only)", page)

    def test_valid_post_redirects_to_report(self):
        body = urlencode(
            {
                "report_type": "differential",
                "start": "2026-11-04T09:30",
                "end": "2026-11-04T10:00",
                "comparison_start": "2026-11-03T09:30",
                "comparison_end": "2026-11-03T10:00",
            }
        ).encode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            with patch(
                "pgprofile_web.get_or_create_report",
                return_value=(
                    "differential-20261104T083000Z-20261104T090000Z-"
                    "0123456789abcdef0123456789abcdef",
                    True,
                ),
            ) as create_report:
                response = pgprofile_web.handle_request(
                    {
                        "REQUEST_METHOD": "POST",
                        "SCRIPT_NAME": "/pgprofile-web",
                        "CONTENT_TYPE": "application/x-www-form-urlencoded",
                    },
                    body,
                    config,
                )
        self.assertEqual(response.status, "303 See Other")
        self.assertTrue(dict(response.headers)["Location"].startswith("/pgprofile-web?report="))
        self.assertEqual(create_report.call_args.args[3], "differential")
        self.assertIsNotNone(create_report.call_args.args[4])
        self.assertIsNotNone(create_report.call_args.args[5])

    def test_rejects_unknown_report_type(self):
        body = urlencode(
            {
                "report_type": "drop-table",
                "start": "2026-11-04T09:30",
                "end": "2026-11-04T10:00",
            }
        ).encode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            with patch("pgprofile_web.get_or_create_report") as create_report:
                response = pgprofile_web.handle_request(
                    {
                        "REQUEST_METHOD": "POST",
                        "SCRIPT_NAME": "/pgprofile-web",
                        "CONTENT_TYPE": "application/x-www-form-urlencoded",
                    },
                    body,
                    make_config(directory),
                )

        self.assertEqual(response.status, "400 Bad Request")
        self.assertIn(b"Choose a valid report type", response.body)
        create_report.assert_not_called()

    def test_differential_report_requires_second_interval(self):
        body = urlencode(
            {
                "report_type": "differential",
                "start": "2026-11-04T09:30",
                "end": "2026-11-04T10:00",
            }
        ).encode("ascii")
        with tempfile.TemporaryDirectory() as directory:
            with patch("pgprofile_web.get_or_create_report") as create_report:
                response = pgprofile_web.handle_request(
                    {
                        "REQUEST_METHOD": "POST",
                        "SCRIPT_NAME": "/pgprofile-web",
                        "CONTENT_TYPE": "application/x-www-form-urlencoded",
                    },
                    body,
                    make_config(directory),
                )

        self.assertEqual(response.status, "400 Bad Request")
        self.assertIn(b"Use a valid date and time", response.body)
        create_report.assert_not_called()

    def test_serialized_response_has_security_headers(self):
        output = pgprofile_web.serialize_response(pgprofile_web.Response.html("ok"))
        self.assertIn(b"Content-Security-Policy:", output)
        self.assertNotIn(b"script-src 'unsafe-inline'", output)
        self.assertIn(b"Cache-Control: private, no-store", output)
        self.assertTrue(output.endswith(b"ok"))

    def test_report_response_allows_only_embedded_script(self):
        with tempfile.TemporaryDirectory() as directory:
            config = make_config(directory)
            start, end = pgprofile_web.validate_interval(
                "2026-11-04T09:30", "2026-11-04T10:00", config
            )
            identifier = pgprofile_web.report_id(config, start, end)
            path = pgprofile_web.report_path(config, identifier)
            path.write_text(VALID_HTML, encoding="utf-8")
            response = pgprofile_web._report_response(config, identifier, False)
            output = pgprofile_web.serialize_response(response)

        self.assertIn(b"script-src 'unsafe-inline'", output)
        self.assertIn(b"connect-src 'none'", output)
        self.assertEqual(output.count(b"Content-Security-Policy:"), 1)


if __name__ == "__main__":
    unittest.main()

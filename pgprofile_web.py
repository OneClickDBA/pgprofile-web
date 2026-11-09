"""Small CGI application for generating cached pg_profile reports."""

import fcntl
import hashlib
import html
import logging
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import psycopg
import yaml
from psycopg import sql


DEFAULT_CONFIG_PATH = "/etc/pgprofile-web/config.yaml"
MAX_REQUEST_BYTES = 8192
REPORT_ID_RE = re.compile(
    r"^(?:(?P<type>regular|differential)-)?"
    r"(?P<start>\d{8}T\d{6}Z)-(?P<end>\d{8}T\d{6}Z)"
    r"(?:-(?P<comparison_start>\d{8}T\d{6}Z)-"
    r"(?P<comparison_end>\d{8}T\d{6}Z))?"
    r"-(?P<digest>[0-9a-f]{32})$"
)
REPORT_TYPES = {"regular", "differential"}

LOG = logging.getLogger("pgprofile-web")


class ConfigError(ValueError):
    """Configuration is missing or invalid."""


class InputError(ValueError):
    """A user-submitted value is invalid."""


class ReportError(RuntimeError):
    """Report generation failed without exposing internal details."""


@dataclass(frozen=True)
class PostgreSQLConfig:
    host: str
    port: int
    database: str
    user: str
    sslmode: str
    connect_timeout: int
    statement_timeout_seconds: int
    sslrootcert: Optional[str] = None
    passfile: Optional[str] = None

    def connection_kwargs(self) -> Dict[str, object]:
        values: Dict[str, object] = {
            "host": self.host,
            "port": self.port,
            "dbname": self.database,
            "user": self.user,
            "sslmode": self.sslmode,
            "connect_timeout": self.connect_timeout,
        }
        if self.sslrootcert:
            values["sslrootcert"] = self.sslrootcert
        if self.passfile:
            values["passfile"] = self.passfile
        return values

    def repository_identity(self) -> str:
        return "%s:%d/%s" % (self.host, self.port, self.database)


@dataclass(frozen=True)
class ReportsConfig:
    cache_directory: Path
    max_interval_hours: int
    recent_reports: int
    server_name: str = "local"
    regular_function: Tuple[str, str] = ("public", "get_report")
    differential_function: Tuple[str, str] = ("public", "get_diffreport")

    def function_for(self, report_type: str) -> Tuple[str, str]:
        if report_type == "regular":
            return self.regular_function
        if report_type == "differential":
            return self.differential_function
        raise InputError("Invalid report type.")

    def function_signature(self, report_type: str) -> str:
        schema, function = self.function_for(report_type)
        arguments = (
            "name,tstzrange,tstzrange,text,boolean,name[]"
            if report_type == "differential"
            else "name,tstzrange,text,boolean,name[]"
        )
        return "%s.%s(%s)" % (schema, function, arguments)


@dataclass(frozen=True)
class AppConfig:
    timezone: ZoneInfo
    postgresql: PostgreSQLConfig
    reports: ReportsConfig
    log_level: str


@dataclass(frozen=True)
class Response:
    status: str
    headers: Tuple[Tuple[str, str], ...]
    body: bytes

    @classmethod
    def html(cls, body: str, status: str = "200 OK") -> "Response":
        return cls(status, (("Content-Type", "text/html; charset=utf-8"),), body.encode("utf-8"))


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ConfigError("%s must be a mapping" % name)
    return value


def _text(section: Mapping[str, object], key: str, default: Optional[str] = None) -> str:
    value = section.get(key, default)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError("%s must be a nonempty string" % key)
    return value.strip()


def _integer(
    section: Mapping[str, object], key: str, default: Optional[int] = None, minimum: int = 1
) -> int:
    value = section.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError("%s must be an integer of at least %d" % (key, minimum))
    return value


def _qualified_function(value: object, key: str) -> Tuple[str, str]:
    if not isinstance(value, str):
        raise ConfigError("%s must be a schema-qualified function name" % key)
    parts = value.split(".")
    identifier = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
    if len(parts) != 2 or any(not identifier.fullmatch(part) for part in parts):
        raise ConfigError(
            "%s must have the form schema.function using unquoted identifiers" % key
        )
    return parts[0], parts[1]


def load_config(path: str) -> AppConfig:
    """Load and validate the YAML configuration file."""
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("cannot read configuration") from exc

    root = _mapping(raw, "configuration")
    application = _mapping(root.get("application", {}), "application")
    database = _mapping(root.get("postgresql"), "postgresql")
    reports = _mapping(root.get("reports"), "reports")
    logging_config = _mapping(root.get("logging", {}), "logging")

    if "password" in database:
        raise ConfigError("postgresql.password is not allowed; use a passfile or environment")

    timezone_name = _text(application, "timezone", "UTC")
    try:
        local_timezone = ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ConfigError("unknown application timezone") from exc

    pg_config = PostgreSQLConfig(
        host=_text(database, "host"),
        port=_integer(database, "port", 5432),
        database=_text(database, "database"),
        user=_text(database, "user"),
        sslmode=_text(database, "sslmode", "verify-full"),
        connect_timeout=_integer(database, "connect_timeout", 10),
        statement_timeout_seconds=_integer(database, "statement_timeout_seconds", 120),
        sslrootcert=database.get("sslrootcert") or None,
        passfile=database.get("passfile") or None,
    )
    if pg_config.sslrootcert is not None and not isinstance(pg_config.sslrootcert, str):
        raise ConfigError("sslrootcert must be a string")
    if pg_config.passfile is not None and not isinstance(pg_config.passfile, str):
        raise ConfigError("passfile must be a string")

    cache_directory = Path(_text(reports, "cache_directory"))
    if not cache_directory.is_absolute():
        raise ConfigError("cache_directory must be an absolute path")

    reports_config = ReportsConfig(
        cache_directory=cache_directory,
        max_interval_hours=_integer(reports, "max_interval_hours", 24),
        recent_reports=_integer(reports, "recent_reports", 10, minimum=0),
        server_name=_text(reports, "server_name", "local"),
        regular_function=_qualified_function(
            reports.get("regular_function", "public.get_report"), "regular_function"
        ),
        differential_function=_qualified_function(
            reports.get("differential_function", "public.get_diffreport"),
            "differential_function",
        ),
    )
    log_level = _text(logging_config, "level", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigError("invalid logging level")

    return AppConfig(local_timezone, pg_config, reports_config, log_level)


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level),
        format="%(asctime)s %(levelname)s pgprofile-web: %(message)s",
    )


def parse_local_datetime(value: str, local_timezone: ZoneInfo) -> datetime:
    """Parse an HTML datetime-local value and reject DST gaps/ambiguity."""
    try:
        naive = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except (TypeError, ValueError) as exc:
        raise InputError("Use a valid date and time.") from exc

    candidates: List[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=local_timezone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(local_timezone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)

    offsets = {candidate.utcoffset() for candidate in candidates}
    if not candidates:
        raise InputError("That local time does not exist because of a clock change.")
    if len(offsets) > 1:
        raise InputError("That local time is ambiguous because of a clock change.")
    return candidates[0]


def validate_interval(
    start_value: str, end_value: str, config: AppConfig
) -> Tuple[datetime, datetime]:
    start = parse_local_datetime(start_value, config.timezone)
    end = parse_local_datetime(end_value, config.timezone)
    if end <= start:
        raise InputError("End time must be later than start time.")
    if end - start > timedelta(hours=config.reports.max_interval_hours):
        raise InputError(
            "The interval cannot exceed %d hours." % config.reports.max_interval_hours
        )
    return start, end


def default_interval(local_timezone: ZoneInfo, now: Optional[datetime] = None) -> Tuple[str, str]:
    current = now.astimezone(local_timezone) if now else datetime.now(local_timezone)
    end = current.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(hours=1)
    return start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M")


def default_intervals(
    local_timezone: ZoneInfo, now: Optional[datetime] = None
) -> Tuple[str, str, str, str]:
    """Return the previous completed hour and the hour immediately before it."""
    current = now.astimezone(local_timezone) if now else datetime.now(local_timezone)
    first_end = current.replace(minute=0, second=0, microsecond=0)
    first_start = first_end - timedelta(hours=1)
    second_end = first_start
    second_start = second_end - timedelta(hours=1)
    return (
        first_start.strftime("%Y-%m-%dT%H:%M"),
        first_end.strftime("%Y-%m-%dT%H:%M"),
        second_start.strftime("%Y-%m-%dT%H:%M"),
        second_end.strftime("%Y-%m-%dT%H:%M"),
    )


def validate_report_type(value: str) -> str:
    if value not in REPORT_TYPES:
        raise InputError("Choose a valid report type.")
    return value


def report_id(
    config: AppConfig,
    start: datetime,
    end: datetime,
    report_type: str = "regular",
    comparison_start: Optional[datetime] = None,
    comparison_end: Optional[datetime] = None,
) -> str:
    report_type = validate_report_type(report_type)
    start_utc = start.astimezone(timezone.utc)
    end_utc = end.astimezone(timezone.utc)
    if report_type == "differential" and (
        comparison_start is None or comparison_end is None
    ):
        raise InputError("Differential reports require a second interval.")
    function_name = ".".join(config.reports.function_for(report_type))
    identity_values = [
        config.postgresql.repository_identity(),
        config.reports.server_name,
        report_type,
        function_name,
        start_utc.isoformat(),
        end_utc.isoformat(),
    ]
    identifier_values = [
        report_type,
        start_utc.strftime("%Y%m%dT%H%M%SZ"),
        end_utc.strftime("%Y%m%dT%H%M%SZ"),
    ]
    if comparison_start is not None and comparison_end is not None:
        comparison_start_utc = comparison_start.astimezone(timezone.utc)
        comparison_end_utc = comparison_end.astimezone(timezone.utc)
        identity_values.extend(
            (comparison_start_utc.isoformat(), comparison_end_utc.isoformat())
        )
        identifier_values.extend(
            (
                comparison_start_utc.strftime("%Y%m%dT%H%M%SZ"),
                comparison_end_utc.strftime("%Y%m%dT%H%M%SZ"),
            )
        )
    source = "\0".join(identity_values).encode("utf-8")
    digest = hashlib.sha256(source).hexdigest()[:32]
    identifier_values.append(digest)
    return "-".join(identifier_values)


def valid_report_id(value: str) -> bool:
    return bool(REPORT_ID_RE.fullmatch(value))


def report_path(config: AppConfig, identifier: str) -> Path:
    if not valid_report_id(identifier):
        raise InputError("Invalid report identifier.")
    return config.reports.cache_directory / (identifier + ".html")


def _valid_report_content(content: str) -> bool:
    beginning = content.lstrip()[:200].lower()
    return bool(content.strip()) and ("<html" in beginning or "<!doctype html" in beginning)


def generate_report(
    config: AppConfig,
    start: datetime,
    end: datetime,
    report_type: str = "regular",
    comparison_start: Optional[datetime] = None,
    comparison_end: Optional[datetime] = None,
    connect: Callable[..., object] = psycopg.connect,
) -> str:
    """Run the installed single-repository pg_profile report function."""
    function = config.reports.function_for(validate_report_type(report_type))
    timeout = "%ds" % config.postgresql.statement_timeout_seconds
    try:
        with connect(**config.postgresql.connection_kwargs()) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT set_config('statement_timeout', %s, false)", (timeout,))
                if report_type == "differential":
                    if comparison_start is None or comparison_end is None:
                        raise InputError("Differential reports require a second interval.")
                    cursor.execute(
                        sql.SQL(
                            "SELECT {}(%s::name, tstzrange(%s, %s, '[)'), "
                            "tstzrange(%s, %s, '[)'))"
                        ).format(sql.Identifier(*function)),
                        (
                            config.reports.server_name,
                            start,
                            end,
                            comparison_start,
                            comparison_end,
                        ),
                    )
                else:
                    cursor.execute(
                        sql.SQL(
                            "SELECT {}(%s::name, tstzrange(%s, %s, '[)'))"
                        ).format(sql.Identifier(*function)),
                        (config.reports.server_name, start, end),
                    )
                row = cursor.fetchone()
    except psycopg.errors.QueryCanceled as exc:
        raise ReportError("Report generation timed out.") from exc
    except psycopg.Error as exc:
        if getattr(exc.diag, "message_primary", "") == "Server not found.":
            raise ReportError(
                "The configured pg_profile monitored server was not found."
            ) from exc
        raise ReportError("The report database is unavailable or rejected the request.") from exc

    content = row[0] if row else None
    if not isinstance(content, str) or not _valid_report_content(content):
        raise ReportError("The database did not return a valid HTML report.")
    return content


def check_connection(
    config: AppConfig, connect: Callable[..., object] = psycopg.connect
) -> Tuple[
    str,
    str,
    Optional[str],
    Optional[int],
    Optional[str],
    Optional[str],
]:
    """Check database access and return non-sensitive repository information."""
    with connect(**config.postgresql.connection_kwargs()) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """SELECT current_database(),
                          current_user,
                          inet_server_addr()::text,
                          inet_server_port(),
                          to_regprocedure(%s)::text,
                          to_regprocedure(%s)::text""",
                (
                    config.reports.function_signature("regular"),
                    config.reports.function_signature("differential"),
                ),
            )
            row = cursor.fetchone()
    if not row:
        raise RuntimeError("PostgreSQL returned no connection-check result")
    return row


def _ensure_cache_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not path.is_dir():
        raise ReportError("The report cache is unavailable.")


def _read_cached(path: Path) -> Optional[str]:
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReportError("The report cache is unavailable.") from exc
    return content if _valid_report_content(content) else None


def _atomic_write(path: Path, content: str) -> None:
    temporary_name: Optional[str] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(path.parent),
            prefix=".report-",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary_name = stream.name
            os.chmod(temporary_name, 0o600)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        temporary_name = None
    except OSError as exc:
        raise ReportError("Could not save the generated report.") from exc
    finally:
        if temporary_name:
            try:
                os.unlink(temporary_name)
            except FileNotFoundError:
                pass


def get_or_create_report(
    config: AppConfig,
    start: datetime,
    end: datetime,
    report_type: str = "regular",
    comparison_start: Optional[datetime] = None,
    comparison_end: Optional[datetime] = None,
    generator: Callable[
        [AppConfig, datetime, datetime, str, Optional[datetime], Optional[datetime]], str
    ] = generate_report,
) -> Tuple[str, bool]:
    """Return a report ID and whether PostgreSQL generation was required."""
    report_type = validate_report_type(report_type)
    identifier = report_id(
        config, start, end, report_type, comparison_start, comparison_end
    )
    path = report_path(config, identifier)
    _ensure_cache_directory(path.parent)
    lock_path = path.with_suffix(".lock")
    try:
        lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as exc:
        raise ReportError("The report cache is unavailable.") from exc

    try:
        with os.fdopen(lock_descriptor, "w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            if _read_cached(path) is not None:
                return identifier, False
            content = generator(
                config,
                start,
                end,
                report_type,
                comparison_start,
                comparison_end,
            )
            _atomic_write(path, content)
            return identifier, True
    finally:
        # Persistent empty lock files avoid unlink races between CGI processes.
        pass


def list_recent_reports(config: AppConfig) -> List[Tuple[str, str]]:
    if config.reports.recent_reports == 0:
        return []
    try:
        candidates = sorted(
            config.reports.cache_directory.glob("*.html"),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    except OSError:
        return []
    result: List[Tuple[str, str]] = []
    for path in candidates:
        identifier = path.stem
        match = REPORT_ID_RE.fullmatch(identifier)
        if not match:
            continue
        start = datetime.strptime(match.group("start"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        end = datetime.strptime(match.group("end"), "%Y%m%dT%H%M%SZ").replace(
            tzinfo=timezone.utc
        )
        report_type = match.group("type")
        type_label = {
            "regular": "Regular",
            "differential": "Differential",
            None: "Report",
        }[report_type]
        label = "%s: %s – %s" % (
            type_label,
            start.astimezone(config.timezone).strftime("%Y-%m-%d %H:%M"),
            end.astimezone(config.timezone).strftime("%Y-%m-%d %H:%M %Z"),
        )
        if match.group("comparison_start") and match.group("comparison_end"):
            comparison_start = datetime.strptime(
                match.group("comparison_start"), "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=timezone.utc)
            comparison_end = datetime.strptime(
                match.group("comparison_end"), "%Y%m%dT%H%M%SZ"
            ).replace(tzinfo=timezone.utc)
            label += " vs %s – %s" % (
                comparison_start.astimezone(config.timezone).strftime(
                    "%Y-%m-%d %H:%M"
                ),
                comparison_end.astimezone(config.timezone).strftime(
                    "%Y-%m-%d %H:%M %Z"
                ),
            )
        result.append((identifier, label))
        if len(result) >= config.reports.recent_reports:
            break
    return result


def _page(
    config: AppConfig,
    script_name: str,
    start_value: str,
    end_value: str,
    report_type: str = "regular",
    comparison_start_value: str = "",
    comparison_end_value: str = "",
    error: Optional[str] = None,
) -> str:
    safe_script = html.escape(script_name, quote=True)
    error_html = '<p class="error">%s</p>' % html.escape(error) if error else ""
    recent_items = []
    for identifier, label in list_recent_reports(config):
        view_query = html.escape(urlencode({"report": identifier}), quote=True)
        download_query = html.escape(urlencode({"download": identifier}), quote=True)
        recent_items.append(
            '<li><span>%s</span> <a href="%s?%s">View</a> '
            '<a href="%s?%s">Download</a></li>'
            % (html.escape(label), safe_script, view_query, safe_script, download_query)
        )
    recent_html = ""
    if recent_items:
        recent_html = "<h2>Recent reports</h2><ul>%s</ul>" % "".join(recent_items)
    regular_selected = " selected" if report_type == "regular" else ""
    differential_selected = " selected" if report_type == "differential" else ""

    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>PostgreSQL Performance Reports</title>
  <style>
    body { font: 16px system-ui, sans-serif; max-width: 48rem; margin: 3rem auto; padding: 0 1rem; color: #202124; }
    form { display: grid; gap: 1rem; max-width: 24rem; }
    fieldset { display: grid; gap: 1rem; margin: 0; padding: 1rem; }
    label { display: grid; gap: .35rem; font-weight: 600; }
    input, select, button { box-sizing: border-box; font: inherit; padding: .6rem; }
    button { cursor: pointer; }
    .error { color: #a40000; background: #fff0f0; padding: .75rem; }
    li { margin: .65rem 0; }
    li a { margin-left: .6rem; }
  </style>
</head>
<body>
  <h1>PostgreSQL Performance Reports</h1>
  %s
  <form method="post" action="%s">
    <label>Report type
      <select name="report_type">
        <option value="regular"%s>Regular report</option>
        <option value="differential"%s>Differential report</option>
      </select>
    </label>
    <fieldset>
      <legend>Interval 1</legend>
      <label>Start date and time
        <input type="datetime-local" name="start" value="%s" required>
      </label>
      <label>End date and time
        <input type="datetime-local" name="end" value="%s" required>
      </label>
    </fieldset>
    <fieldset>
      <legend>Interval 2 (differential reports only)</legend>
      <label>Start date and time
        <input type="datetime-local" name="comparison_start" value="%s">
      </label>
      <label>End date and time
        <input type="datetime-local" name="comparison_end" value="%s">
      </label>
    </fieldset>
    <button type="submit">Generate report</button>
  </form>
  %s
</body>
</html>""" % (
        error_html,
        safe_script,
        regular_selected,
        differential_selected,
        html.escape(start_value, quote=True),
        html.escape(end_value, quote=True),
        html.escape(comparison_start_value, quote=True),
        html.escape(comparison_end_value, quote=True),
        recent_html,
    )


def _report_response(config: AppConfig, identifier: str, download: bool) -> Response:
    try:
        content = _read_cached(report_path(config, identifier))
    except InputError:
        return Response.html("<h1>Invalid report identifier</h1>", "400 Bad Request")
    if content is None:
        return Response.html("<h1>Report not found</h1>", "404 Not Found")
    disposition = "attachment" if download else "inline"
    return Response(
        "200 OK",
        (
            ("Content-Type", "text/html; charset=utf-8"),
            ("Content-Disposition", '%s; filename="pgprofile-report-%s.html"' % (disposition, identifier)),
            (
                "Content-Security-Policy",
                "default-src 'none'; script-src 'unsafe-inline'; "
                "style-src 'unsafe-inline'; img-src data:; font-src data:; "
                "connect-src 'none'; object-src 'none'; base-uri 'none'; "
                "form-action 'none'; frame-ancestors 'none'",
            ),
        ),
        content.encode("utf-8"),
    )


def handle_request(
    environ: Mapping[str, str], body: bytes, config: AppConfig
) -> Response:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    script_name = environ.get("SCRIPT_NAME", "/pgprofile-web/")
    if not re.fullmatch(r"/[A-Za-z0-9/_-]*", script_name):
        script_name = "/pgprofile-web/"

    if method == "GET":
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        if "report" in query:
            return _report_response(config, query["report"][0], False)
        if "download" in query:
            return _report_response(config, query["download"][0], True)
        (
            start_value,
            end_value,
            comparison_start_value,
            comparison_end_value,
        ) = default_intervals(config.timezone)
        return Response.html(
            _page(
                config,
                script_name,
                start_value,
                end_value,
                "regular",
                comparison_start_value,
                comparison_end_value,
            )
        )

    if method != "POST":
        return Response(
            "405 Method Not Allowed",
            (("Content-Type", "text/html; charset=utf-8"), ("Allow", "GET, POST")),
            b"<h1>Method not allowed</h1>",
        )

    if len(body) > MAX_REQUEST_BYTES:
        return Response.html("<h1>Request too large</h1>", "413 Content Too Large")
    content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip().lower()
    if content_type != "application/x-www-form-urlencoded":
        return Response.html("<h1>Unsupported form encoding</h1>", "415 Unsupported Media Type")

    start_value = ""
    end_value = ""
    report_type = "regular"
    comparison_start_value = ""
    comparison_end_value = ""
    try:
        form = parse_qs(body.decode("utf-8"), keep_blank_values=True, strict_parsing=True)
        start_value = form.get("start", [""])[0]
        end_value = form.get("end", [""])[0]
        report_type = validate_report_type(form.get("report_type", ["regular"])[0])
        start, end = validate_interval(start_value, end_value, config)
        comparison_start = None
        comparison_end = None
        if report_type == "differential":
            comparison_start_value = form.get("comparison_start", [""])[0]
            comparison_end_value = form.get("comparison_end", [""])[0]
            comparison_start, comparison_end = validate_interval(
                comparison_start_value, comparison_end_value, config
            )
        identifier, generated = get_or_create_report(
            config,
            start,
            end,
            report_type,
            comparison_start,
            comparison_end,
        )
        LOG.info(
            "%s report %s for interval %s to %s (%s)",
            report_type,
            identifier,
            start.isoformat(),
            end.isoformat(),
            "generated" if generated else "cache hit",
        )
    except (UnicodeDecodeError, ValueError) as exc:
        message = str(exc) if isinstance(exc, InputError) else "Invalid form submission."
        return Response.html(
            _page(
                config,
                script_name,
                start_value,
                end_value,
                report_type,
                comparison_start_value,
                comparison_end_value,
                message,
            ),
            "400 Bad Request",
        )
    except ReportError as exc:
        LOG.exception("report generation failed")
        return Response.html(
            _page(
                config,
                script_name,
                start_value,
                end_value,
                report_type,
                comparison_start_value,
                comparison_end_value,
                str(exc),
            ),
            "502 Bad Gateway",
        )

    location = "%s?%s" % (script_name, urlencode({"report": identifier}))
    return Response("303 See Other", (("Location", location),), b"")


SECURITY_HEADERS = (
    ("Cache-Control", "private, no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
)

DEFAULT_CONTENT_SECURITY_POLICY = (
    "default-src 'none'; style-src 'unsafe-inline'; img-src data:; "
    "font-src data:; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


def serialize_response(response: Response) -> bytes:
    headers = list(response.headers) + list(SECURITY_HEADERS)
    if not any(name.lower() == "content-security-policy" for name, _ in headers):
        headers.append(("Content-Security-Policy", DEFAULT_CONTENT_SECURITY_POLICY))
    headers.append(("Content-Length", str(len(response.body))))
    lines = ["Status: %s" % response.status]
    lines.extend("%s: %s" % item for item in headers)
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + response.body


def cgi_main() -> int:
    import sys

    try:
        content_length = int(os.environ.get("CONTENT_LENGTH", "0") or "0")
    except ValueError:
        content_length = MAX_REQUEST_BYTES + 1
    if content_length < 0:
        content_length = MAX_REQUEST_BYTES + 1
    body = sys.stdin.buffer.read(min(content_length, MAX_REQUEST_BYTES + 1))
    config_path = os.environ.get("PGPROFILE_WEB_CONFIG", DEFAULT_CONFIG_PATH)
    try:
        config = load_config(config_path)
        configure_logging(config.log_level)
        response = handle_request(os.environ, body, config)
    except ConfigError:
        logging.basicConfig(level=logging.ERROR)
        LOG.exception("configuration error")
        response = Response.html("<h1>Service configuration error</h1>", "500 Internal Server Error")
    except Exception:
        LOG.exception("unexpected request failure")
        response = Response.html("<h1>Internal server error</h1>", "500 Internal Server Error")
    sys.stdout.buffer.write(serialize_response(response))
    return 0


def command_line_main() -> int:
    """Run small, read-only operator diagnostics from a terminal."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="pgprofile-web operator diagnostics")
    parser.add_argument(
        "--config",
        default=os.environ.get("PGPROFILE_WEB_CONFIG", DEFAULT_CONFIG_PATH),
        help="YAML configuration path (default: %(default)s)",
    )
    parser.add_argument(
        "--check-connection",
        action="store_true",
        help="connect to PostgreSQL and verify both pg_profile report functions",
    )
    arguments = parser.parse_args()
    if not arguments.check_connection:
        parser.print_help()
        return 2

    try:
        config = load_config(arguments.config)
        database, user, address, port, regular_function, differential_function = (
            check_connection(config)
        )
    except ConfigError as exc:
        print("Configuration error: %s" % exc, file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print("PostgreSQL connection failed: %s" % exc, file=sys.stderr)
        return 1
    except Exception as exc:
        print("Connection check failed: %s" % exc, file=sys.stderr)
        return 1

    print("PostgreSQL connection: OK")
    print("Database: %s" % database)
    print("User: %s" % user)
    print("Server: %s:%s" % (address or "local socket", port or "unknown"))
    print("Monitored server: %s" % config.reports.server_name)
    result = 0
    if regular_function:
        print("Regular function: %s" % regular_function)
    else:
        print(
            "Regular function: %s NOT FOUND"
            % config.reports.function_signature("regular"),
            file=sys.stderr,
        )
        result = 1
    if differential_function:
        print("Differential function: %s" % differential_function)
    else:
        print(
            "Differential function: %s NOT FOUND"
            % config.reports.function_signature("differential"),
            file=sys.stderr,
        )
        result = 1
    return result


if __name__ == "__main__":
    raise SystemExit(command_line_main())

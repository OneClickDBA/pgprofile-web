# pgprofile-web

`pgprofile-web` is a deliberately small CGI application that turns a date range
into a cached PostgreSQL `pg_profile` HTML report. It runs behind Nginx and
`fcgiwrap`; it does not need Uvicorn, FastAPI, a worker, or a REST API.

## Requirements

- Python 3.9 or newer
- `psycopg` 3
- PyYAML 6
- Nginx and `fcgiwrap`
- An existing `pg_profile` repository where these queries are valid:

  ```sql
  SELECT public.get_report(
      $1::name,
      tstzrange($2, $3, '[)')
  );
  SELECT public.get_diffreport(
      $1::name,
      tstzrange($2, $3, '[)'),
      tstzrange($4, $5, '[)')
  );
  ```

The PostgreSQL role needs only connection permission and permission to execute
the report function and read anything that function requires. Do not use a
superuser role.

## Install

The exact package installation method depends on the RHEL environment. A basic
application layout is:

```text
/opt/pgprofile-web/
├── pgprofile-web.cgi
└── pgprofile_web.py

/etc/pgprofile-web/
├── config.yaml
├── pgpass                 optional
└── root.crt               when required by TLS configuration

/var/cache/pgprofile-web/
```

Install the Python dependencies from `requirements.txt`, copy
`config.example.yaml` to `/etc/pgprofile-web/config.yaml`, and adapt it. The CGI
entry point must be executable. The user running `fcgiwrap` must be able to:

- traverse `/opt/pgprofile-web` and execute `pgprofile-web.cgi`;
- read the Python module and configuration;
- create files in `/var/cache/pgprofile-web`;
- read the PostgreSQL passfile and TLS CA file, when configured.

Suggested permissions are a cache directory owned by the `fcgiwrap` user with
mode `0700`, report files with mode `0600`, and a passfile with mode `0600`.
Do not put the database password in YAML.

The report functions are configurable. The regular function must accept one
`tstzrange`; the differential function must accept two:

```yaml
reports:
  cache_directory: /var/cache/pgprofile-web
  max_interval_hours: 24
  recent_reports: 10
  server_name: local
  regular_function: public.get_report
  differential_function: public.get_diffreport
```

Function names must use the `schema.function` form. They are validated and
quoted as PostgreSQL identifiers; date/time values remain bound query
parameters. `server_name` is the monitored server name inside the `pg_profile`
repository, not the PostgreSQL database hostname.

Set `PGPROFILE_WEB_CONFIG` in the `fcgiwrap` service environment only when the
configuration is not at `/etc/pgprofile-web/config.yaml`.

## Configure fcgiwrap on RHEL

The RHEL-compatible EPEL package supplies instantiated systemd units named
`fcgiwrap@.socket` and `fcgiwrap@.service`. The instance name is the Unix user
that executes CGI scripts. With the standard Nginx package, use the `nginx`
instance.

Install `fcgiwrap` after enabling the appropriate EPEL repository for the host:

```sh
sudo dnf install fcgiwrap
```

Confirm the packaged unit names and executable path before enabling them:

```sh
systemctl cat fcgiwrap@.socket
systemctl cat fcgiwrap@.service
command -v fcgiwrap
```

Enable the socket-activated Nginx instance:

```sh
sudo systemctl enable --now fcgiwrap@nginx.socket
```

The EPEL socket unit normally creates:

```text
/run/fcgiwrap/fcgiwrap-nginx.sock
```

It has mode `0660` and is accessible to the `nginx` user. The service itself is
started automatically the first time Nginx connects to the socket. Verify both
the unit and socket with:

```sh
systemctl status fcgiwrap@nginx.socket
systemctl status fcgiwrap@nginx.service
ls -lZ /run/fcgiwrap/fcgiwrap-nginx.sock
```

It is normal for the service to be inactive before the first request while the
socket unit is active and listening.

The CGI process runs as `nginx`, so prepare the application files and writable
cache accordingly:

```sh
sudo install -d -o root -g root -m 0755 /opt/pgprofile-web
sudo install -o root -g root -m 0755 pgprofile-web.cgi /opt/pgprofile-web/pgprofile-web.cgi
sudo install -o root -g root -m 0644 pgprofile_web.py /opt/pgprofile-web/pgprofile_web.py

sudo install -d -o root -g nginx -m 0750 /etc/pgprofile-web
sudo install -o root -g nginx -m 0640 config.example.yaml /etc/pgprofile-web/config.yaml
sudo install -d -o nginx -g nginx -m 0700 /var/cache/pgprofile-web
```

If a PostgreSQL passfile is used, install it so only the CGI account can read
it, then set its path in `config.yaml`:

```sh
sudo install -o nginx -g nginx -m 0600 pgpass /etc/pgprofile-web/pgpass
```

The default configuration path is already `/etc/pgprofile-web/config.yaml`. If a
different path is required, add it to the instantiated service with:

```sh
sudo systemctl edit fcgiwrap@nginx.service
```

Add this override:

```ini
[Service]
Environment=PGPROFILE_WEB_CONFIG=/another/path/config.yaml
```

Then apply it:

```sh
sudo systemctl daemon-reload
sudo systemctl restart fcgiwrap@nginx.service
```

Do not create a second custom `fcgiwrap.service` when the packaged instantiated
units are available. Running two wrappers on the same socket will fail.

### Fallback systemd units

If `fcgiwrap` exists but the installation does not provide socket-activated
units, create `/etc/systemd/system/pgprofile-web-fcgi.socket`:

```ini
[Unit]
Description=pgprofile-web FastCGI socket

[Socket]
ListenStream=/run/pgprofile-web/fcgi.sock
SocketUser=nginx
SocketGroup=nginx
SocketMode=0660
DirectoryMode=0755
Accept=false
RemoveOnStop=true

[Install]
WantedBy=sockets.target
```

Create the matching `/etc/systemd/system/pgprofile-web-fcgi.service`:

```ini
[Unit]
Description=fcgiwrap for pgprofile-web
After=network.target

[Service]
Type=simple
User=nginx
Group=nginx
Environment=PGPROFILE_WEB_CONFIG=/etc/pgprofile-web/config.yaml
ExecStart=/usr/sbin/fcgiwrap
StandardError=journal
```

Check the `ExecStart` path with `command -v fcgiwrap`, then activate only the
socket:

```sh
sudo systemctl daemon-reload
sudo systemctl enable --now pgprofile-web-fcgi.socket
systemctl status pgprofile-web-fcgi.socket
```

For these fallback units, change the Nginx upstream to:

```nginx
fastcgi_pass unix:/run/pgprofile-web/fcgi.sock;
```

Use either the packaged `fcgiwrap@nginx.*` units or these fallback units, never
both.

### SELinux on RHEL

When SELinux is enforcing, allow the web-service domain to execute the CGI,
write the report cache, and connect to PostgreSQL. The persistent labels and
booleans can be configured with:

```sh
sudo semanage fcontext -a -t httpd_sys_script_exec_t \
  '/opt/pgprofile-web/pgprofile-web\.cgi'
sudo semanage fcontext -a -t httpd_sys_content_t \
  '/opt/pgprofile-web/pgprofile_web\.py'
sudo semanage fcontext -a -t httpd_sys_rw_content_t \
  '/var/cache/pgprofile-web(/.*)?'
sudo restorecon -Rv /opt/pgprofile-web /var/cache/pgprofile-web
sudo setsebool -P httpd_enable_cgi on
sudo setsebool -P httpd_can_network_connect_db on
```

The `semanage` command is supplied by `policycoreutils-python-utils` on current
RHEL releases. Check `/var/log/audit/audit.log` for AVC denials instead of
disabling SELinux if the local policy differs.

## Nginx example

For the RHEL/EPEL `fcgiwrap@nginx.socket` instance above, add this location to
the authenticated HTTPS `server` block:

```nginx
location = /pgprofile-web {
    include fastcgi_params;
    fastcgi_param SCRIPT_FILENAME /opt/pgprofile-web/pgprofile-web.cgi;
    fastcgi_param SCRIPT_NAME /pgprofile-web;
    fastcgi_pass unix:/run/fcgiwrap/fcgiwrap-nginx.sock;

    # Must exceed postgresql.statement_timeout_seconds.
    fastcgi_read_timeout 150s;
}
```

Protect this location with the site's existing HTTPS authentication. Do not
expose it directly to the public internet. Ensure Nginx does not override the
application's `Cache-Control` or Content Security Policy headers.

Report generation is synchronous: one browser request waits for PostgreSQL.
`fastcgi_read_timeout` therefore needs to be slightly longer than
`statement_timeout_seconds` in the YAML configuration.

Validate and reload the complete setup:

```sh
sudo nginx -t
sudo systemctl reload nginx
curl -k -sS -o /dev/null -w '%{http_code}\n' https://localhost/pgprofile-web
journalctl -u fcgiwrap@nginx.service -n 50 --no-pager
```

Replace the `curl` options with those required by the configured hostname,
certificate, and authentication method. A successful request also activates
`fcgiwrap@nginx.service`.

## Run tests

From the project directory:

```sh
python3 -m unittest discover -s tests -v
```

## Debug a PostgreSQL connection

`pgprofile_web.py` has a read-only connection check. Run it as the same `nginx`
user used by `fcgiwrap` so that file permissions, the passfile, TLS certificate
access, Python dependencies, and SELinux policy match the web request:

```sh
sudo -H -u nginx /usr/bin/python3 /opt/pgprofile-web/pgprofile_web.py \
  --config /etc/pgprofile-web/config.yaml \
  --check-connection
```

If the application uses a virtual environment, replace `/usr/bin/python3` with
that environment's Python executable. The successful output is similar to:

```text
PostgreSQL connection: OK
Database: postgres
User: pgprofile_web
Server: 192.0.2.10:5432
Monitored server: local
Regular function: get_report(name,tstzrange,text,boolean,name[])
Differential function: get_diffreport(name,tstzrange,tstzrange,text,boolean,name[])
```

This command does not generate or cache a report. It connects using the exact
settings from YAML, runs one read-only query, and verifies that
both configured report functions are visible to the configured role.
Connection, TLS, authentication, passfile, and permission errors are printed
to stderr. The command exits with a nonzero status on any failure.

To use the default configuration path, the shorter form is:

```sh
sudo -H -u nginx /usr/bin/python3 /opt/pgprofile-web/pgprofile_web.py \
  --check-connection
```

After a failure through Nginx, also inspect the service journal and SELinux
audit log:

```sh
journalctl -u fcgiwrap@nginx.service -n 100 --no-pager
sudo ausearch -m AVC -ts recent
```

## Local CGI smoke test

Create a configuration whose cache points to a writable directory, then run:

```sh
REQUEST_METHOD=GET \
SCRIPT_NAME=/pgprofile-web \
PGPROFILE_WEB_CONFIG=./config.example.yaml \
./pgprofile-web.cgi
```

This prints CGI headers followed by the HTML form. Generating an actual report
requires working database and credential settings.

## Behavior

- The default interval is the previous completed hour in the configured zone.
- Regular reports use Interval 1. Differential reports compare Interval 1 with
  Interval 2 using `get_diffreport(name, tstzrange, tstzrange)`.
- Interval 1 defaults to the previous completed hour. Interval 2 defaults to
  the completed hour immediately before Interval 1.
- Form input during DST gaps or ambiguous clock changes is rejected.
- Intervals longer than `max_interval_hours` are rejected.
- Identical intervals reuse the same cached report.
- A per-report filesystem lock prevents identical simultaneous requests from
  running duplicate PostgreSQL queries.
- Cached files may be deleted manually; PostgreSQL data is unaffected.
- Generated HTML is served with the embedded JavaScript required by modern
  `pg_profile` reports enabled. External scripts, network requests, plugins,
  framing, and form submissions remain blocked by Content Security Policy.

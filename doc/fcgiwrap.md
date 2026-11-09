# Deploy with Nginx and fcgiwrap on RHEL

This guide installs `pgprofile-web` behind Nginx using `fcgiwrap`. It covers the
packaged EPEL systemd units, custom fallback units, filesystem permissions,
SELinux, and the Nginx location configuration.

[Back to the project README](../README.md)

## Install and configure fcgiwrap

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

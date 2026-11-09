#!/usr/bin/env python3
"""CGI entry point for Nginx and fcgiwrap."""

from pgprofile_web import cgi_main


if __name__ == "__main__":
    raise SystemExit(cgi_main())

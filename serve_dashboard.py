#!/usr/bin/env python3
"""Serves dashboard.html, and nothing else, publicly on port 8080.

The page is re-read on every request, so the daily cron run shows up
without a restart. Runs as the bubble-dashboard systemd service.
"""
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PAGE = os.path.join(HERE, "dashboard.html")
PORT = int(os.environ.get("DASHBOARD_PORT", "8080"))
STALE_HOURS = 30  # the page is rebuilt daily; older than this means a run failed

# The page has no scripts, images or forms, so the browser is told to refuse all of them.
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; style-src 'unsafe-inline'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-cache",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "dashboard"  # don't advertise the Python version
    sys_version = ""
    timeout = 10  # drop connections that stall, so slow clients can't pile up

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/dashboard.html"):
            self.send_error(404)
            return
        try:
            with open(PAGE, "rb") as f:
                body = f.read()
            age = (time.time() - os.path.getmtime(PAGE)) / 3600
        except FileNotFoundError:
            self.send_error(503, "Dashboard not generated yet")
            return
        if age > STALE_HOURS:  # say so on the page itself, so nobody reads old numbers as today's
            banner = (f'<p style="border:2px solid #b00020;padding:8px;font-weight:700">This page was '
                      f'last rebuilt {age:.0f} hours ago: the daily update has failed, so these '
                      f'numbers may be out of date.</p>').encode()
            body = body.replace(b"<body>", b"<body>" + banner, 1)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass  # scanners hit public ports constantly; keep the journal quiet


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()

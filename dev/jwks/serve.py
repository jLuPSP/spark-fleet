"""Tiny static file server for the DEV_AUTH issuer.

Serves dev/auth/www (mounted at /www) with application/json for everything,
because the discovery document has no file extension and lua-resty-openidc
deserves a correct Content-Type. Stdlib only, python:3.12-slim base.
"""

import http.server


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory="/www", **kwargs)

    def guess_type(self, path):
        return "application/json"

    def log_message(self, fmt, *args):
        pass


if __name__ == "__main__":
    http.server.ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()

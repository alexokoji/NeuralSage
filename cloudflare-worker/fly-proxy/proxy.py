"""Tiny HTTP proxy — forwards signed Bybit testnet requests from Render.

Deploy to Fly.io (US East) so Bybit's CloudFront allows the connection:
  fly launch --name neuralsage-proxy --region iad --no-deploy
  fly secrets set PROXY_SECRET=neuraltrade-proxy-secret
  fly deploy
"""
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
import urllib.request
import urllib.error

BYBIT_BASE = "https://api-testnet.bybit.com"
SECRET = os.environ.get("PROXY_SECRET", "")
PORT = int(os.environ.get("PORT", 8080))


class ProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # suppress access logs

    def _auth(self):
        if SECRET and self.headers.get("X-Proxy-Secret") != SECRET:
            self.send_response(403)
            self.end_headers()
            self.wfile.write(b"Forbidden")
            return False
        return True

    def do_GET(self):
        if not self._auth():
            return
        self._proxy(None)

    def do_POST(self):
        if not self._auth():
            return
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else None
        self._proxy(body)

    def _proxy(self, body):
        upstream = BYBIT_BASE + self.path
        fwd_headers = {}
        for key in ("X-BAPI-API-KEY", "X-BAPI-SIGN", "X-BAPI-SIGN-TYPE",
                    "X-BAPI-TIMESTAMP", "X-BAPI-RECV-WINDOW", "Content-Type"):
            if self.headers.get(key):
                fwd_headers[key] = self.headers[key]

        req = urllib.request.Request(
            upstream, data=body, headers=fwd_headers, method=self.command
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(data)


if __name__ == "__main__":
    print(f"Bybit proxy listening on port {PORT}")
    HTTPServer(("0.0.0.0", PORT), ProxyHandler).serve_forever()

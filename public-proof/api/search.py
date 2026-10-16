"""Stateless same-origin endpoint for the bounded synthetic retrieval demo."""
from http.server import BaseHTTPRequestHandler
import json
from demo.live import run_query


class handler(BaseHTTPRequestHandler):
    def send_json(self, status, value):
        body = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.send_json(405, {"error": "Use POST"})

    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 4096:
                self.send_json(413, {"error": "Input must contain 1 to 4096 bytes"})
                return
            result = run_query(json.loads(self.rfile.read(size)))
        except (ValueError, TypeError, KeyError):
            self.send_json(400, {"error": "Invalid bounded retrieval inputs"})
            return
        except Exception:
            self.send_json(500, {"error": "Retrieval failed; no result is available"})
            return
        self.send_json(200, result)

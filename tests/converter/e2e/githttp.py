"""A smart-HTTP git server for the e2e: every bare repo under ROOT, clone and
push, no auth. Argo CD's repo-server and the portal both reach it over plain
http, the one transport a test cluster can use without credentials.

usage: githttp.py ROOT PORT [BIND]
"""
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = sys.argv[1]


class Handler(BaseHTTPRequestHandler):
    def run(self):
        path, _, qs = self.path.partition("?")
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        env = dict(os.environ, GIT_PROJECT_ROOT=ROOT, GIT_HTTP_EXPORT_ALL="1", PATH_INFO=path, QUERY_STRING=qs,
                   REQUEST_METHOD=self.command, CONTENT_TYPE=self.headers.get("Content-Type", ""),
                   CONTENT_LENGTH=str(len(body)), REMOTE_USER="e2e", REMOTE_ADDR=self.client_address[0],
                   HTTP_CONTENT_ENCODING=self.headers.get("Content-Encoding", ""))
        p = subprocess.run(["git", "http-backend"], input=body, env=env, capture_output=True)
        head, _, out = p.stdout.partition(b"\r\n\r\n")
        status, headers = 200, []
        for line in head.split(b"\r\n"):
            if not line:
                continue
            k, _, v = line.decode().partition(":")
            if k.lower() == "status":
                status = int(v.strip().split()[0])
            else:
                headers.append((k, v.strip()))
        self.send_response(status)
        for k, v in headers:
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    do_GET = do_POST = run

    def log_message(self, *args):
        pass


ThreadingHTTPServer((sys.argv[3] if len(sys.argv) > 3 else "0.0.0.0", int(sys.argv[2])), Handler).serve_forever()

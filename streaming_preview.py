"""Loopback-only preview panel; display updates never change keyboard focus."""
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import threading


class PreviewServer:
    def __init__(self, port=18767):
        self.token = secrets.token_urlsafe(24)
        self.lock = threading.Lock()
        self.data = {'phase': 'loading', 'message': '正在加载模型', 'preview': '', 'final': ''}
        owner = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.headers.get('Host') != f'127.0.0.1:{owner.server.server_port}':
                    self.send_error(403)
                    return
                origin = self.headers.get('Origin')
                if origin and origin != f'http://127.0.0.1:{owner.server.server_port}':
                    self.send_error(403)
                    return
                prefix = f'/{owner.token}/'
                if self.path == prefix:
                    payload = Path(__file__).with_name('streaming_preview.html').read_bytes()
                    mime = 'text/html; charset=utf-8'
                elif self.path == prefix+'status':
                    with owner.lock:
                        payload = json.dumps(owner.data, ensure_ascii=False).encode()
                    mime = 'application/json; charset=utf-8'
                else:
                    self.send_error(404)
                    return
                self.send_response(200)
                self.send_header('Content-Type', mime)
                self.send_header('Cache-Control', 'no-store')
                self.send_header('Referrer-Policy', 'no-referrer')
                self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f'http://127.0.0.1:{self.server.server_port}/{self.token}/'

    def start(self):
        self.thread.start()

    def publish(self, value):
        with self.lock:
            self.data = copy.deepcopy(value)

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from sessionpack import catalog, diagnostics
from sessionpack.paths import default_memory_root, ensure_layout
from sessionpack.summary import build_summary

WEB_ROOT = Path(__file__).resolve().parent / "web"
SESSION_ID_RE = catalog.SESSION_ID_RE
PAGES = {"/": "index.html", "/session": "session.html", "/diagnostics": "diagnostics.html"}
STATIC = {
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


def create_server(
    catalog_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    memory_root: Path | None = None,
) -> ThreadingHTTPServer:
    catalog_path = Path(catalog_path)
    memory_root = Path(memory_root) if memory_root else default_memory_root()
    actual_token = token or secrets.token_urlsafe(16)

    class Handler(BaseHTTPRequestHandler):
        server_version = "SessionPack/0.1"

        def log_message(self, fmt: str, *args) -> None:
            return

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def _authorized(self, query: dict) -> bool:
            supplied = self.headers.get("X-Token") or (query.get("token") or [""])[0]
            try:
                return secrets.compare_digest(supplied, actual_token)
            except TypeError:
                # compare_digest 拒绝含非 ASCII 的 str；按未授权处理而非崩溃
                return False

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            query = parse_qs(parsed.query)
            if path in PAGES:
                return self._page(PAGES[path])
            if path in STATIC:
                return self._static(*STATIC[path])
            if path.startswith("/api/"):
                if not self._authorized(query):
                    return self._json(HTTPStatus.FORBIDDEN, {"error": "token 无效"})
                return self._api(path, query)
            self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})

        def _page(self, name: str) -> None:
            self._send(HTTPStatus.OK, (WEB_ROOT / name).read_bytes(),
                       "text/html; charset=utf-8")

        def _static(self, name: str, content_type: str) -> None:
            self._send(HTTPStatus.OK, (WEB_ROOT / name).read_bytes(), content_type)

        def _api(self, path: str, query: dict) -> None:
            conn = catalog.connect(catalog_path)
            try:
                if path == "/api/sessions":
                    project = (query.get("project") or [None])[0]
                    term = (query.get("q") or [None])[0]
                    self._json(HTTPStatus.OK, {
                        "sessions": catalog.list_sessions(
                            conn, project_id=project, q=term, limit=200),
                        "projects": catalog.get_projects(conn),
                    })
                    return
                if path.startswith("/api/sessions/"):
                    session_id = path.rsplit("/", 1)[-1]
                    if not SESSION_ID_RE.match(session_id):
                        self._json(HTTPStatus.NOT_FOUND, {"error": "会话 ID 非法"})
                        return
                    session = catalog.get_session(conn, session_id)
                    if session is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "会话不存在"})
                        return
                    messages = catalog.get_messages(conn, session_id)
                    self._json(HTTPStatus.OK, {
                        "session": session,
                        "messages": messages,
                        "summary": build_summary(session, messages),
                    })
                    return
                if path == "/api/diagnostics":
                    layout = ensure_layout(catalog_path.parent)
                    self._json(HTTPStatus.OK, diagnostics.collect(memory_root, layout))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            finally:
                conn.close()

    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.token = actual_token
    return httpd


def serve(
    catalog_path: Path,
    *,
    port: int = 8765,
    token: str | None = None,
    open_browser: bool = False,
    memory_root: Path | None = None,
) -> None:
    httpd = create_server(catalog_path, port=port, token=token,
                          memory_root=memory_root)
    url = f"http://127.0.0.1:{httpd.server_address[1]}/?token={httpd.token}"
    print(f"SessionPack 已启动：{url}")
    print("按 Ctrl+C 停止。")
    if open_browser:
        threading.Timer(0.3, webbrowser.open, args=[url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()

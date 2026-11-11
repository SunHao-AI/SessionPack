from __future__ import annotations

import json
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlparse

from sessionpack import catalog, diagnostics, export
from sessionpack.paths import default_memory_root, ensure_layout
from sessionpack.share_images import resolve_image
from sessionpack.summary import build_summary

WEB_ROOT = Path(__file__).resolve().parent / "web"
PAGES = {"/": "index.html", "/session": "session.html", "/diagnostics": "diagnostics.html"}
STATIC = {
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}


class _TokenServer(ThreadingHTTPServer):
    """附带 token 属性的 ThreadingHTTPServer，供 serve() 拼启动 URL。"""

    token: str


def create_server(
    catalog_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    token: str | None = None,
    memory_root: Path | None = None,
) -> _TokenServer:
    catalog_path = Path(catalog_path)
    memory_root_path = Path(memory_root) if memory_root else default_memory_root()
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

        def _loopback_ok(self) -> bool:
            origin = self.headers.get("Origin")
            if origin:
                host = urlparse(origin).hostname
                if host not in ("127.0.0.1", "localhost", "::1"):
                    return False
            raw = self.headers.get("Host") or ""
            name, sep, port = raw.rpartition(":")
            if not sep:
                name, port = raw, ""
            if name not in ("127.0.0.1", "localhost", "[::1]", "::1"):
                return False
            if port and port != str(cast("tuple[str, int]", self.server.server_address)[1]):
                return False
            return True

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if not self._loopback_ok():
                self._json(HTTPStatus.FORBIDDEN, {"error": "forbidden"})
                return
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
            if path.startswith("/api/images/"):
                rel = path[len("/api/images/"):]
                target = resolve_image(catalog_path.parent / "images", rel)
                if target is None:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                    return
                ctype = IMAGE_TYPES.get(target.suffix.lower(),
                                        "application/octet-stream")
                self._send(HTTPStatus.OK, target.read_bytes(), ctype)
                return
            conn = catalog.connect(catalog_path)
            try:
                if path == "/api/sessions":
                    project = (query.get("project") or [None])[0]
                    term = (query.get("q") or [None])[0]
                    source = (query.get("source") or [None])[0]
                    date_from = (query.get("date_from") or [None])[0]
                    date_to = (query.get("date_to") or [None])[0]
                    self._json(HTTPStatus.OK, {
                        "sessions": catalog.list_sessions(
                            conn, project_id=project, source_kind=source,
                            date_from=date_from, date_to=date_to,
                            q=term, limit=200,
                        ),
                        "projects": catalog.get_projects(conn),
                    })
                    return
                if path == "/api/search":
                    term = (query.get("q") or [""])[0].strip()
                    if not term:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "缺少 q"})
                        return
                    try:
                        limit = int((query.get("limit") or ["50"])[0])
                    except ValueError:
                        limit = 50
                    limit = max(1, min(limit, 500))  # 与 cli run_search 同规则
                    rows = catalog.search_messages(conn, term, limit=limit)
                    sessions = catalog.search_sessions(conn, term, limit=limit)
                    self._json(HTTPStatus.OK, {
                        "query": term, "count": len(rows),
                        "hits": [{"session_id": r["session_id"],
                                  "message_id": r["message_id"],
                                  "body": r["body"]} for r in rows],
                        "session_hits": [
                            {"session_id": r["session_id"],
                             "title": r["title"], "summary": r["summary"]}
                            for r in sessions]})
                    return
                if path.startswith("/api/sessions/") and path.endswith("/export"):
                    session_id = path.rsplit("/", 2)[-2]
                    if not catalog.SESSION_ID_RE.fullmatch(session_id):
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    session = catalog.get_session(conn, session_id)
                    if session is None:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                        return
                    fmt = (query.get("format") or ["markdown"])[0]
                    if fmt not in ("markdown", "json"):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "bad format"})
                        return
                    messages = catalog.get_messages(conn, session_id)
                    try:
                        text = export.render(session, messages, fmt)
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "bad format"})
                        return
                    suffix = "json" if fmt == "json" else "md"
                    payload = text.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type",
                                     "application/json"
                                     if fmt == "json" else "text/markdown; charset=utf-8")
                    self.send_header("Content-Length", str(len(payload)))
                    self.send_header("Content-Disposition",
                                     f'attachment; filename="{session_id}.{suffix}"')
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if path.startswith("/api/sessions/"):
                    session_id = path.rsplit("/", 1)[-1]
                    if not catalog.SESSION_ID_RE.fullmatch(session_id):
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
                    self._json(HTTPStatus.OK, diagnostics.collect(memory_root_path, layout))
                    return
                self._json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            finally:
                conn.close()

    httpd = _TokenServer((host, port), Handler)
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

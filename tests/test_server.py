import http.client
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from sessionpack import archive, catalog
from sessionpack.adapters import TraeMemoryAdapter
from sessionpack.importer import import_source
from sessionpack.server import create_server


@pytest.fixture
def base_url(memory_tree: Path, tmp_path: Path):
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    server = create_server(db, port=0, token="test-token", memory_root=memory_tree)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _get(url: str, token: str | None = None) -> tuple[int, bytes]:
    request = urllib.request.Request(url)
    if token:
        request.add_header("X-Token", token)
    with urllib.request.urlopen(request, timeout=5) as response:
        return response.status, response.read()


def test_server_binds_loopback(base_url: str) -> None:
    assert base_url.startswith("http://127.0.0.1:")


def test_api_requires_token(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/api/sessions")
    assert exc.value.code == 403


def test_api_lists_sessions_with_header_token(base_url: str) -> None:
    status, body = _get(base_url + "/api/sessions", token="test-token")
    data = json.loads(body)
    assert status == 200
    assert len(data["sessions"]) == 2
    assert len(data["projects"]) == 2


def test_api_accepts_query_token(base_url: str) -> None:
    query = urllib.parse.quote("归档")
    status, body = _get(base_url + f"/api/sessions?token=test-token&q={query}")
    data = json.loads(body)
    assert status == 200
    assert len(data["sessions"]) == 1
    assert data["sessions"][0]["session_id"] == "a" * 24


def test_non_ascii_token_returns_403(base_url: str) -> None:
    request = urllib.request.Request(base_url + "/api/sessions",
                                     headers={"X-Token": "é"})
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        urllib.request.urlopen(request)
    assert excinfo.value.code == 403


def test_session_api_returns_summary_and_messages(base_url: str) -> None:
    status, body = _get(base_url + "/api/sessions/" + "a" * 24 + "?token=test-token")
    data = json.loads(body)
    assert status == 200
    assert data["summary"]["session_id"] == "a" * 24
    assert len(data["messages"]) == 2
    assert "实现归档包的校验流程" in data["summary"]["title"]


def test_session_api_rejects_bad_id(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/api/sessions/not-an-id?token=test-token")
    assert exc.value.code == 404


def test_diagnostics_api(base_url: str) -> None:
    status, body = _get(base_url + "/api/diagnostics?token=test-token")
    data = json.loads(body)
    assert status == 200
    assert data["adapter"]["id"] == "trae-cn-memory"
    assert data["catalog"]["stats"]["messages"] == 4


def test_pages_and_static_files_are_served(base_url: str) -> None:
    for path in ("/", "/session", "/diagnostics", "/app.js", "/style.css"):
        status, body = _get(base_url + path)
        assert status == 200
        assert body


def _request_with_host(base: str, host: str, token: str = "test-token"):
    from urllib.parse import urlsplit
    parts = urlsplit(base)
    conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=5)
    conn.request("GET", "/api/sessions?token=" + token, headers={"Host": host})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def test_host_header_validated(base_url) -> None:
    status, _ = _request_with_host(base_url, "evil.example.com")
    assert status == 403
    status, _ = _request_with_host(base_url, "localhost")
    assert status == 200


def test_origin_header_validated(base_url) -> None:
    from urllib.parse import urlsplit
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection("127.0.0.1", parts.port, timeout=5)
    conn.request("GET", "/api/sessions?token=test-token",
                 headers={"Origin": "http://evil.example.com"})
    assert conn.getresponse().status == 403
    conn.close()


def test_unknown_route_returns_404(base_url: str) -> None:
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(base_url + "/nope")
    assert exc.value.code == 404


def test_sessions_filter_params_passthrough(base_url) -> None:
    status, body = _get(f"{base_url}/api/sessions?source=trae-memory",
                        token="test-token")
    data = json.loads(body)
    assert status == 200 and len(data["sessions"]) == 2
    status, body = _get(f"{base_url}/api/sessions?source=none",
                        token="test-token")
    assert json.loads(body)["sessions"] == []
    status, body = _get(f"{base_url}/api/sessions?date_from=2026-01-02",
                        token="test-token")
    ids = {s["session_id"] for s in json.loads(body)["sessions"]}
    assert "b" * 24 in ids


def test_session_export_endpoint(base_url) -> None:
    sid = "a" * 24
    status, body = _get(f"{base_url}/api/sessions/{sid}/export?format=markdown",
                        token="test-token")
    assert status == 200
    assert "归档" in body.decode("utf-8")
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(f"{base_url}/api/sessions/{sid}/export?format=bogus",
             token="test-token")
    assert exc.value.code == 400


def test_archive_to_serve_search_roundtrip(
    memory_tree: Path, tmp_path: Path
) -> None:
    db = tmp_path / "catalog.sqlite"
    import_source(TraeMemoryAdapter(), memory_tree, db)
    conn = catalog.connect(db)
    project = catalog.get_projects(conn)[0]
    built = archive.build_archive(conn, tmp_path / "a", project["project_id"])
    fresh_path = tmp_path / "fresh.sqlite"
    fresh = catalog.connect(fresh_path)
    catalog.init_schema(fresh)
    archive.import_archive(fresh, built.path)
    srv = create_server(fresh_path, port=0, token="test-token")
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        status, body = _get(f"{base}/api/sessions?q=sha256", token="test-token")
        assert status == 200
        ids = {s["session_id"] for s in json.loads(body)["sessions"]}
        assert "a" * 24 in ids
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def test_export_session_content_type_utf8(base_url: str) -> None:
    request = urllib.request.Request(
        base_url + "/api/sessions/" + "a" * 24 + "/export?format=markdown"
    )
    request.add_header("X-Token", "test-token")
    with urllib.request.urlopen(request, timeout=5) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/markdown; charset=utf-8"
        assert response.read().decode("utf-8")

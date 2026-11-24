"""Local web server for u3dconv (Stage 3: Web UI).

Serves webui.html and a small JSON API using only the Python standard library,
so it can run inside an iOS app container (BeeWare Briefcase, or a Swift
wrapper that loads the URL in a WKWebView) as well as on a desktop.

Security model
--------------
* The server binds to 127.0.0.1 by default.
* Every request must carry the random access token generated at startup, either
  in the X-U3D-Token header or in a ?t= query parameter (used by <img> tags and
  download links). Web pages on other sites cannot learn the token, so they
  cannot drive the server. Open the full URL printed at startup (it ends with
  ?t=...).
* Uploaded files live in a temporary workspace, one folder per session, and are
  deleted when the session is closed, when it expires, or when the server stops.
* Binding to a non-loopback address (for example 0.0.0.0 to reach it from a phone
  on your Wi-Fi) exposes the server to your network. Traffic is not encrypted.
  Anyone who has the URL can use it, so share it only with yourself.

Use embedded in an app:

    handle = start_server(port=0, workspace=documents_dir + "/u3dconv")
    webview.load(handle.url)
    ...
    handle.stop()
"""

from __future__ import annotations

import hmac
import io
import json
import os
import re
import secrets
import shutil
import socket
import sys
import tempfile
import threading
import time
import webbrowser
import zipfile
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional
from urllib.parse import parse_qs, urlparse

from . import console
from .bundle import (
    BundleError,
    count_by_type,
    list_objects,
    load_environment,
    read_unityfs_header,
    repack_bundle,
    sanitize_name,
)
from .constants import (
    DEFAULT_MAX_UPLOAD_MB,
    DEFAULT_PACKER,
    DEFAULT_PORT,
    PACKER_CHOICES,
)
from .textures import (
    TextureError,
    collect_png_files,
    export_textures,
    list_textures,
    parse_path_id,
    resolve_texture_format,
)
from .tree import tree_to_dict

HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "webui.html")

MAX_PNG_BYTES = 64 * 1024 * 1024
MAX_JSON_BYTES = 64 * 1024
SESSION_TTL_SECONDS = 6 * 60 * 60
MAX_SESSIONS = 8
THUMB_SIZE = (256, 256)
CHUNK = 1024 * 1024

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


class HttpError(Exception):
    """An error that maps directly to an HTTP status and a JSON message."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def default_workspace() -> str:
    """Folder for temporary session data.

    On iOS, pass a path under the app's tmp/ or Documents/ directory instead.
    """
    return os.environ.get("U3DCONV_WORKSPACE") or os.path.join(
        tempfile.gettempdir(), "u3dconv_web"
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

@dataclass
class Session:
    """One uploaded bundle plus its staged PNGs and outputs."""

    id: str
    root: str
    filename: str
    src_path: str
    created: float = field(default_factory=time.time)
    lock: threading.RLock = field(default_factory=threading.RLock)
    env: object = None
    objects: list = field(default_factory=list)
    counts: dict = field(default_factory=dict)
    tex_objs: dict = field(default_factory=dict)
    tex_infos: list = field(default_factory=list)
    output_path: Optional[str] = None

    @property
    def pngs_dir(self) -> str:
        return os.path.join(self.root, "pngs")

    @property
    def out_dir(self) -> str:
        return os.path.join(self.root, "out")

    @property
    def thumbs_dir(self) -> str:
        return os.path.join(self.root, "thumbs")

    @property
    def export_dir(self) -> str:
        return os.path.join(self.root, "export")

    def load(self) -> None:
        """Open the bundle once and cache what the UI needs."""
        with self.lock:
            if self.env is not None:
                return
            env = load_environment(self.src_path)
            self.objects = list_objects(env)
            self.counts = count_by_type(self.objects)
            self.tex_objs = {
                obj.path_id: obj
                for obj in env.objects
                if obj.type.name == "Texture2D"
            }
            self.tex_infos = list_textures(env)
            self.env = env

    def staged_files(self) -> List[str]:
        if not os.path.isdir(self.pngs_dir):
            return []
        return sorted(os.listdir(self.pngs_dir))

    def staged_ids(self) -> set:
        ids = set()
        for name in self.staged_files():
            pid = parse_path_id(os.path.splitext(name)[0])
            if pid is not None:
                ids.add(pid)
        return ids


class App:
    """Shared server state: configuration and the session table."""

    def __init__(self, workspace: str, token: str, max_upload: int, verbose: bool):
        self.workspace = os.path.abspath(workspace)
        self.token = token
        self.max_upload = max_upload
        self.verbose = verbose
        self.sessions: Dict[str, Session] = {}
        self.lock = threading.Lock()
        os.makedirs(self.workspace, exist_ok=True)
        self._remove_stale_folders()

    def _remove_stale_folders(self) -> None:
        """Delete leftovers from earlier runs that are older than the TTL."""
        cutoff = time.time() - SESSION_TTL_SECONDS
        for name in os.listdir(self.workspace):
            path = os.path.join(self.workspace, name)
            if SESSION_ID_RE.match(name) and os.path.isdir(path):
                try:
                    if os.path.getmtime(path) < cutoff:
                        shutil.rmtree(path, ignore_errors=True)
                except OSError:
                    pass

    def _expire(self) -> None:
        """Drop expired sessions, then the oldest ones beyond MAX_SESSIONS."""
        now = time.time()
        for sid in [s for s, v in self.sessions.items()
                    if now - v.created > SESSION_TTL_SECONDS]:
            self._drop(sid)
        while len(self.sessions) >= MAX_SESSIONS:
            oldest = min(self.sessions.values(), key=lambda v: v.created)
            self._drop(oldest.id)

    def _drop(self, sid: str) -> None:
        session = self.sessions.pop(sid, None)
        if session is not None:
            shutil.rmtree(session.root, ignore_errors=True)

    def new_session(self, filename: str) -> Session:
        with self.lock:
            self._expire()
            sid = secrets.token_urlsafe(12)
            root = os.path.join(self.workspace, sid)
            for sub in ("pngs", "out", "thumbs", "export"):
                os.makedirs(os.path.join(root, sub), exist_ok=True)
            safe_name = sanitize_name(os.path.splitext(filename)[0])
            ext = re.sub(r"[^A-Za-z0-9.]", "", os.path.splitext(filename)[1])[:12]
            src_path = os.path.join(root, f"{safe_name}{ext}")
            session = Session(id=sid, root=root, filename=safe_name + ext,
                              src_path=src_path)
            self.sessions[sid] = session
            return session

    def get(self, sid: str) -> Session:
        if not SESSION_ID_RE.match(sid):
            raise HttpError(404, "Unknown session")
        with self.lock:
            session = self.sessions.get(sid)
        if session is None:
            raise HttpError(404, "Unknown or expired session; open the file again")
        return session

    def delete(self, sid: str) -> None:
        with self.lock:
            self._drop(sid)

    def close(self) -> None:
        with self.lock:
            for sid in list(self.sessions):
                self._drop(sid)


# ---------------------------------------------------------------------------
# API logic (kept separate from HTTP plumbing so it is easy to read and test)
# ---------------------------------------------------------------------------

def _header_dict(path: str) -> Optional[dict]:
    try:
        header = read_unityfs_header(path)
    except (OSError, BundleError):
        return None
    if header is None:
        return None
    return {
        "version": header.version,
        "unity_version": header.unity_version,
        "unity_revision": header.unity_revision,
        "bundle_size": header.bundle_size,
        "compression": header.compression,
        "compressed_blocks_info_size": header.compressed_blocks_info_size,
        "uncompressed_blocks_info_size": header.uncompressed_blocks_info_size,
    }


def session_summary(session: Session) -> dict:
    return {
        "id": session.id,
        "filename": session.filename,
        "size": os.path.getsize(session.src_path),
        "header": _header_dict(session.src_path),
        "object_count": len(session.objects),
        "counts": session.counts,
        "texture_count": len(session.tex_infos),
        "staged": session.staged_files(),
        "output_name": os.path.basename(session.output_path)
        if session.output_path and os.path.isfile(session.output_path)
        else None,
    }


def texture_list(session: Session) -> List[dict]:
    staged = session.staged_ids()
    return [
        {
            "path_id": t.path_id,
            "name": t.name,
            "format": t.format_name,
            "width": t.width,
            "height": t.height,
            "mips": t.mip_count,
            "replaced": t.path_id in staged,
        }
        for t in session.tex_infos
    ]


def texture_png_bytes(session: Session, path_id: int, thumb: bool) -> bytes:
    with session.lock:
        obj = session.tex_objs.get(path_id)
        if obj is None:
            raise HttpError(404, "No such texture")
        cache_path = os.path.join(session.thumbs_dir, f"{path_id}.png")
        if thumb and os.path.isfile(cache_path):
            with open(cache_path, "rb") as fh:
                return fh.read()
        try:
            image = obj.read().image
            if image is None:
                raise ValueError("decoded to nothing")
            if thumb:
                image = image.copy()
                image.thumbnail(THUMB_SIZE)
            buf = io.BytesIO()
            image.save(buf, "PNG")
        except Exception as exc:
            raise HttpError(422, f"Could not decode this texture: {exc}") from exc
        data = buf.getvalue()
        if thumb:
            with open(cache_path, "wb") as fh:
                fh.write(data)
        return data


def stage_png(session: Session, filename: str, data: bytes) -> str:
    """Store an uploaded PNG for the next repack. Returns the stored name."""
    if not data.startswith(PNG_SIGNATURE):
        raise HttpError(400, "That file is not a PNG image")
    stem = sanitize_name(os.path.splitext(filename)[0])
    stored = stem + ".png"
    path_id = parse_path_id(stem)
    with session.lock:
        # One staged file per texture: a new upload replaces the old one.
        if path_id is not None:
            for existing in session.staged_files():
                if parse_path_id(os.path.splitext(existing)[0]) == path_id:
                    os.remove(os.path.join(session.pngs_dir, existing))
        with open(os.path.join(session.pngs_dir, stored), "wb") as fh:
            fh.write(data)
    return stored


def run_repack(session: Session, options: dict) -> dict:
    packer = options.get("packer") or DEFAULT_PACKER
    if packer not in PACKER_CHOICES:
        raise HttpError(400, f"Unknown packer '{packer}'")

    target_format = None
    format_name = (options.get("format") or "").strip()
    if format_name:
        target_format = resolve_texture_format(format_name)  # TextureError -> 400

    if not session.staged_files():
        raise HttpError(400, "Stage at least one PNG before repacking")

    base, ext = os.path.splitext(session.filename)
    out_path = os.path.join(session.out_dir, f"{base}_repacked{ext}")

    with session.lock:
        result = repack_bundle(
            session.src_path,
            out_path,
            session.pngs_dir,
            packer=packer,
            target_format=target_format,
            allow_resize=bool(options.get("allow_resize", False)),
            fallback=bool(options.get("fallback", True)),
            overwrite=True,
        )
        session.output_path = out_path

    report = result.import_report
    return {
        "output_name": os.path.basename(out_path),
        "output_size": result.output_size,
        "compression": result.verify.header.compression,
        "object_count": result.verify.object_count,
        "unreadable": result.verify.unreadable[:20],
        "failed": report.failed,
        "replaced": report.replaced,
        "results": [
            {"path_id": r.path_id, "name": r.name, "status": r.status, "detail": r.detail}
            for r in report.results
        ],
        "unmatched": report.unmatched_files,
    }


def _zip_entries(zip_path: str, entries: List[tuple]) -> None:
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, arcname in entries:
            zf.write(path, arcname)


def build_package_zip(session: Session, include_pngs: bool) -> str:
    if not session.output_path or not os.path.isfile(session.output_path):
        raise HttpError(404, "Nothing repacked yet")
    entries = [(session.output_path, os.path.basename(session.output_path))]
    if include_pngs:
        for path in collect_png_files(session.pngs_dir):
            entries.append((path, "textures/" + os.path.basename(path)))
    zip_path = os.path.join(session.export_dir, "package.zip")
    _zip_entries(zip_path, entries)
    return zip_path


def build_textures_zip(session: Session) -> str:
    with session.lock:
        folder = os.path.join(session.export_dir, "textures")
        shutil.rmtree(folder, ignore_errors=True)
        report = export_textures(session.env, folder)
        if not report.written:
            detail = report.errors[0] if report.errors else "no textures in this file"
            raise HttpError(404, f"Nothing to export: {detail}")
        zip_path = os.path.join(session.export_dir, "textures.zip")
        _zip_entries(
            zip_path,
            [(os.path.join(folder, name), name) for name in report.written],
        )
    return zip_path


# ---------------------------------------------------------------------------
# HTTP plumbing
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "u3dconv"
    # HTTP/1.0 (the default) closes the connection after each response, which
    # keeps error handling simple: an unread upload body can never confuse the
    # next request.

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # quiet unless --verbose
        if self.app.verbose:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- responses ---------------------------------------------------------

    def _common_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")

    def _send_json(self, status: int, payload) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_bytes(self, data: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: str, download_name: str) -> None:
        size = os.path.getsize(path)
        safe = sanitize_name(os.path.splitext(download_name)[0]) + re.sub(
            r"[^A-Za-z0-9.]", "", os.path.splitext(download_name)[1]
        )
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition", f'attachment; filename="{safe}"')
        self._common_headers()
        self.end_headers()
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _send_index(self) -> None:
        try:
            with open(HTML_PATH, "rb") as fh:
                html = fh.read()
        except OSError as exc:
            raise HttpError(500, f"webui.html is missing: {exc}") from exc
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(html)))
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:",
        )
        self._common_headers()
        self.end_headers()
        self.wfile.write(html)

    # -- request helpers ---------------------------------------------------

    def _authorized(self, query: dict) -> bool:
        supplied = self.headers.get("X-U3D-Token") or (query.get("t") or [""])[0]
        return hmac.compare_digest(supplied.encode(), self.app.token.encode())

    def _content_length(self, limit: int) -> int:
        raw = self.headers.get("Content-Length")
        if raw is None:
            raise HttpError(411, "Content-Length is required")
        try:
            length = int(raw)
        except ValueError:
            raise HttpError(400, "Bad Content-Length")
        if length < 0:
            raise HttpError(400, "Bad Content-Length")
        if length > limit:
            raise HttpError(
                413, f"File is too large (limit {limit // (1024 * 1024)} MB)"
            )
        return length

    def _read_body(self, limit: int) -> bytes:
        length = self._content_length(limit)
        data = self.rfile.read(length)
        if len(data) != length:
            raise HttpError(400, "Upload was cut off")
        return data

    def _stream_body_to(self, dest: str, limit: int) -> int:
        length = self._content_length(limit)
        part = dest + ".part"
        received = 0
        try:
            with open(part, "wb") as fh:
                while received < length:
                    chunk = self.rfile.read(min(CHUNK, length - received))
                    if not chunk:
                        raise HttpError(400, "Upload was cut off")
                    fh.write(chunk)
                    received += len(chunk)
            os.replace(part, dest)
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except OSError:
                    pass
        return received

    # -- dispatch ----------------------------------------------------------

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            path = parsed.path

            if not self._authorized(query):
                raise HttpError(
                    403,
                    "Missing or invalid access token. Open the full link that "
                    "u3dconv printed (it ends with ?t=...).",
                )

            if method == "GET" and path == "/":
                return self._send_index()
            if method == "POST" and path == "/api/upload":
                return self._api_upload(query)

            match = re.fullmatch(r"/api/session/([^/]+)(?:/(.*))?", path)
            if match:
                session = self.app.get(match.group(1))
                return self._api_session(method, session, match.group(2) or "", query)

            raise HttpError(404, "Not found")

        except HttpError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except (BundleError, TextureError) as exc:
            self._send_json(400, {"error": str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser went away; nothing to report
        except Exception as exc:  # never leak a traceback to the browser
            if self.app.verbose:
                import traceback
                traceback.print_exc()
            self._send_json(500, {"error": f"Internal error: {exc}"})

    # -- routes ------------------------------------------------------------

    def _api_upload(self, query: dict) -> None:
        filename = (query.get("name") or ["upload.bin"])[0] or "upload.bin"
        session = self.app.new_session(filename)
        try:
            self._stream_body_to(session.src_path, self.app.max_upload)
            session.load()
        except BaseException:
            self.app.delete(session.id)
            raise
        self._send_json(200, session_summary(session))

    def _api_session(self, method: str, session: Session, rest: str, query: dict) -> None:
        if rest == "":
            if method == "GET":
                session.load()
                return self._send_json(200, session_summary(session))
            if method == "DELETE":
                self.app.delete(session.id)
                return self._send_json(200, {"deleted": True})

        session.load()

        if rest == "objects" and method == "GET":
            wanted = {t.lower() for t in query.get("type", [])}
            objs = [o for o in session.objects if not wanted or o.type_name.lower() in wanted]
            return self._send_json(200, tree_to_dict(objs))

        if rest == "textures" and method == "GET":
            return self._send_json(200, texture_list(session))

        match = re.fullmatch(r"texture/(-?\d+)\.png", rest)
        if match and method == "GET":
            thumb = (query.get("size") or ["full"])[0] == "thumb"
            data = texture_png_bytes(session, int(match.group(1)), thumb)
            return self._send_bytes(data, "image/png")

        match = re.fullmatch(r"texture/(-?\d+)/replace", rest)
        if match and method == "POST":
            path_id = int(match.group(1))
            if path_id not in session.tex_objs:
                raise HttpError(404, "No such texture")
            data = self._read_body(MAX_PNG_BYTES)
            stored = stage_png(session, f"texture_{path_id}.png", data)
            return self._send_json(200, {"saved": stored, "staged": session.staged_files()})

        if rest == "png" and method == "POST":
            name = (query.get("name") or ["image.png"])[0]
            data = self._read_body(MAX_PNG_BYTES)
            stored = stage_png(session, name, data)
            return self._send_json(200, {"saved": stored, "staged": session.staged_files()})

        if rest == "pngs" and method == "GET":
            return self._send_json(200, {"staged": session.staged_files()})

        if rest == "pngs" and method == "DELETE":
            with session.lock:
                shutil.rmtree(session.pngs_dir, ignore_errors=True)
                os.makedirs(session.pngs_dir, exist_ok=True)
            return self._send_json(200, {"staged": []})

        if rest == "repack" and method == "POST":
            try:
                options = json.loads(self._read_body(MAX_JSON_BYTES) or b"{}")
            except ValueError:
                raise HttpError(400, "Request body must be JSON")
            if not isinstance(options, dict):
                raise HttpError(400, "Request body must be a JSON object")
            return self._send_json(200, run_repack(session, options))

        if rest == "download/bundle" and method == "GET":
            if not session.output_path or not os.path.isfile(session.output_path):
                raise HttpError(404, "Nothing repacked yet")
            return self._send_file(session.output_path, os.path.basename(session.output_path))

        if rest == "download/package.zip" and method == "GET":
            include = (query.get("include_pngs") or ["0"])[0] == "1"
            return self._send_file(build_package_zip(session, include), "u3dconv_package.zip")

        if rest == "download/textures.zip" and method == "GET":
            return self._send_file(build_textures_zip(session), "u3dconv_textures.zip")

        raise HttpError(404, "Not found")


# ---------------------------------------------------------------------------
# Starting and stopping
# ---------------------------------------------------------------------------

class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: App


class _Server6(_Server):
    address_family = socket.AF_INET6


@dataclass
class ServerHandle:
    server: _Server
    thread: threading.Thread
    app: App
    url: str
    token: str

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.app.close()


def start_server(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    workspace: Optional[str] = None,
    max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB,
    token: Optional[str] = None,
    verbose: bool = False,
) -> ServerHandle:
    """Start the server in a background thread and return a handle.

    Pass port=0 to let the OS choose a free port; the real port is in handle.url.
    Suitable for embedding in an iOS app container.
    """
    token = token or secrets.token_urlsafe(18)
    app = App(
        workspace or default_workspace(),
        token,
        max(1, int(max_upload_mb)) * 1024 * 1024,
        verbose,
    )
    server_class = _Server6 if ":" in host else _Server
    server = server_class((host, port), Handler)
    server.app = app

    thread = threading.Thread(target=server.serve_forever, name="u3dconv-web", daemon=True)
    thread.start()

    shown = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    if ":" in shown:
        shown = f"[{shown}]"
    url = f"http://{shown}:{server.server_address[1]}/?t={token}"
    return ServerHandle(server=server, thread=thread, app=app, url=url, token=token)


def serve(
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    workspace: Optional[str] = None,
    max_upload_mb: int = DEFAULT_MAX_UPLOAD_MB,
    open_browser: bool = False,
    verbose: bool = False,
) -> int:
    """Run the web UI until interrupted (used by `u3dconv web`)."""
    try:
        handle = start_server(host, port, workspace, max_upload_mb, verbose=verbose)
    except OSError as exc:
        console.error(f"Could not start the web server on {host}:{port}: {exc}")
        return 1

    console.ok("u3dconv web UI is running. Open this link:")
    print(f"\n  {handle.url}\n")
    console.heading(f"Workspace: {handle.app.workspace}")
    if host not in ("127.0.0.1", "localhost", "::1"):
        console.warn(
            "This server is reachable from your network and is not encrypted. "
            "On another device, replace 127.0.0.1 in the link with this "
            "computer's address. Only share the link with yourself."
        )
    print("Press Ctrl+C to stop. Uploaded files are deleted on exit.")

    if open_browser:
        webbrowser.open(handle.url)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        handle.stop()
    console.ok("Stopped.")
    return 0

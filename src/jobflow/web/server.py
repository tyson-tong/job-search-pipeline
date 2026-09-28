"""A small local-only HTTP server for the web app (jobflow web).

It listens on 127.0.0.1 only. Each start creates a random token that the page sends
with every API call, and requests must name this machine as the host, so other
websites open in your browser can't read or change your jobs.
"""
from __future__ import annotations

import json
import re
import secrets
import sys
from datetime import date, datetime
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.parse import parse_qs, urlsplit

from . import queries, scoring

MAX_BODY = 1_000_000


def _json_default(value: object):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"Not JSON serialisable: {type(value).__name__}")


def make_handler(token: str, port: int):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    page = (resources.files("jobflow.web") / "static" / "index.html").read_text()
    page = page.replace("__JOBFLOW_TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        server_version = "jobflow"

        def log_request(self, code="-", size="-") -> None:
            # Quiet for successful requests; show errors in the terminal.
            if str(int(code) if str(code).isdigit() else code)[:1] in ("4", "5"):
                sys.stderr.write(f"{self.command} {urlsplit(self.path).path} -> {code}\n")

        def _send(self, status: int, body: bytes, content_type: str,
                  extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, payload: object, status: int = 200) -> None:
            self._send(status, json.dumps(payload, default=_json_default).encode(),
                       "application/json")

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message}, status)

        def _allowed(self, query: dict) -> bool:
            if self.headers.get("Host") not in allowed_hosts:
                self._error(HTTPStatus.FORBIDDEN, "Unexpected host")
                return False
            supplied = self.headers.get("X-Jobflow-Token") or (query.get("token") or [""])[0]
            if not secrets.compare_digest(supplied, token):
                self._error(HTTPStatus.FORBIDDEN, "Missing or wrong token; reload the page")
                return False
            return True

        def do_GET(self) -> None:  # noqa: N802 - name from the base class
            url = urlsplit(self.path)
            query = parse_qs(url.query)
            if url.path == "/":
                if self.headers.get("Host") not in allowed_hosts:
                    return self._error(HTTPStatus.FORBIDDEN, "Unexpected host")
                # No framing: another site could otherwise load the tracker in a hidden
                # frame and trick you into clicking Save (clickjacking).
                return self._send(200, page.encode(), "text/html; charset=utf-8",
                                  {"Content-Security-Policy":
                                   "default-src 'self'; style-src 'unsafe-inline'; "
                                   "script-src 'unsafe-inline'; img-src 'self' data:; "
                                   "frame-ancestors 'none'",
                                   "X-Frame-Options": "DENY"})
            if not url.path.startswith("/api/"):
                return self._error(HTTPStatus.NOT_FOUND, "Not found")
            if not self._allowed(query):
                return None
            try:
                if url.path == "/api/summary":
                    return self._json(queries.summary())
                if url.path == "/api/scoring":
                    return self._json(scoring.status())
                if url.path == "/api/searches":
                    from ..ingest.report import describe_searches, reference
                    return self._json({**describe_searches(), "reference": reference()})
                if url.path == "/api/countries":
                    return self._json(queries.countries(queries.parse_filters(query)))
                if url.path == "/api/jobs":
                    return self._json(queries.list_jobs(queries.parse_filters(query)))
                if url.path == "/api/export.csv":
                    # With id=..., only those jobs: the rows on the page being viewed.
                    ids = ({int(i) for i in query["id"] if i.isdigit()} if "id" in query else None)
                    body = queries.export_csv(queries.parse_filters(query), ids).encode()
                    name = f"jobs-{'filtered-' if ids is not None else ''}{date.today()}.csv"
                    return self._send(200, body, "text/csv; charset=utf-8",
                                      {"Content-Disposition": f'attachment; filename="{name}"'})
                match = re.fullmatch(r"/api/jobs/(\d+)", url.path)
                if match:
                    job = queries.job_detail(int(match[1]))
                    return self._json(job) if job else self._error(404, "No such job")
                match = re.fullmatch(r"/api/jobs/(\d+)/duplicates", url.path)
                if match:
                    from ..ingest.store import possible_duplicates
                    return self._json(possible_duplicates(int(match[1])))
                return self._error(HTTPStatus.NOT_FOUND, "Not found")
            except Exception as exc:  # report, keep serving
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

        def do_POST(self) -> None:  # noqa: N802 - name from the base class
            url = urlsplit(self.path)
            if not self._allowed({}):
                return None
            if url.path not in ("/api/save", "/api/scoring"):
                return self._error(HTTPStatus.NOT_FOUND, "Not found")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Send JSON")
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0:
                return self._error(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
            if length > MAX_BODY:
                return self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request too large")
            if url.path == "/api/scoring":
                return self._start_scoring(length)
            try:
                payload = json.loads(self.rfile.read(length) or b"null")
                if not isinstance(payload, dict):
                    raise queries.InvalidChange("Send an object with changes")
                saved = queries.save_changes(payload.get("changes"),
                                             force=payload.get("force") is True)
            except (json.JSONDecodeError, queries.InvalidChange) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except queries.Conflict as exc:
                return self._json({"error": str(exc), "conflicts": exc.conflicts},
                                  HTTPStatus.CONFLICT)
            except Exception as exc:  # the transaction was rolled back
                return self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"Nothing was saved: {exc}")
            return self._json({"saved": saved})

        def _start_scoring(self, length: int) -> None:
            try:
                job_ids = scoring.parse_request(json.loads(self.rfile.read(length) or b"null"))
                return self._json(scoring.start(job_ids))
            except (json.JSONDecodeError, ValueError) as exc:
                return self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except scoring.Busy as exc:
                return self._error(HTTPStatus.CONFLICT, str(exc))
            except RuntimeError as exc:  # LLM or CV configuration
                return self._error(HTTPStatus.UNPROCESSABLE_ENTITY, str(exc))

    return Handler


def serve(port: int = 8765) -> None:
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(token, port))
    print(f"jobflow web is running at http://127.0.0.1:{port}/  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()

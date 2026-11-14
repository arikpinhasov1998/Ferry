"""Local HTTP/JSON API of the background service (127.0.0.1 only, token protected)."""
from __future__ import annotations

import hmac
import json
import os
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from ferry import __version__
from ferry.config import Config, platform_label
from ferry.spec import JobSpec


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _mask(cfg: dict) -> dict:
    c = json.loads(json.dumps(cfg))
    tok = c.get("telegram", {}).get("token")
    if tok:
        c["telegram"]["token"] = tok[:6] + "…" + tok[-4:]
    c.get("api", {}).pop("token", None)
    return c


class _FastBindServer(ThreadingHTTPServer):
    """HTTPServer.server_bind calls socket.getfqdn(), a reverse DNS lookup that can
    stall for tens of seconds on some machines (macOS especially). We never need it."""

    def server_bind(self):
        import socketserver
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port


class ApiServer:
    def __init__(self, daemon):
        self.daemon = daemon
        cfg = daemon.config
        host, port = cfg.get("api.host", "127.0.0.1"), int(cfg.get("api.port"))
        token = cfg.get("api.token")
        api = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"Ferry/{__version__}"

            def log_message(self, *args):
                pass

            def _send(self, status, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body, default=str).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method):
                auth = self.headers.get("Authorization", "")
                if not hmac.compare_digest(auth, f"Bearer {token}"):
                    return self._send(401, {"error": "unauthorized"})
                url = urlparse(self.path)
                body = {}
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    try:
                        body = json.loads(self.rfile.read(n))
                    except ValueError:
                        return self._send(400, {"error": "invalid JSON"})
                try:
                    result = api.route(method, url.path.rstrip("/"), parse_qs(url.query), body)
                    if isinstance(result, tuple):
                        return self._send(200, result[0], result[1])
                    return self._send(200, result)
                except ApiError as e:
                    return self._send(e.status, {"error": str(e)})
                except KeyError as e:
                    return self._send(404, {"error": str(e).strip("'\"")})
                except (ValueError, RuntimeError) as e:
                    return self._send(400, {"error": str(e)})
                except Exception as e:  # pragma: no cover
                    daemon.log.exception("API error")
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def do_DELETE(self):
                self._handle("DELETE")

        self.httpd = _FastBindServer((host, port), Handler)
        self.httpd.daemon_threads = True

    def serve(self):
        t = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="ferry-api")
        t.start()
        return t

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ----------------------------------------------------------- routing --
    def route(self, method, path, query, body):
        d = self.daemon
        m = d.manager
        parts = [p for p in path.split("/") if p][1:] if path.startswith("/api") else []
        if not parts:
            raise ApiError(404, "not found")
        head = parts[0]

        if head == "ping":
            return {"ok": True, "version": __version__, "machine": d.config.get("machine_name"),
                    "pid": os.getpid(), "platform": platform_label(), "started": d.started}
        if head == "status":
            return d.status_snapshot()
        if head == "runs":
            if len(parts) == 1:
                return [r.info() for r in m.all_runs()]
            key = parts[1]
            if len(parts) == 2:
                r = m.get(key)
                if not r:
                    raise KeyError(f"no such run: {key}")
                return r.info()
            action = parts[2]
            if action == "failures":
                return m.failures(key, int(query.get("limit", ["1000"])[0]))
            if action == "log":
                p = m.log_path(key)
                if not p:
                    raise KeyError("no log for this run")
                tail = int(query.get("tail", ["0"])[0])
                data = p.read_bytes()
                if tail:
                    data = b"\n".join(data.splitlines()[-tail:])
                return data, "text/plain; charset=utf-8"
            if method != "POST":
                raise ApiError(405, "use POST")
            if action == "pause":
                return m.pause(key).info()
            if action == "resume":
                return m.resume(key).info()
            if action == "cancel":
                return m.cancel(key).info()
            if action == "confirm":
                return m.confirm(key, body.get("choice", "all")).info()
            raise ApiError(404, "unknown action")
        if head == "run" and method == "POST":
            spec = JobSpec.from_dict(body["spec"])
            return m.start(spec, trigger=body.get("trigger", "manual")).info()
        if head == "jobs":
            if len(parts) == 1:
                if method == "POST":
                    job = JobSpec.from_dict(body)
                    m.jobs.put(job)
                    d.refresh_watchers()
                    return job.to_dict()
                return [j.to_dict() for j in m.jobs.list()]
            key = parts[1]
            if len(parts) == 2:
                if method == "DELETE":
                    if not m.jobs.delete(key):
                        raise KeyError(f"no such job: {key}")
                    d.refresh_watchers()
                    return {"ok": True}
                j = m.jobs.get(key)
                if not j:
                    raise KeyError(f"no such job: {key}")
                return j.to_dict()
            if parts[2] == "run" and method == "POST":
                return m.start_job(key, trigger=body.get("trigger", "manual"),
                                   dry_run=bool(body.get("dry_run"))).info()
            raise ApiError(404, "unknown job action")
        if head == "history":
            return m.history(int(query.get("limit", ["50"])[0]))
        if head == "pause_all" and method == "POST":
            m.pause_all()
            return {"ok": True}
        if head == "resume_all" and method == "POST":
            m.resume_all()
            return {"ok": True}
        if head == "config":
            if method == "POST":
                for k, v in body.items():
                    if k.startswith("api."):
                        raise ApiError(400, "api settings can only be changed in the config file")
                    d.config.set(k, v)
                d.reload_telegram()
            return _mask(d.config.data)
        if head == "telegram" and len(parts) > 1 and parts[1] == "test" and method == "POST":
            return d.telegram_test()
        if head == "shutdown" and method == "POST":
            threading.Timer(0.2, d.stop).start()
            return {"ok": True}
        raise ApiError(404, "not found")


class Client:
    """Talks to the running daemon."""

    def __init__(self, config: Config | None = None, timeout: float = 10):
        self.config = config or Config()
        self.timeout = timeout

    @property
    def base(self):
        host = self.config.get("api.host", "127.0.0.1")
        if host in ("0.0.0.0", "::"):
            host = "127.0.0.1"
        return f"http://{host}:{self.config.get('api.port')}/api"

    def call(self, method: str, path: str, body=None, raw=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.config.get('api.token')}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = r.read()
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", str(e))
            except Exception:
                msg = str(e)
            raise ApiError(e.code, msg) from None
        if raw:
            return payload.decode("utf-8", "replace")
        return json.loads(payload)

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, body=None):
        return self.call("POST", path, body if body is not None else {})

    def delete(self, path):
        return self.call("DELETE", path)

    def alive(self) -> bool:
        try:
            old, self.timeout = self.timeout, 2
            self.get("/ping")
            return True
        except Exception:
            return False
        finally:
            self.timeout = old

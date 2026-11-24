import json
import logging
import os
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlsplit

from .core import CONFIG, ROOT, SKILLS, Agent, Store, dump
from .pipeline import catalog, collect, promote
from .planner import Planner

LOG = logging.getLogger("pa")
MAX_BODY = 32 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    def send(self, code, data, mime="application/json; charset=utf-8"):
        body = dump(data).encode() if mime.startswith("application/json") else data
        self.send_response(code)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; object-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.dispatch(False)

    def do_POST(self):
        self.dispatch(True)

    def dispatch(self, post):
        url = urlsplit(self.path)
        path = url.path
        store = self.server.store
        agent = self.server.agent
        try:
            if not post:
                static = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
                          "/style.css": ("style.css", "text/css")}
                if path in static:
                    name, mime = static[path]
                    return self.send(200, (ROOT / "web" / name).read_bytes(), mime + "; charset=utf-8")
                if path == "/api/health":
                    data = agent.planner.health()
                elif path == "/api/steps":
                    data = CONFIG["steps"]
                elif path == "/api/skills":
                    data = list(SKILLS.values())
                elif path == "/api/datasets":
                    data = catalog(store, dict(parse_qsl(url.query)))
                elif path in ("/api/tasks", "/api/events", "/api/episodes"):
                    data = store.list(path.rsplit("/", 1)[1])
                elif path.startswith("/api/tasks/"):
                    data = store.get("tasks", path.rsplit("/", 1)[1])
                elif path == "/api/dashboard":
                    tasks = store.list("tasks")
                    events = store.list("events")
                    data = {"states": Counter(t["state"] for t in tasks),
                            "recovered": sum(bool(t["recoveries"]) for t in tasks),
                            "tasks": tasks[:10], "events": events[:20]}
                else:
                    return self.send(404, {"error": "경로 없음"})
                return self.send(200, data)
            if self.headers.get_content_type() != "application/json":
                return self.send(415, {"error": "application/json 필요"})
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= MAX_BODY:
                return self.send(413, {"error": "요청 크기 초과 또는 빈 요청"})
            payload = json.loads(self.rfile.read(size), parse_constant=lambda x: float(x))
            if not isinstance(payload, dict):
                raise ValueError("JSON 객체 필요")
            dump(payload)
            if path == "/api/tasks":
                result = agent.plan(payload)
                if result.get("error"):
                    LOG.warning("TaskID=%s state=%s %s", result["TaskID"], result["state"], result["error"])
            elif path.startswith("/api/tasks/") and path.endswith("/run"):
                result = agent.run(path.split("/")[3], payload.get("scenario", "normal"))
            elif path == "/api/episodes":
                result = collect(store, payload)
            elif path == "/api/datasets":
                result = promote(store, payload)
            elif path == "/api/outbox/replay":
                result = {"replayed": store.replay()}
            else:
                return self.send(404, {"error": "경로 없음"})
            self.send(201, result)
        except (ValueError, TypeError, AttributeError) as error:
            self.send(400, {"error": str(error)})
        except KeyError:
            self.send(404, {"error": "레코드 없음"})
        except Exception:
            LOG.exception("요청 처리 실패")
            self.send(500, {"error": "요청 처리 실패 · 서버 로그를 확인하세요."})


def main():
    logging.basicConfig(level=logging.INFO)
    store = Store()
    planner = Planner()
    server = ThreadingHTTPServer((os.getenv("PA_HOST", "0.0.0.0"), int(os.getenv("PA_PORT", "8080"))), Handler)
    server.store, server.agent = store, Agent(store, planner)
    LOG.info("LLM 상태 %s", planner.health())
    LOG.info("PA Agent 포트 %s", server.server_port)
    with server:
        server.serve_forever()


if __name__ == "__main__":
    main()

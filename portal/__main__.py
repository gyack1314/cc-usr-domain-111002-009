"""本地合成业务门户及浏览器联调服务。"""
import argparse
import json
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

WEB = Path(__file__).resolve().parents[1] / "web"


def make_server(host="127.0.0.1", port=0):
    state = {"phase": "base", "version": 1, "confirmed": False, "actions": []}
    lock = threading.RLock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, status, data, mime="application/json; charset=utf-8"):
            if not isinstance(data, bytes):
                data = json.dumps(data, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = unquote(urlsplit(self.path).path)
            if path in ("/api/runtime", "/__state"):
                with lock:
                    self.send(200, state)
                return
            target = (WEB / (path.lstrip("/") or "index.html")).resolve()
            if not target.is_relative_to(WEB.resolve()) or not target.is_file():
                self.send(404, {"error": "页面不存在"})
                return
            mime = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".txt": "text/plain; charset=utf-8"}.get(target.suffix, "application/octet-stream")
            self.send(200, target.read_bytes(), mime)

        def do_POST(self):
            path = urlsplit(self.path).path
            try:
                data = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                if not isinstance(data, dict):
                    raise ValueError("请求内容必须是对象")
            except (ValueError, TypeError):
                self.send(400, {"error": "JSON内容不合法"})
                return
            with lock:
                if path == "/__control":
                    for key in ("phase", "version", "confirmed"):
                        if key in data:
                            state[key] = data[key]
                    self.send(200, state)
                elif path == "/api/action":
                    name = data.get("name")
                    if not isinstance(name, str):
                        self.send(400, {"error": "缺少动作名称"})
                        return
                    state["actions"].append(data)
                    if name == "human-confirm":
                        state["confirmed"] = True
                    self.send(200, {"sequence": len(state["actions"]), "confirmed": state["confirmed"]})
                else:
                    self.send(404, {"error": "接口不存在"})

    server = ThreadingHTTPServer((host, port), Handler)
    server.portal_state = state
    return server


@contextmanager
def running():
    server = make_server()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_port), server.portal_state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def main():
    parser = argparse.ArgumentParser(description="启动本地业务门户")
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    server = make_server(args.host, args.port)
    print("本地门户已启动", server.server_address, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()

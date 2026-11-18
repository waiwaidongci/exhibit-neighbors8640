"""标准库 HTTP 服务：REST API + 静态页面，并在启动时恢复未发布研究。"""

from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional
from urllib.parse import parse_qs, urlparse

from .service import CalibrationService
from .store import Store

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

_STORE: Optional[Store] = None
_SERVICE: Optional[CalibrationService] = None


def get_service(db_path: Optional[str] = None) -> CalibrationService:
    global _STORE, _SERVICE
    if _SERVICE is None:
        _STORE = Store(db_path or os.environ.get(
            "CALIBRATION_DB", os.path.join(os.getcwd(), "calibration_data", "app.db")))
        _SERVICE = CalibrationService(_STORE)
    return _SERVICE


def reset_singletons() -> None:
    """测试隔离：关闭并清空进程级单例。"""
    global _STORE, _SERVICE
    if _STORE is not None:
        _STORE.close()
    _STORE = None
    _SERVICE = None


class Handler(BaseHTTPRequestHandler):
    server_version = "CalibrationHTTP/1.0"

    # ----------------------------- 基础工具 ----------------------------- #
    def _json(self, code: int, body: Any) -> None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise _HttpError(400, f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise _HttpError(400, "请求体必须是 JSON 对象")
        return data

    def log_message(self, fmt: str, *args: Any) -> None:  # 静默默认日志
        pass

    # ----------------------------- 路由 ----------------------------- #
    def do_GET(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            qs = parse_qs(parsed.query)
            service = get_service()

            if path == "/health":
                return self._json(200, {"ok": True})
            if path == "/api/studies":
                return self._json(200, {"studies": service.list_studies()})
            if path.startswith("/api/studies/"):
                rest = path[len("/api/studies/"):]
                parts = rest.split("/")
                if len(parts) == 1:
                    return self._json(200, service.get_status(parts[0]))
                if len(parts) == 2 and parts[1] == "result":
                    return self._json(200, service.get_result(parts[0]))
                if len(parts) == 2 and parts[1] == "events":
                    sid = parts[0]
                    service.get_status(sid)  # 存在性检查
                    return self._json(
                        200,
                        {"study_id": sid,
                         "events": service.store.list_events(sid)})
            if path == "/api/demo-payload":
                from . import demo
                return self._json(200, demo.build_payload())
            return self._static(path)
        except _HttpError as exc:
            return self._json(exc.code, {"error": exc.message})
        except KeyError:
            return self._json(404, {"error": "研究不存在"})
        except Exception as exc:  # 兜底，避免连接中断
            return self._json(500, {"error": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/")
            service = get_service()

            if path == "/api/studies":
                data = self._read_json()
                idem = self.headers.get("Idempotency-Key") or data.pop(
                    "idempotency_key", None)
                run_async = str(data.pop("async", True)).lower() != "false"
                out = service.submit(data, idem_key=idem, run_async=run_async)
                return self._json(201 if not out.get("deduplicated") else 200,
                                  out)
            if path.startswith("/api/studies/"):
                rest = path[len("/api/studies/"):]
                parts = rest.split("/")
                if len(parts) == 2 and parts[1] == "resume":
                    sid = parts[0]
                    service.get_status(sid)
                    threading.Thread(
                        target=service.run_pipeline, args=(sid,),
                        daemon=True).start()
                    return self._json(202, {"study_id": sid,
                                            "resuming": True})
                if len(parts) == 2 and parts[1] == "publish":
                    sid = parts[0]
                    return self._json(200, service.publish(sid))
            if path == "/api/resume-all":
                return self._json(200,
                                  {"recovered": service.resume_all()})
            return self._json(404, {"error": "未知路径"})
        except _HttpError as exc:
            return self._json(exc.code, {"error": exc.message})
        except KeyError:
            return self._json(404, {"error": "研究不存在"})
        except Exception as exc:
            from . import engine
            if isinstance(exc, engine.CalibrationError):
                return self._json(422, {"error": str(exc)})
            return self._json(500, {"error": str(exc)})

    # ----------------------------- 静态资源 ----------------------------- #
    def _static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        rel = path.lstrip("/")
        full = os.path.normpath(os.path.join(STATIC_DIR, rel))
        if not full.startswith(STATIC_DIR) or not os.path.isfile(full):
            return self._json(404, {"error": "静态资源不存在"})
        ctype = "text/html; charset=utf-8" if full.endswith(".html") else \
            "application/javascript; charset=utf-8" if full.endswith(".js") \
            else "text/css; charset=utf-8" if full.endswith(".css") \
            else "application/octet-stream"
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _HttpError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def main() -> None:
    host = os.environ.get("CALIBRATION_HOST", "127.0.0.1")
    port = int(os.environ.get("CALIBRATION_PORT", "18321"))
    service = get_service()
    recovered = service.resume_all()  # 中断后沿用冻结版本恢复
    if recovered:
        print(f"[startup] 恢复研究 {len(recovered)} 项: {recovered}")
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"校准服务运行于 http://{host}:{port} （Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

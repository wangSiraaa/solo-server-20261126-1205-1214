"""本地 HTTP JSON 接口（仅标准库）。

路由：
  POST /batches                      业务端开单
  POST /batches/{id}/prepare         备料：排程 + 刻片登记（不可行返回 422 + 原因）
  POST /batches/{id}/open            下洗印单
  GET  /batches/{id}/print-sheet     师傅取洗印单
  GET  /batches/{id}/status          查询批次/纸状态
  POST /papers/{id}/shots            师傅确认一拍曝光（重复/迟到回条 409）
  POST /papers/{id}/samples          接回新样
  POST /orders/{id}/deliver          凭单取成品
  POST /stock/trials                 登记已付费旧试印库存
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .domain import LabError
from .service import PhotoLabService


class Handler(BaseHTTPRequestHandler):
    service: PhotoLabService = None  # 由 make_server 注入
    lock = threading.Lock()

    def log_message(self, fmt, *args):  # 静默常规访问日志，保持演示输出干净
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self):  # noqa: N802
        path = urlparse(self.path).path.strip("/").split("/")
        with self.lock:
            try:
                if len(path) == 3 and path[0] == "batches" and path[2] == "status":
                    return self._send(200, self.service.status(path[1]))
                if len(path) == 3 and path[0] == "batches" and path[2] == "print-sheet":
                    return self._send(200, self.service.print_sheet(path[1]))
                self._send(404, {"error": "未知路由", "path": self.path})
            except LabError as exc:
                self._send(exc.status, {"error": str(exc)})

    def do_POST(self):  # noqa: N802
        path = urlparse(self.path).path.strip("/").split("/")
        with self.lock:
            try:
                if path == ["batches"]:
                    data = self._body()
                    result = self.service.intake_batch(
                        data["batch_id"], data["rows"], data["cols"],
                        data["card"], data["sheet_budget"], data["shot_budget"],
                        data["papers"], data["order"],
                    )
                    return self._send(201, result)

                if len(path) == 3 and path[0] == "batches" and path[2] == "prepare":
                    result = self.service.prepare(path[1])
                    code = 200 if result["feasible"] else 422
                    return self._send(code, result)

                if len(path) == 3 and path[0] == "batches" and path[2] == "open":
                    return self._send(200, self.service.open_batch(path[1]))

                if len(path) == 3 and path[0] == "papers" and path[2] == "shots":
                    data = self._body()
                    return self._send(200, self.service.confirm_shot(
                        path[1], data.get("exposures"), data.get("note", "")))

                if len(path) == 3 and path[0] == "papers" and path[2] == "samples":
                    data = self._body()
                    return self._send(201, self.service.receive_sample(
                        data["sample_id"], path[1], data.get("note", "")))

                if len(path) == 3 and path[0] == "orders" and path[2] == "deliver":
                    return self._send(200, self.service.deliver(path[1]))

                if path == ["stock", "trials"]:
                    data = self._body()
                    for item in data.get("trials", []):
                        self.service.store.add_stock_trial(item["id"], item["level_name"])
                    return self._send(201, {"accepted": len(data.get("trials", []))})

                self._send(404, {"error": "未知路由", "path": self.path})
            except LabError as exc:
                self._send(exc.status, {"error": str(exc)})
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                self._send(400, {"error": f"请求体不合法: {exc}"})


def make_server(db_path: str, product_dir: str, host: str = "127.0.0.1",
                port: int = 8080) -> ThreadingHTTPServer:
    service = PhotoLabService(db_path, product_dir)
    Handler.service = service
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.service = service
    return httpd

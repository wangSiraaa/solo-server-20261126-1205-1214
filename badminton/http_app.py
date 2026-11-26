"""本地 HTTP 服务：记分台 / 边裁 / 球员三类角色各自的入口。

路由约定（角色入口在路径前缀上强制隔离）：
  POST /api/desk/games                 登记开局
  POST /api/desk/games/<g>/confirm     确认当前拍
  POST /api/desk/games/<g>/adjudicate  裁定报反争议
  POST /api/desk/games/<g>/void        开拍前作废重发
  POST /api/desk/games/<g>/release-timeout
  POST /api/judge/games/<g>/reports    边裁临时报分
  POST /api/judge/games/<g>/objections 边裁举证
  POST /api/player/games/<g>/serve     球员持单开球
  POST /api/player/games/<g>/timeouts  球员领用暂停
  POST /api/player/games/<g>/objections 球员举证
  GET  /api/games/<g>                  整局状态（三方共用只读视图）
  GET  /api/games/<g>/permits/<p>      单张开拍单
  GET  /api/games/<g>/rallies          已打回合（含原发球人）
  GET  /api/games/<g>/replay           事件流重放
所有 POST 均可在 JSON 体内带 idempotency_key，重放同一记录不会多打一拍。
"""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .service import Service, ServiceError


def build_handler(service: Service):
    class Handler(BaseHTTPRequestHandler):
        server_version = "BadmintonDesk/1.0"

        def log_message(self, fmt, *args):  # 精简日志
            logs = getattr(self.server, "_logs", None)
            if logs is not None:
                logs.append(f"{self.command} {self.path}")

        def _send(self, status: int, payload):
            body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise ServiceError("请求体不是合法 JSON", 400)

        def do_GET(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                # /api/games/<g>/replay 与 /api/games/<g>/rallies（4 段）
                if len(parts) == 4 and parts[:2] == ["api", "games"] \
                        and parts[2] != "games":
                    if parts[3] == "replay":
                        return self._send(200, service.replay_events(parts[2]))
                    if parts[3] == "rallies":
                        return self._send(
                            200,
                            {"rallies": list(
                                service.get_rallies(parts[2]).values())},
                        )
                # /api/games/<g>（3 段）
                if len(parts) == 3 and parts[:2] == ["api", "games"]:
                    return self._send(200, service.get_game(parts[2]))
                # /api/games/<g>/permits/<p>（5 段）
                if len(parts) == 5 and parts[:2] == ["api", "games"] \
                        and parts[3] == "permits":
                    return self._send(
                        200, service.get_permit(parts[2], parts[4])
                    )
                self._send(404, {"error": "未知路径"})
            except ServiceError as exc:
                self._send(exc.status, {"error": str(exc)})

        def do_POST(self):
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                body = self._read_json()
                idem = body.pop("idempotency_key", None)
                result = self._route(parts, body, idem)
                self._send(200, result)
            except ServiceError as exc:
                self._send(exc.status, {"error": str(exc)})
            except ValueError as exc:
                self._send(400, {"error": str(exc)})

        def _route(self, parts, body, idem):
            # 记分台入口
            if parts[:2] == ["api", "desk"]:
                if parts == ["api", "desk", "games"]:
                    return service.create_game(body, idem)
                g = self._game_id(parts, "desk")
                tail = parts[4:]
                if tail == ["confirm"]:
                    return service.confirm_rally(g, body["permit_id"], idem)
                if tail == ["adjudicate"]:
                    return service.adjudicate(g, body["permit_id"], body["winner"], idem)
                if tail == ["void"]:
                    return service.void_and_reissue(g, body["permit_id"], idem)
                if tail == ["release-timeout"]:
                    return service.release_timeout(g, body["permit_id"], idem)

            # 边裁入口
            if parts[:2] == ["api", "judge"]:
                g = self._game_id(parts, "judge")
                tail = parts[4:]
                if tail == ["reports"]:
                    return service.submit_report(
                        g, body["permit_id"], body["judge_id"], body["winner"], idem
                    )
                if tail == ["objections"]:
                    return service.file_objection(
                        g, body["permit_id"], "judge", body["judge_id"],
                        body["kind"], body.get("evidence", ""), idem,
                    )

            # 球员入口
            if parts[:2] == ["api", "player"]:
                g = self._game_id(parts, "player")
                tail = parts[4:]
                if tail == ["serve"]:
                    return service.serve(
                        g, body["permit_id"], body["player_id"], idem
                    )
                if tail == ["timeouts"]:
                    return service.claim_timeout(
                        g, body["permit_id"], body["player_id"], idem
                    )
                if tail == ["objections"]:
                    return service.file_objection(
                        g, body["permit_id"], "player", body["player_id"],
                        body["kind"], body.get("evidence", ""), idem,
                    )

            raise ServiceError("未知路径或角色入口不符", 404)

        @staticmethod
        def _game_id(parts, role: str) -> str:
            # /api/<role>/games/<g>/...
            if len(parts) >= 4 and parts[2] == "games":
                return parts[3]
            raise ServiceError(f"{role} 入口路径缺少局号", 400)

    return Handler


def run(host: str = "127.0.0.1", port: int = 8080, data_dir: str = "./data"):
    service = Service(data_dir)
    httpd = ThreadingHTTPServer((host, port), build_handler(service))
    httpd._logs = []
    print(f"羽球记分台已启动: http://{host}:{port}  数据目录: {data_dir}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭……")
        httpd.server_close()


if __name__ == "__main__":
    run()

"""HTTP 入口：编辑、印厂、投递站各自用 JSON 办理业务。

启动：python3 server.py [--port 8000] [--db paper.db]
所有写操作单事务提交，出错回滚并返回 JSON 错误。
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import db
import service as svc

DB_PATH = os.environ.get("PAPER_DB", "paper.db")
WRITE_LOCK = threading.Lock()

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, pattern, fn))
        return fn
    return deco


class Handler(BaseHTTPRequestHandler):
    server_version = "PaperOffice/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("[http] " + fmt % args + "\n")

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise svc.ApiError(400, "请求体不是合法 JSON")
        if not isinstance(data, dict):
            raise svc.ApiError(400, "请求体必须是 JSON 对象")
        return data

    def _match(self, method, path):
        for m, pattern, fn in ROUTES:
            if m != method:
                continue
            parts = [p for p in pattern.strip("/").split("/") if p]
            targets = [p for p in path.strip("/").split("/") if p]
            if len(parts) != len(targets):
                continue
            kwargs = {}
            ok = True
            for part, target in zip(parts, targets):
                if part.startswith("{") and part.endswith("}"):
                    kwargs[part[1:-1]] = target
                elif part != target:
                    ok = False
                    break
            if ok:
                return fn, kwargs
        return None, None

    def _handle(self, method):
        parsed = urlparse(self.path)
        fn, kwargs = self._match(method, parsed.path)
        if fn is None:
            self._send(404, {"error": f"无此路由: {method} {parsed.path}"})
            return
        conn = db.connect(DB_PATH)
        locked = method in ("POST", "PUT")
        if locked:
            WRITE_LOCK.acquire()
        try:
            if method in ("POST", "PUT"):
                data = self._read_json()
            else:
                qs = parse_qs(urlparse(self.path).query)
                data = {k: v[0] for k, v in qs.items()}
            result = fn(conn, data, **kwargs)
            conn.commit()
            self._send(200, result)
        except svc.ApiError as e:
            conn.rollback()
            self._send(e.status, {"error": e.message})
        except Exception as e:  # noqa: BLE001
            conn.rollback()
            self._send(500, {"error": f"{type(e).__name__}: {e}"})
        finally:
            conn.close()
            if locked:
                WRITE_LOCK.release()

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


# ---------- 基础资料 ----------
@route("POST", "/editions")
def r_edition(conn, data):
    return svc.create_edition(conn, data["code"], data["name"])


@route("POST", "/households")
def r_household(conn, data):
    return svc.create_household(conn, data["name"])


@route("POST", "/subscriptions")
def r_subscribe(conn, data):
    return svc.subscribe(conn, data["household_id"], data["edition_code"])


@route("POST", "/households/{hid}/unsubscribe")
def r_unsubscribe(conn, data, hid):
    return svc.unsubscribe(conn, hid)


# ---------- 编辑 ----------
@route("POST", "/messages")
def r_message(conn, data):
    return svc.create_message(conn, data["title"])


@route("POST", "/manuscripts")
def r_manuscript(conn, data):
    body = data["body"] if isinstance(data["body"], str) else json.dumps(
        data["body"], ensure_ascii=False)
    return svc.create_manuscript(conn, data["message_id"], body)


@route("POST", "/pages")
def r_page(conn, data):
    return svc.create_page(conn, data["edition_code"], data["title"],
                           data.get("fixed_ms_id"))


@route("POST", "/placements")
def r_place(conn, data):
    return svc.place(conn, data["page_id"], data["message_id"],
                     data["manuscript_id"])


@route("POST", "/issues")
def r_issue(conn, data):
    return svc.create_issue(conn, data["label"])


@route("POST", "/proofs")
def r_proof(conn, data):
    return svc.set_proof(conn, data["issue_id"], data["page_id"],
                         data["manuscript_id"])


@route("POST", "/corrections")
def r_correction(conn, data):
    return svc.publish_correction(conn, data["message_id"],
                                  data["from_ms_id"], data["to_ms_id"])


@route("GET", "/corrections/{cid}")
def r_correction_status(conn, data, cid):
    return svc.correction_status(conn, cid)


# ---------- 印厂 ----------
@route("POST", "/print-runs")
def r_print_run(conn, data):
    return svc.create_print_run(conn, data["issue_id"], data["label"])


@route("POST", "/bundles")
def r_bundle(conn, data):
    return svc.create_bundle(conn, data["run_id"], data["edition_code"])


@route("GET", "/bundles/{bid}")
def r_bundle_trace(conn, data, bid):
    return svc.bundle_trace(conn, bid)


# ---------- 投递站 ----------
@route("POST", "/receipts")
def r_receipt(conn, data):
    return svc.post_receipt(conn, data["receipt_id"], data["household_id"],
                            data.get("items", []), data.get("signed_at"))


@route("POST", "/receipts/{rid}/delete")
def r_receipt_delete(conn, data, rid):
    return svc.delete_receipt(conn, rid)


@route("GET", "/receipts/{rid}")
def r_receipt_detail(conn, data, rid):
    return svc.receipt_detail(conn, rid)


@route("GET", "/households/{hid}/trace")
def r_household_trace(conn, data, hid):
    return svc.household_trace(conn, hid)


# ---------- 发行部 ----------
@route("POST", "/distribution/mode")
def r_mode_set(conn, data):
    return svc.set_distribution_mode(conn, data["mode"])


@route("GET", "/distribution/mode")
def r_mode_get(conn, data):
    return svc.get_distribution_mode(conn)


@route("GET", "/owed")
def r_owed(conn, data):
    return {"owed": svc.owed_list(conn)}


@route("POST", "/special/prepare")
def r_special_prepare(conn, data):
    return svc.prepare_special(conn)


@route("POST", "/special/trips")
def r_trip(conn, data):
    return svc.dispatch_trip(conn, data["slip_ids"])


@route("POST", "/insert/seal")
def r_seal(conn, data):
    return svc.seal_bundle(conn, data["bundle_id"], data["household_id"],
                           data.get("promised_by"))


@route("POST", "/insert/reroute")
def r_reroute(conn, data):
    return svc.reroute_bundle(conn, data["bundle_id"])


@route("POST", "/slips/{sid}/cancel")
def r_cancel_slip(conn, data, sid):
    return svc.cancel_slip(conn, sid, data.get("reason", "cancelled"))


@route("GET", "/insert/overdue")
def r_overdue(conn, data):
    return {"overdue": svc.overdue_households(conn, data.get("today"))}


@route("GET", "/health")
def r_health(conn, data):
    return {"status": "ok"}


def main():
    global DB_PATH
    port = 8000
    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1]); i += 2
        elif args[i] == "--db" and i + 1 < len(args):
            DB_PATH = args[i + 1]; i += 2
        else:
            i += 1
    db.init_db(DB_PATH)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"paper office listening on http://127.0.0.1:{port} db={DB_PATH}",
          flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    main()

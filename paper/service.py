"""领域服务：编辑、印厂、投递站、发行部的全部业务规则。

设计要点（对应题面）：
- 开印时把每页“实际用稿身份”钉死在 run_page，签收只认真实印次；
- 欠件 = 真实回单证明读过旧稿，且没有任何一条已签收的勘误纸条覆盖该更正；
- 纸条只带字段级改动（diff），不重发整篇；
- 专递按户合并、出车冻结；随刊夹送绑定封包，封而不达不销账，退订不销账。
"""
import json
from datetime import date

from db import next_id


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def now_iso():
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


# ---------- 工具 ----------

def _require(conn, table, key_field, key_val):
    row = conn.execute(
        f"SELECT * FROM {table} WHERE {key_field}=?", (key_val,)
    ).fetchone()
    if row is None:
        raise ApiError(404, f"{table} {key_val} 不存在")
    return row


def _get_kv(conn, key, default=None):
    row = conn.execute("SELECT v FROM kv WHERE k=?", (key,)).fetchone()
    return row["v"] if row else default


def _set_kv(conn, key, val):
    conn.execute(
        "INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
        (key, val),
    )


def manuscript_changes(old_body, new_body):
    """只算字段级改动；非 JSON 正文退化为整体替换说明。"""
    try:
        old = json.loads(old_body)
        new = json.loads(new_body)
    except (json.JSONDecodeError, TypeError):
        return [{"field": "_body", "old": old_body, "new": new_body}]
    changes = []
    for field in sorted(set(old) | set(new)):
        if old.get(field) != new.get(field):
            changes.append({"field": field, "old": old.get(field),
                            "new": new.get(field)})
    return changes


def render_changes(changes):
    return "；".join(f"{c['field']}：{c['old']} → {c['new']}" for c in changes)


# ---------- 基础资料：版面 / 住户 / 订阅 ----------

def create_edition(conn, code, name):
    conn.execute("INSERT INTO edition(code,name) VALUES(?,?)", (code, name))
    return {"code": code, "name": name}


def create_household(conn, name):
    hid = next_id(conn, "H")
    conn.execute("INSERT INTO household(id,name) VALUES(?,?)", (hid, name))
    return {"id": hid, "name": name}


def subscribe(conn, household_id, edition_code):
    _require(conn, "household", "id", household_id)
    _require(conn, "edition", "code", edition_code)
    conn.execute(
        "INSERT OR IGNORE INTO subscription(household_id, edition_code) VALUES(?,?)",
        (household_id, edition_code),
    )
    return {"household_id": household_id, "edition_code": edition_code}


def unsubscribe(conn, household_id):
    """退订只是停止后续报纸；已经欠下的更正不因此销账。"""
    _require(conn, "household", "id", household_id)
    conn.execute("UPDATE household SET active=0 WHERE id=?", (household_id,))
    return {"household_id": household_id, "active": False}


# ---------- 编辑：消息、稿件、版面、落版、校样 ----------

def create_message(conn, title):
    mid = next_id(conn, "M")
    conn.execute("INSERT INTO message(id,title) VALUES(?,?)", (mid, title))
    return {"id": mid, "title": title}


def create_manuscript(conn, message_id, body):
    _require(conn, "message", "id", message_id)
    mid = next_id(conn, "MS")
    conn.execute(
        "INSERT INTO manuscript(id,message_id,body,created_at) VALUES(?,?,?,?)",
        (mid, message_id, body, now_iso()),
    )
    return {"id": mid, "message_id": message_id, "body": json.loads(body)}


def create_page(conn, edition_code, title, fixed_ms_id=None):
    _require(conn, "edition", "code", edition_code)
    if fixed_ms_id:
        _require(conn, "manuscript", "id", fixed_ms_id)
    pid = next_id(conn, "P")
    conn.execute(
        "INSERT INTO page(id,edition_code,title,fixed_ms_id) VALUES(?,?,?,?)",
        (pid, edition_code, title, fixed_ms_id),
    )
    return {"id": pid, "edition_code": edition_code, "title": title,
            "fixed_ms_id": fixed_ms_id}


def place(conn, page_id, message_id, manuscript_id):
    """同一条消息可以不同短文落在若干版面。"""
    _require(conn, "page", "id", page_id)
    ms = _require(conn, "manuscript", "id", manuscript_id)
    _require(conn, "message", "id", message_id)
    if ms["message_id"] != message_id:
        raise ApiError(400, "稿件不属于该消息")
    pid = next_id(conn, "PL")
    conn.execute(
        "INSERT INTO placement(id,page_id,message_id,manuscript_id) VALUES(?,?,?,?)",
        (pid, page_id, message_id, manuscript_id),
    )
    return {"id": pid, "page_id": page_id, "message_id": message_id,
            "manuscript_id": manuscript_id}


def create_issue(conn, label):
    iid = next_id(conn, "I")
    conn.execute("INSERT INTO issue(id,label) VALUES(?,?)", (iid, label))
    return {"id": iid, "label": label}


def set_proof(conn, issue_id, page_id, manuscript_id):
    _require(conn, "issue", "id", issue_id)
    _require(conn, "page", "id", page_id)
    _require(conn, "manuscript", "id", manuscript_id)
    conn.execute(
        "INSERT INTO issue_proof(issue_id,page_id,manuscript_id) VALUES(?,?,?) "
        "ON CONFLICT(issue_id,page_id) DO UPDATE SET manuscript_id=excluded.manuscript_id",
        (issue_id, page_id, manuscript_id),
    )
    return {"issue_id": issue_id, "page_id": page_id, "manuscript_id": manuscript_id}


def publish_correction(conn, message_id, from_ms_id, to_ms_id):
    """编辑改稿：登记一条更正，内容只是两稿之间的字段级差异。"""
    _require(conn, "message", "id", message_id)
    old = _require(conn, "manuscript", "id", from_ms_id)
    new = _require(conn, "manuscript", "id", to_ms_id)
    if old["message_id"] != message_id or new["message_id"] != message_id:
        raise ApiError(400, "稿件不属于该消息")
    changes = manuscript_changes(old["body"], new["body"])
    cid = next_id(conn, "C")
    conn.execute(
        "INSERT INTO correction(id,message_id,from_ms_id,to_ms_id,changes_json,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (cid, message_id, from_ms_id, to_ms_id,
         json.dumps(changes, ensure_ascii=False), now_iso()),
    )
    return {"id": cid, "message_id": message_id, "from_ms_id": from_ms_id,
            "to_ms_id": to_ms_id, "changes": changes}


# ---------- 印厂：开印钉稿、封包 ----------

def _effective_ms(conn, issue_id, page):
    if page["fixed_ms_id"]:
        return page["fixed_ms_id"]
    row = conn.execute(
        "SELECT manuscript_id FROM issue_proof WHERE issue_id=? AND page_id=?",
        (issue_id, page["id"]),
    ).fetchone()
    if row is None:
        raise ApiError(400, f"跟随型版面 {page['id']} 在本期缺少校样")
    return row["manuscript_id"]


def create_print_run(conn, issue_id, label):
    _require(conn, "issue", "id", issue_id)
    rid = next_id(conn, "R")
    conn.execute(
        "INSERT INTO print_run(id,issue_id,label,printed_at) VALUES(?,?,?,?)",
        (rid, issue_id, label, now_iso()),
    )
    frozen = []
    pages = conn.execute("SELECT * FROM page").fetchall()
    for page in pages:
        ms_id = _effective_ms(conn, issue_id, page)
        conn.execute(
            "INSERT INTO run_page(run_id,page_id,manuscript_id) VALUES(?,?,?)",
            (rid, page["id"], ms_id),
        )
        frozen.append({"page_id": page["id"], "edition_code": page["edition_code"],
                       "manuscript_id": ms_id})
    return {"id": rid, "issue_id": issue_id, "label": label, "frozen_pages": frozen}


def create_bundle(conn, run_id, edition_code):
    _require(conn, "print_run", "id", run_id)
    _require(conn, "edition", "code", edition_code)
    bid = next_id(conn, "B")
    conn.execute(
        "INSERT INTO bundle(id,run_id,edition_code,status) VALUES(?,?,?,'stock')",
        (bid, run_id, edition_code),
    )
    return {"id": bid, "run_id": run_id, "edition_code": edition_code,
            "status": "stock"}


def _run_page_ms(conn, run_id, page_id):
    row = conn.execute(
        "SELECT manuscript_id FROM run_page WHERE run_id=? AND page_id=?",
        (run_id, page_id),
    ).fetchone()
    if row is None:
        raise ApiError(400, f"印次 {run_id} 没有页面 {page_id} 的钉稿记录")
    return row["manuscript_id"]


# ---------- 欠件计算：全部从真实印次与签收回执出发 ----------

def _exposed_households(conn, correction_id):
    """回单上确实签过该更正旧稿身份的住户。

    链式更正在此处理：一条消息可能连续改多版（MS1→MS2→MS3）。
    读过“目标稿之前任一稿”的住户都需要这条更正——读 MS1 的人
    既没见过 MS2 也没见过 MS3，C2（MS2→MS3）对他同样欠着。
    """
    c = conn.execute("SELECT * FROM correction WHERE id=?",
                     (correction_id,)).fetchone()
    older = conn.execute(
        """
        SELECT id FROM manuscript
        WHERE message_id=?
          AND (created_at < (SELECT created_at FROM manuscript WHERE id=?)
               OR (created_at = (SELECT created_at FROM manuscript WHERE id=?)
                   AND id != ?))
        """,
        (c["message_id"], c["to_ms_id"], c["to_ms_id"], c["to_ms_id"]),
    ).fetchall()
    older_ids = [r["id"] for r in older]
    if not older_ids:
        return set()
    marks = ",".join("?" for _ in older_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT r.household_id
        FROM receipt_item ri
        JOIN receipt r ON r.id = ri.receipt_id
        WHERE ri.kind='page' AND ri.manuscript_id IN ({marks})
        """,
        older_ids,
    ).fetchall()
    return {r["household_id"] for r in rows}


def _settled_households(conn, correction_id):
    """已签收覆盖该更正的纸条的住户（纸条状态为 signed）。"""
    rows = conn.execute(
        """
        SELECT DISTINCT s.household_id
        FROM slip_correction sc
        JOIN slip s ON s.id = sc.slip_id
        WHERE sc.correction_id = ? AND s.status = 'signed'
        """,
        (correction_id,),
    ).fetchall()
    return {r["household_id"] for r in rows}


def correction_status(conn, correction_id):
    """编辑视角：这处更正还有谁没收到。"""
    c = _require(conn, "correction", "id", correction_id)
    exposed = _exposed_households(conn, correction_id)
    settled = _settled_households(conn, correction_id)
    owed = sorted(exposed - settled)
    return {
        "correction_id": correction_id,
        "message_id": c["message_id"],
        "changes": json.loads(c["changes_json"]),
        "exposed": sorted(exposed),
        "settled": sorted(settled & exposed),
        "still_owed": owed,
    }


def _household_owed_corrections(conn, household_id):
    corr_rows = conn.execute("SELECT * FROM correction").fetchall()
    owed = []
    for c in corr_rows:
        exposed = _exposed_households(conn, c["id"])
        if household_id not in exposed:
            continue
        settled = _settled_households(conn, c["id"])
        if household_id not in settled:
            owed.append(c)
    return owed


def owed_list(conn):
    """全局欠件名单：户 → 该户实际缺失的更正（只含 diff）。"""
    households = conn.execute("SELECT * FROM household").fetchall()
    out = []
    for h in households:
        owed = _household_owed_corrections(conn, h["id"])
        if owed:
            out.append({
                "household_id": h["id"],
                "name": h["name"],
                "active": bool(h["active"]),
                "corrections": [
                    {"correction_id": c["id"], "message_id": c["message_id"],
                     "changes": json.loads(c["changes_json"])}
                    for c in owed
                ],
            })
    return out


def _slip_payload(conn, slip_row):
    corrs = conn.execute(
        """
        SELECT c.* FROM slip_correction sc
        JOIN correction c ON c.id = sc.correction_id
        WHERE sc.slip_id=? ORDER BY c.id
        """,
        (slip_row["id"],),
    ).fetchall()
    changes = []
    for c in corrs:
        for ch in json.loads(c["changes_json"]):
            changes.append({"correction_id": c["id"], **ch})
    return {
        "slip_id": slip_row["id"],
        "household_id": slip_row["household_id"],
        "mode": slip_row["mode"],
        "status": slip_row["status"],
        "bundle_id": slip_row["bundle_id"],
        "trip_id": slip_row["trip_id"],
        "promised_by": slip_row["promised_by"],
        "text": render_changes(changes),
        "corrections": [c["id"] for c in corrs],
    }


# ---------- 发行部：模式开关 ----------

def set_distribution_mode(conn, mode):
    if mode not in ("special", "insert"):
        raise ApiError(400, "mode 只能是 special 或 insert")
    _set_kv(conn, "distribution_mode", mode)
    return {"mode": mode}


def get_distribution_mode(conn):
    return {"mode": _get_kv(conn, "distribution_mode", "special")}


def _assert_mode(conn, mode):
    current = _get_kv(conn, "distribution_mode", "special")
    if current != mode:
        raise ApiError(409, f"发行部当前开的是 {current} 业务，不能办理 {mode}")


def _corr_already_covered(conn, household_id, corr_id):
    """该更正是否已在这户一张有效（非误投）纸条上。"""
    row = conn.execute(
        """
        SELECT 1 FROM slip_correction sc
        JOIN slip s ON s.id = sc.slip_id
        WHERE sc.correction_id=? AND s.household_id=?
          AND s.status != 'misdirected'
        LIMIT 1
        """,
        (corr_id, household_id),
    ).fetchone()
    return row is not None


# ---------- 专递勘误：按户合并、出车冻结、回单结清 ----------

def prepare_special(conn):
    """把尚未出车（其实是尚未备条）的更正按户合并成纸条。"""
    _assert_mode(conn, "special")
    created = []
    households = conn.execute("SELECT * FROM household").fetchall()
    for h in households:
        owed = _household_owed_corrections(conn, h["id"])
        fresh = [c for c in owed
                 if not _corr_already_covered(conn, h["id"], c["id"])]
        if not fresh:
            continue
        sid = next_id(conn, "S")
        conn.execute(
            "INSERT INTO slip(id,household_id,mode,status,created_at) "
            "VALUES(?,?,'special','prepared',?)",
            (sid, h["id"], now_iso()),
        )
        for c in fresh:
            conn.execute(
                "INSERT INTO slip_correction(slip_id,correction_id) VALUES(?,?)",
                (sid, c["id"]),
            )
        created.append(sid)
    return {"prepared_slips": created, "count": len(created)}


def dispatch_trip(conn, slip_ids):
    """车开走：冻结这趟车实际带的纸条清单，之后不得再改动。"""
    _assert_mode(conn, "special")
    if not slip_ids:
        raise ApiError(400, "至少带一条纸条")
    manifest = []
    for sid in slip_ids:
        s = _require(conn, "slip", "id", sid)
        if s["mode"] != "special":
            raise ApiError(400, f"{sid} 不是专递纸条")
        if s["status"] not in ("prepared",):
            raise ApiError(409, f"纸条 {sid} 状态 {s['status']}，不能出车")
        manifest.append(_slip_payload(conn, s))
    tid = next_id(conn, "T")
    conn.execute(
        "INSERT INTO trip(id,departed_at,manifest_json) VALUES(?,?,?)",
        (tid, now_iso(), json.dumps(manifest, ensure_ascii=False)),
    )
    for sid in slip_ids:
        conn.execute(
            "UPDATE slip SET status='dispatched', trip_id=? WHERE id=?",
            (tid, sid),
        )
    return {"trip_id": tid, "manifest": manifest}


# ---------- 随刊夹送：封包配条、改投、退订、逾期 ----------

def seal_bundle(conn, bundle_id, household_id, promised_by):
    """在下一包面向该户的报纸封包时配上欠件纸条。"""
    _assert_mode(conn, "insert")
    bundle = _require(conn, "bundle", "id", bundle_id)
    _require(conn, "household", "id", household_id)
    if bundle["status"] != "stock":
        raise ApiError(409,
                       f"包 {bundle_id} 状态 {bundle['status']}，只有库存包能封包")
    owed = _household_owed_corrections(conn, household_id)
    fresh = [c for c in owed
             if not _corr_already_covered(conn, household_id, c["id"])]
    slip_id = None
    if fresh:
        slip_id = next_id(conn, "S")
        conn.execute(
            "INSERT INTO slip(id,household_id,mode,status,bundle_id,promised_by,created_at) "
            "VALUES(?,?,'insert','prepared',?,?,?)",
            (slip_id, household_id, bundle_id, promised_by, now_iso()),
        )
        for c in fresh:
            conn.execute(
                "INSERT INTO slip_correction(slip_id,correction_id) VALUES(?,?)",
                (slip_id, c["id"]),
            )
    conn.execute("UPDATE bundle SET status='sealed' WHERE id=?", (bundle_id,))
    return {"bundle_id": bundle_id, "household_id": household_id,
            "status": "sealed", "slip_id": slip_id, "promised_by": promised_by}


def reroute_bundle(conn, bundle_id):
    """拆包改投：原包纸条成为误投，欠件保留；包释放回库存，可重新封给别户。"""
    bundle = _require(conn, "bundle", "id", bundle_id)
    conn.execute(
        "UPDATE slip SET status='misdirected' WHERE bundle_id=? AND status!='signed'",
        (bundle_id,),
    )
    conn.execute("UPDATE bundle SET status='stock' WHERE id=?", (bundle_id,))
    return {"bundle_id": bundle_id, "status": "stock"}


def cancel_slip(conn, slip_id, reason):
    """撤销一条未签收纸条（如专递转随刊）：欠件保留，可重新备条。"""
    s = _require(conn, "slip", "id", slip_id)
    if s["status"] == "signed":
        raise ApiError(409, "已签收纸条不能撤销")
    conn.execute("UPDATE slip SET status='misdirected' WHERE id=?", (slip_id,))
    return {"slip_id": slip_id, "status": "misdirected", "reason": reason}


def overdue_households(conn, today=None):
    """超出承诺日且没有可用包/在途纸条的逾期户；退订户的欠账照样列出。"""
    today = today or date.today().isoformat()
    out = []
    households = conn.execute("SELECT * FROM household").fetchall()
    for h in households:
        owed = _household_owed_corrections(conn, h["id"])
        if not owed:
            continue
        slips = conn.execute(
            "SELECT * FROM slip WHERE household_id=? AND status!='misdirected'",
            (h["id"],),
        ).fetchall()
        in_flight = any(s["status"] == "dispatched" for s in slips)
        usable_package = any(
            s["status"] == "prepared" and s["bundle_id"]
            and conn.execute(
                "SELECT 1 FROM bundle WHERE id=? AND status='sealed'",
                (s["bundle_id"],)).fetchone()
            for s in slips
        )
        # 误投条不计承诺日；只看仍有效的纸条
        promised = max((s["promised_by"] for s in slips if s["promised_by"]),
                       default=None)
        if in_flight or usable_package:
            continue
        if promised and promised >= today:
            continue
        out.append({
            "household_id": h["id"], "name": h["name"],
            "active": bool(h["active"]),
            "unsubscribed": not bool(h["active"]),
            "promised_by": promised,
            "reason": "no_promise_date" if not promised else "past_promise_no_package",
            "corrections": [c["id"] for c in owed],
        })
    return out


# ---------- 投递站：签收回执、同包拆投、同回单重发 ----------

def _sign_slip(conn, slip_id, receipt_id):
    s = _require(conn, "slip", "id", slip_id)
    if s["status"] == "signed":
        return
    if s["status"] == "misdirected":
        raise ApiError(409, f"纸条 {slip_id} 已误投，不能签收")
    conn.execute("UPDATE slip SET status='signed' WHERE id=?", (slip_id,))
    conn.execute(
        "INSERT INTO receipt_item(id,receipt_id,kind,slip_id,bundle_id) "
        "VALUES(?,?, 'slip', ?, ?)",
        (next_id(conn, "RI"), receipt_id, slip_id, s["bundle_id"]),
    )


def post_receipt(conn, receipt_id, household_id, items, signed_at=None):
    """登记回单。

    items: [{"kind":"page","bundle_id":..,"page_id":..},
            {"kind":"slip","slip_id":..}]
    页面稿号一律取印次冻结的 run_page，不接受外部传入；
    同一回单重发只补登缺项，已登项幂等跳过。
    """
    _require(conn, "household", "id", household_id)
    existing = conn.execute(
        "SELECT 1 FROM receipt WHERE id=?", (receipt_id,)).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO receipt(id,household_id,signed_at) VALUES(?,?,?)",
            (receipt_id, household_id, signed_at or now_iso()),
        )
    touched_bundles = set()
    explicit_slips = []
    for it in items:
        kind = it.get("kind")
        if kind == "page":
            bid = it["bundle_id"]; pid = it["page_id"]
            bundle = _require(conn, "bundle", "id", bid)
            ms_id = _run_page_ms(conn, bundle["run_id"], pid)
            dup = conn.execute(
                "SELECT 1 FROM receipt_item WHERE receipt_id=? AND kind='page' "
                "AND bundle_id=? AND page_id=?",
                (receipt_id, bid, pid)).fetchone()
            if dup:
                continue
            conn.execute(
                "INSERT INTO receipt_item(id,receipt_id,kind,bundle_id,run_id,"
                "page_id,manuscript_id) VALUES(?,?, 'page', ?,?,?,?)",
                (next_id(conn, "RI"), receipt_id, bid,
                 bundle["run_id"], pid, ms_id),
            )
            touched_bundles.add(bid)
        elif kind == "slip":
            explicit_slips.append(it["slip_id"])
        else:
            raise ApiError(400, f"未知回执项类型 {kind}")

    # 随刊夹送：包里的纸条随报纸签收而结清（同包拆投按户各自签收）
    for bid in touched_bundles:
        auto = conn.execute(
            "SELECT * FROM slip WHERE bundle_id=? AND household_id=? "
            "AND status != 'misdirected'",
            (bid, household_id),
        ).fetchall()
        for s in auto:
            _sign_slip(conn, s["id"], receipt_id)
        conn.execute(
            "UPDATE bundle SET status='delivered' WHERE id=?", (bid,))

    for sid in explicit_slips:
        _sign_slip(conn, sid, receipt_id)

    return receipt_detail(conn, receipt_id)


def delete_receipt(conn, receipt_id):
    """撤单（用于“只改变一户实际签收的印次”这类修正）：回滚签收与销账。"""
    _require(conn, "receipt", "id", receipt_id)
    signed_slips = conn.execute(
        "SELECT slip_id, trip_id FROM receipt_item ri JOIN slip s ON s.id=ri.slip_id "
        "WHERE ri.receipt_id=? AND ri.kind='slip'", (receipt_id,)).fetchall()
    delivered_bundles = conn.execute(
        "SELECT DISTINCT bundle_id FROM receipt_item WHERE receipt_id=? AND kind='page'",
        (receipt_id,)).fetchall()
    conn.execute("DELETE FROM receipt_item WHERE receipt_id=?", (receipt_id,))
    conn.execute("DELETE FROM receipt WHERE id=?", (receipt_id,))
    for row in signed_slips:
        fallback = "dispatched" if row["trip_id"] else "prepared"
        conn.execute("UPDATE slip SET status=? WHERE id=?",
                     (fallback, row["slip_id"]))
    for row in delivered_bundles:
        has_slip = conn.execute(
            "SELECT 1 FROM slip WHERE bundle_id=? AND status!='misdirected' LIMIT 1",
            (row["bundle_id"],)).fetchone()
        conn.execute("UPDATE bundle SET status=? WHERE id=?",
                     ("sealed" if has_slip else "stock", row["bundle_id"]))
    return {"deleted": receipt_id}


def receipt_detail(conn, receipt_id):
    """从一户的回单追到具体页和原稿身份。"""
    r = _require(conn, "receipt", "id", receipt_id)
    items = conn.execute(
        "SELECT * FROM receipt_item WHERE receipt_id=? ORDER BY id",
        (receipt_id,)).fetchall()
    return {
        "receipt_id": receipt_id,
        "household_id": r["household_id"],
        "signed_at": r["signed_at"],
        "items": [dict(it) for it in items],
    }


# ---------- 追查：包号去向、住户阅读轨迹 ----------

def bundle_trace(conn, bundle_id):
    """重开进程后，站员凭包号查回投递与补正的去向。"""
    b = _require(conn, "bundle", "id", bundle_id)
    pages = conn.execute(
        "SELECT rp.page_id, rp.manuscript_id, p.edition_code, p.title "
        "FROM run_page rp JOIN page p ON p.id=rp.page_id "
        "WHERE rp.run_id=? AND p.edition_code=?",
        (b["run_id"], b["edition_code"]),
    ).fetchall()
    receipts = conn.execute(
        "SELECT DISTINCT r.id AS receipt_id, r.household_id, r.signed_at "
        "FROM receipt_item ri JOIN receipt r ON r.id=ri.receipt_id "
        "WHERE ri.bundle_id=? ORDER BY r.id",
        (bundle_id,),
    ).fetchall()
    slips = conn.execute(
        "SELECT s.* FROM slip s WHERE s.bundle_id=? ORDER BY s.id",
        (bundle_id,)).fetchall()
    return {
        "bundle_id": bundle_id,
        "run_id": b["run_id"],
        "edition_code": b["edition_code"],
        "status": b["status"],
        "frozen_pages": [dict(p) for p in pages],
        "receipts": [dict(r) for r in receipts],
        "slips": [_slip_payload(conn, s) for s in slips],
    }


def household_trace(conn, household_id):
    """这户读到过哪一句（稿号）、收过哪些纸条、还欠什么。"""
    _require(conn, "household", "id", household_id)
    reads = conn.execute(
        """
        SELECT ri.receipt_id, ri.page_id, ri.manuscript_id, ms.body AS manuscript_body,
               ms.message_id
        FROM receipt_item ri
        JOIN manuscript ms ON ms.id = ri.manuscript_id
        JOIN receipt r ON r.id = ri.receipt_id
        WHERE ri.kind='page' AND r.household_id=?
        ORDER BY ri.id
        """,
        (household_id,),
    ).fetchall()
    slips = conn.execute(
        "SELECT * FROM slip WHERE household_id=? ORDER BY id", (household_id,),
    ).fetchall()
    return {
        "household_id": household_id,
        "signed_pages": [dict(x) for x in reads],
        "slips": [_slip_payload(conn, s) for s in slips],
        "owed": [
            {"correction_id": c["id"], "changes": json.loads(c["changes_json"])}
            for c in _household_owed_corrections(conn, household_id)
        ],
    }

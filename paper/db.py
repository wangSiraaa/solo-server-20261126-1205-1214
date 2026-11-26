"""SQLite 持久化层。

所有状态落盘到一个数据库文件，重开进程后站员仍可按包号追查去向。
每个 HTTP 请求开一条独立连接，写操作在单个事务内完成。
"""
import sqlite3

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS seq (
    prefix TEXT PRIMARY KEY,
    n      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS edition (
    code      TEXT PRIMARY KEY,
    name      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS household (
    id    TEXT PRIMARY KEY,
    name  TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS subscription (
    household_id TEXT NOT NULL REFERENCES household(id),
    edition_code TEXT NOT NULL REFERENCES edition(code),
    PRIMARY KEY (household_id, edition_code)
);

CREATE TABLE IF NOT EXISTS message (
    id     TEXT PRIMARY KEY,
    title  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS manuscript (
    id         TEXT PRIMARY KEY,
    message_id TEXT NOT NULL REFERENCES message(id),
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);

-- 版面：固定旧稿 fixed_ms_id 常驻；跟随本期校样的版面 fixed_ms_id 为空
CREATE TABLE IF NOT EXISTS page (
    id           TEXT PRIMARY KEY,
    edition_code TEXT NOT NULL REFERENCES edition(code),
    title        TEXT NOT NULL,
    fixed_ms_id  TEXT REFERENCES manuscript(id)
);

-- 编辑决定：哪条消息以哪篇短文落在哪一版（同一条消息可登若干版面）
CREATE TABLE IF NOT EXISTS placement (
    id           TEXT PRIMARY KEY,
    page_id      TEXT NOT NULL REFERENCES page(id),
    message_id   TEXT NOT NULL REFERENCES message(id),
    manuscript_id TEXT NOT NULL REFERENCES manuscript(id)
);

CREATE TABLE IF NOT EXISTS issue (
    id    TEXT PRIMARY KEY,
    label TEXT NOT NULL
);

-- 本期选定的校样：跟随型版面在此确定当期稿
CREATE TABLE IF NOT EXISTS issue_proof (
    issue_id   TEXT NOT NULL REFERENCES issue(id),
    page_id    TEXT NOT NULL REFERENCES page(id),
    manuscript_id TEXT NOT NULL REFERENCES manuscript(id),
    PRIMARY KEY (issue_id, page_id)
);

CREATE TABLE IF NOT EXISTS print_run (
    id        TEXT PRIMARY KEY,
    issue_id  TEXT NOT NULL REFERENCES issue(id),
    label     TEXT NOT NULL,
    printed_at TEXT NOT NULL
);

-- 开印时钉死每一页实际用稿的身份（固定版面 = 固定稿；跟随版面 = 本期校样）
CREATE TABLE IF NOT EXISTS run_page (
    run_id        TEXT NOT NULL REFERENCES print_run(id),
    page_id       TEXT NOT NULL REFERENCES page(id),
    manuscript_id TEXT NOT NULL REFERENCES manuscript(id),
    PRIMARY KEY (run_id, page_id)
);

CREATE TABLE IF NOT EXISTS bundle (
    id           TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL REFERENCES print_run(id),
    edition_code TEXT NOT NULL REFERENCES edition(code),
    status       TEXT NOT NULL DEFAULT 'stock'
        CHECK (status IN ('stock','sealed','delivered','rerouted'))
);

CREATE TABLE IF NOT EXISTS receipt (
    id           TEXT PRIMARY KEY,
    household_id TEXT NOT NULL REFERENCES household(id),
    signed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS receipt_item (
    id                TEXT PRIMARY KEY,
    receipt_id        TEXT NOT NULL REFERENCES receipt(id),
    kind              TEXT NOT NULL CHECK (kind IN ('page','slip')),
    bundle_id         TEXT REFERENCES bundle(id),
    run_id            TEXT REFERENCES print_run(id),
    page_id           TEXT REFERENCES page(id),
    manuscript_id     TEXT REFERENCES manuscript(id),
    slip_id           TEXT REFERENCES slip(id)
);

CREATE TABLE IF NOT EXISTS correction (
    id            TEXT PRIMARY KEY,
    message_id    TEXT NOT NULL REFERENCES message(id),
    from_ms_id    TEXT NOT NULL REFERENCES manuscript(id),
    to_ms_id      TEXT NOT NULL REFERENCES manuscript(id),
    changes_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slip (
    id            TEXT PRIMARY KEY,
    household_id  TEXT NOT NULL REFERENCES household(id),
    mode          TEXT NOT NULL CHECK (mode IN ('special','insert')),
    status        TEXT NOT NULL CHECK (status IN
                    ('prepared','dispatched','signed','misdirected')),
    bundle_id     TEXT REFERENCES bundle(id),
    trip_id       TEXT,
    promised_by   TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS slip_correction (
    slip_id       TEXT NOT NULL REFERENCES slip(id),
    correction_id TEXT NOT NULL REFERENCES correction(id),
    PRIMARY KEY (slip_id, correction_id)
);

CREATE TABLE IF NOT EXISTS trip (
    id          TEXT PRIMARY KEY,
    departed_at TEXT NOT NULL,
    -- 出车时冻结的清单快照（纸条 id、户、内容），车走后不得再改
    manifest_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);
"""


def connect(path):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(path):
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def next_id(conn, prefix):
    row = conn.execute("SELECT n FROM seq WHERE prefix=?", (prefix,)).fetchone()
    if row is None:
        n = 1
        conn.execute("INSERT INTO seq(prefix, n) VALUES(?,?)", (prefix, n))
    else:
        n = row["n"] + 1
        conn.execute("UPDATE seq SET n=? WHERE prefix=?", (n, prefix))
    return f"{prefix}{n:04d}"

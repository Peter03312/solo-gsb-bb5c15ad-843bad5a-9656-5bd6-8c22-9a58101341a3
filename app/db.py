"""SQLite connection handling and schema definition."""

from __future__ import annotations

import os
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS quota_pools (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id     TEXT    NOT NULL,                  -- 池为影片级共享额度
    max_sessions INTEGER NOT NULL CHECK (max_sessions >= 1),
    note        TEXT,
    created_at  TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_quota_pools_film ON quota_pools (film_id);

CREATE TABLE IF NOT EXISTS licenses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id       TEXT    NOT NULL,
    venue_id      TEXT    NOT NULL,
    valid_from    TEXT    NOT NULL,               -- canonical UTC ISO-8601
    valid_to      TEXT    NOT NULL,               -- canonical UTC ISO-8601
    max_sessions  INTEGER NOT NULL CHECK (max_sessions >= 1),
    pool_id       INTEGER REFERENCES quota_pools (id),  -- 绑定后不可改；NULL=未绑定旧语义
    note          TEXT,
    created_at    TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_licenses_film_venue
    ON licenses (film_id, venue_id);

CREATE TABLE IF NOT EXISTS revocations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id  INTEGER NOT NULL UNIQUE REFERENCES licenses (id),
    reason      TEXT    NOT NULL,
    revoked_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS suspensions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    license_id  INTEGER NOT NULL REFERENCES licenses (id),
    suspend_from TEXT   NOT NULL,               -- 左闭，canonical UTC ISO-8601
    suspend_to   TEXT   NOT NULL,               -- 右开，canonical UTC ISO-8601
    reason      TEXT    NOT NULL,
    created_at  TEXT    NOT NULL
);
-- 核验在写事务内按 license_id + 场次时间做左闭右开命中查询
CREATE INDEX IF NOT EXISTS idx_suspensions_license_window
    ON suspensions (license_id, suspend_from, suspend_to);

CREATE TABLE IF NOT EXISTS verifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT    NOT NULL UNIQUE,
    request_hash    TEXT    NOT NULL,
    film_id         TEXT    NOT NULL,
    venue_id        TEXT    NOT NULL,
    start_time      TEXT    NOT NULL,             -- canonical UTC ISO-8601
    decision        TEXT    NOT NULL CHECK (decision IN ('approved', 'rejected')),
    license_id      INTEGER REFERENCES licenses (id),
    pool_id         INTEGER REFERENCES quota_pools (id),  -- 本次核准所消耗的池
    reject_reason   TEXT,
    remaining_quota INTEGER,                      -- 命中授权消耗后的自身剩余
    pool_remaining_quota INTEGER,                 -- 所绑池消耗后的剩余（无池为 NULL）
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_verifications_license
    ON verifications (license_id, decision);

CREATE TABLE IF NOT EXISTS cancellations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id INTEGER NOT NULL UNIQUE REFERENCES verifications (id),
    idempotency_key TEXT    NOT NULL,             -- 冗余原核验幂等键，便于按键查询
    reason          TEXT    NOT NULL,
    cancelled_at    TEXT    NOT NULL
);
-- 每条核验流水至多一条取消记录：唯一约束在并发取消时兜底，额度不会重复释放
CREATE UNIQUE INDEX IF NOT EXISTS idx_cancellations_key
    ON cancellations (idempotency_key);

-- 一次性开场码：运营方按原核验幂等键为核准场次领取；每场至多一个码，
-- verification_id 唯一约束兜底并发领取；取消登记后该码状态立即失效（voided），
-- 不删行，保留可追溯的领取记录。
CREATE TABLE IF NOT EXISTS opening_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id INTEGER NOT NULL UNIQUE REFERENCES verifications (id),
    idempotency_key TEXT    NOT NULL UNIQUE,
    code            TEXT    NOT NULL UNIQUE,      -- 随机一次性码，仅首次领取返回
    issued_at       TEXT    NOT NULL
);

-- 核销回执：场馆在计划开场时刻起 30 分钟内核销成功后产生；
-- opening_code_id 唯一约束保证同一码至多一条回执，重复核销返回首次回执。
CREATE TABLE IF NOT EXISTS redemptions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    opening_code_id INTEGER NOT NULL UNIQUE REFERENCES opening_codes (id),
    venue_id        TEXT    NOT NULL,             -- 核销场馆编号（须与核准场馆一致）
    redeemed_at     TEXT    NOT NULL
);

-- 排片改签：运营方凭原核验幂等键 + 改签操作键，把尚未开场、未取消、未领开场码
-- 的核准场次改到同一影片+场馆的新开场时间。原核验行（决定/快照/占用归属）保持
-- 原样不变；当前排片与实时额度归属由"最近一条成功改签"派生（见 service 层
-- COALESCE 子查询）。operation_key 全库唯一：同键同内容重放不重复迁移，
-- 同键异内容冲突。业务上无法核准的改签同样落一条 decision='rejected' 流水，
-- 排片与占用不变且可按操作键幂等重放。
CREATE TABLE IF NOT EXISTS reschedules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    verification_id INTEGER NOT NULL REFERENCES verifications (id),
    idempotency_key TEXT    NOT NULL,             -- 冗余原核验幂等键，便于按键查询
    operation_key   TEXT    NOT NULL UNIQUE,      -- 改签操作键：同键同内容重放，异内容 409
    request_hash    TEXT    NOT NULL,             -- 原核验键 + 新开场时间的内容指纹
    from_start_time TEXT    NOT NULL,             -- 改签前的当前开场时刻
    to_start_time   TEXT    NOT NULL,             -- 改签请求的新开场时刻（被拒也留痕）
    from_license_id INTEGER,                      -- 改签前占用的授权（释放一次）
    to_license_id   INTEGER,                      -- 改签后新占用的授权（新增一次）；被拒为 NULL
    from_pool_id    INTEGER,                      -- 改签前占用的共享池（释放一次）
    to_pool_id      INTEGER,                      -- 改签后新占用的共享池；未绑池/被拒为 NULL
    new_remaining_quota INTEGER,                  -- 新授权占用后的自身剩余快照；被拒为 NULL
    new_pool_remaining_quota INTEGER,             -- 新池占用后的剩余快照；无池/被拒为 NULL
    decision        TEXT    NOT NULL CHECK (decision IN ('approved', 'rejected')),
    reject_reason   TEXT,                         -- 无法核准时的既有拒绝原因
    reason          TEXT,                         -- 运营方填写的改签原因（可选）
    rescheduled_at  TEXT    NOT NULL
);
-- 当前生效归属取"最近一次成功改签"：按 verification + 倒序 id 命中
CREATE INDEX IF NOT EXISTS idx_reschedules_verification
    ON reschedules (verification_id, decision, id);
CREATE INDEX IF NOT EXISTS idx_reschedules_key
    ON reschedules (idempotency_key, id);
"""

# Columns added after the initial release; applied lazily so databases
# created by older versions keep working without a manual migration.
_EXTRA_COLUMNS = {
    "licenses": [
        ("pool_id", "INTEGER REFERENCES quota_pools (id)"),
    ],
    "verifications": [
        ("pool_id", "INTEGER REFERENCES quota_pools (id)"),
        ("pool_remaining_quota", "INTEGER"),
    ],
}

# Indexes on columns introduced after the first release; created after the
# columns above exist, hence kept out of SCHEMA (IF NOT EXISTS makes this a
# no-op on databases that already have them).
_EXTRA_INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_licenses_pool ON licenses (pool_id)",
    "CREATE INDEX IF NOT EXISTS idx_verifications_pool ON verifications (pool_id, decision)",
]


def connect(db_path: str) -> sqlite3.Connection:
    """Open a connection tuned for concurrent writers.

    ``isolation_level=None`` puts the driver in autocommit mode so the
    service can control transactions explicitly with ``BEGIN IMMEDIATE``,
    which takes the write lock up-front and serialises concurrent
    verification requests instead of failing with lock-upgrade errors.
    """
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release when missing."""
    for table, columns in _EXTRA_COLUMNS.items():
        existing = {
            row["name"]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        for name, decl in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
    for stmt in _EXTRA_INDEXES:
        conn.execute(stmt)


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)


def init_db(db_path: str) -> None:
    if db_path != ":memory:":
        directory = os.path.dirname(os.path.abspath(db_path))
        os.makedirs(directory, exist_ok=True)
    conn = connect(db_path)
    try:
        init_schema(conn)
    finally:
        conn.close()

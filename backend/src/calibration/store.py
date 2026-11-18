"""SQLite 持久层：冻结输入、分阶段检查点、审计事件、幂等键（标准库 sqlite3）。"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS studies (
    study_id        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL,
    stage           TEXT NOT NULL,
    input_json      TEXT NOT NULL,
    input_sha256    TEXT NOT NULL,
    tolerance       REAL NOT NULL,
    anchor_json     TEXT,
    result_json     TEXT,
    error           TEXT,
    version         INTEGER NOT NULL DEFAULT 1,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL,
    published_at    REAL
);
CREATE TABLE IF NOT EXISTS study_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    study_id    TEXT NOT NULL,
    event       TEXT NOT NULL,
    detail      TEXT,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS idempotency_keys (
    idem_key    TEXT PRIMARY KEY,
    study_id    TEXT NOT NULL,
    created_at  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runtime_flags (
    flag_key    TEXT PRIMARY KEY,
    flag_value  TEXT NOT NULL
);
"""

TERMINAL_STATUS = "PUBLISHED"
# 检查点顺序；中断后从最后一个已落盘检查点继续，输入版本永不改变
STAGES = [
    "INTAKE_FROZEN",
    "GRAPH_BUILT",
    "COMPONENTS_SOLVED",
    "STABILITY_DONE",
    "PUBLISHED",
]


class Store:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".",
                    exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ----------------------------- 研究主表 ----------------------------- #
    def create_study(self, name: str, frozen_input: Dict[str, Any],
                     input_sha: str, idem_key: Optional[str]) -> str:
        study_id = uuid.uuid4().hex
        now = time.time()
        anchor = frozen_input.get("anchor_exhibit_ids")
        with self._lock:
            self._conn.execute(
                "INSERT INTO studies (study_id, name, status, stage, "
                "input_json, input_sha256, tolerance, anchor_json, "
                "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (study_id, name, "FROZEN", "INTAKE_FROZEN",
                 json.dumps(frozen_input, ensure_ascii=False, sort_keys=True),
                 input_sha, frozen_input["tolerance"],
                 json.dumps(anchor, ensure_ascii=False), now, now),
            )
            if idem_key:
                self._conn.execute(
                    "INSERT OR IGNORE INTO idempotency_keys "
                    "(idem_key, study_id, created_at) VALUES (?,?,?)",
                    (idem_key, study_id, now),
                )
            self.add_event(study_id, "INTAKE_FROZEN",
                           f"受理冻结：容差={frozen_input['tolerance']}，"
                           f"显式锚点={'是' if anchor else '自动共有展品'}")
            self._conn.commit()
        return study_id

    def get_study(self, study_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM studies WHERE study_id=?", (study_id,)
            ).fetchone()
        return dict(row) if row else None

    def list_studies(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM studies ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def find_by_idem_key(self, idem_key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT study_id FROM idempotency_keys WHERE idem_key=?",
                (idem_key,),
            ).fetchone()
        return row["study_id"] if row else None

    def save_checkpoint(self, study_id: str, stage: str, status: str,
                        result_patch: Optional[Dict[str, Any]] = None,
                        error: Optional[str] = None) -> None:
        """落盘检查点；result_patch 与既有 result_json 深度合并（顶层键）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM studies WHERE study_id=?",
                (study_id,),
            ).fetchone()
            result = json.loads(row["result_json"]) if row["result_json"] else {}
            if result_patch:
                result.update(result_patch)
            published_at = result.get("_published_at")
            self._conn.execute(
                "UPDATE studies SET status=?, stage=?, result_json=?, "
                "error=?, updated_at=?, published_at=COALESCE(published_at,?) "
                "WHERE study_id=?",
                (status, stage,
                 json.dumps(result, ensure_ascii=False, sort_keys=True),
                 error, time.time(), published_at, study_id),
            )
            self._conn.commit()

    def mark_failed(self, study_id: str, stage: str, message: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE studies SET status='FAILED', stage=?, error=?, "
                "updated_at=? WHERE study_id=?",
                (stage, message, time.time(), study_id),
            )
            self.add_event(study_id, "FAILED", f"阶段 {stage} 失败：{message}")
            self._conn.commit()

    def mark_published(self, study_id: str) -> bool:
        """一次性发布：已发布则返回 False（重试不重复发布）。"""
        now = time.time()
        with self._lock:
            row = self._conn.execute(
                "SELECT status FROM studies WHERE study_id=?", (study_id,)
            ).fetchone()
            if not row or row["status"] == TERMINAL_STATUS:
                return False
            row2 = self._conn.execute(
                "SELECT result_json FROM studies WHERE study_id=?",
                (study_id,),
            ).fetchone()
            result = json.loads(row2["result_json"]) if row2["result_json"] else {}
            result["_published_at"] = now
            self._conn.execute(
                "UPDATE studies SET status='PUBLISHED', stage='PUBLISHED', "
                "result_json=?, published_at=?, updated_at=? WHERE study_id=?",
                (json.dumps(result, ensure_ascii=False, sort_keys=True),
                 now, now, study_id),
            )
            self.add_event(study_id, "PUBLISHED", "榜单发布（一次性）")
            self._conn.commit()
            return True

    def add_event(self, study_id: str, event: str,
                  detail: Optional[str] = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO study_events (study_id, event, detail, created_at) "
                "VALUES (?,?,?,?)", (study_id, event, detail, time.time()),
            )
            self._conn.commit()

    def list_events(self, study_id: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM study_events WHERE study_id=? ORDER BY id",
                (study_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def resumable_studies(self) -> List[Dict[str, Any]]:
        """启动恢复：任何未发布的研究（含 FAILED）都沿用冻结版本继续。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM studies WHERE status != ? ORDER BY created_at",
                (TERMINAL_STATUS,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------- 测试用运行开关 ----------------------------- #
    def set_flag(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO runtime_flags (flag_key, flag_value) VALUES (?,?) "
                "ON CONFLICT(flag_key) DO UPDATE SET flag_value=excluded.flag_value",
                (key, value),
            )
            self._conn.commit()

    def get_flag(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT flag_value FROM runtime_flags WHERE flag_key=?", (key,)
            ).fetchone()
        return row["flag_value"] if row else None

    def clear_flag(self, key: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM runtime_flags WHERE flag_key=?", (key,)
            )
            self._conn.commit()

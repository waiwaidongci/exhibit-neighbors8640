"""校准研究服务：受理冻结、检查点流水线、中断恢复、一次性发布。

恢复语义：研究在受理时冻结评分/锚点/容差并记录输入 SHA256。流水线按
GRAPH_BUILT → COMPONENTS_SOLVED → STABILITY_DONE → PUBLISHED 落盘检查点；
中断（进程退出或注入故障）后重启自动沿用同一份冻结输入从最后检查点续算，
已发布的研究不会重复发布。
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from typing import Any, Dict, List, Optional

from . import engine
from .store import STAGES, Store


class CalibrationService:
    def __init__(self, store: Store):
        self.store = store
        self._running = set()
        self._run_lock = threading.Lock()

    # ----------------------------- 受理 ----------------------------- #
    def submit(self, payload: Dict[str, Any],
               idem_key: Optional[str] = None,
               run_async: bool = True) -> Dict[str, Any]:
        if idem_key:
            existing = self.store.find_by_idem_key(idem_key)
            if existing:
                row = self.store.get_study(existing)
                return {"study_id": existing, "deduplicated": True,
                        "status": row["status"], "stage": row["stage"]}
        frozen = engine.validate_payload(payload)
        digest = hashlib.sha256(
            json.dumps(frozen, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        study_id = self.store.create_study(frozen["name"], frozen, digest, idem_key)
        if run_async:
            threading.Thread(target=self._run_guarded,
                             args=(study_id,), daemon=True).start()
            return {"study_id": study_id, "deduplicated": False,
                    "status": "FROZEN", "stage": "INTAKE_FROZEN"}
        try:
            self.run_pipeline(study_id)
        except Exception:
            # 失败态已落盘（FAILED + 冻结输入保留），把受理 id 交给调用方恢复
            return {"study_id": study_id, "deduplicated": False,
                    "status": "FAILED", "stage": self.get_status(study_id)["stage"]}
        return self.get_status(study_id)

    # ----------------------------- 流水线 ----------------------------- #
    def _run_guarded(self, study_id: str) -> None:
        with self._run_lock:
            if study_id in self._running:
                return
            self._running.add(study_id)
        try:
            self.run_pipeline(study_id)
        finally:
            self._running.discard(study_id)

    def _frozen(self, study_id: str) -> Dict[str, Any]:
        row = self.store.get_study(study_id)
        if not row:
            raise KeyError(study_id)
        return json.loads(row["input_json"])

    def _crashpoint(self, study_id: str, stage: str) -> None:
        """测试注入：runtime_flags.crash_at = 阶段名，命中后抛错模拟中断。"""
        if self.store.get_flag("crash_at") == stage:
            self.store.add_event(study_id, "INTERRUPTED",
                                 f"模拟中断于 {stage}（检查点未推进）")
            raise RuntimeError(f"injected interruption at {stage}")

    def run_pipeline(self, study_id: str) -> Dict[str, Any]:
        row = self.store.get_study(study_id)
        if not row:
            raise KeyError(study_id)
        if row["status"] == "PUBLISHED":
            return self.get_status(study_id)

        frozen = json.loads(row["input_json"])
        stage = row["stage"]
        try:
            if stage == "INTAKE_FROZEN":
                self._crashpoint(study_id, "GRAPH_BUILT")
                edges, anchors = engine.build_edges(frozen)
                self.store.save_checkpoint(
                    study_id, "GRAPH_BUILT", "RUNNING",
                    {"graph": {"edges": edges,
                               "anchor_exhibit_ids": anchors}})
                self.store.add_event(study_id, "GRAPH_BUILT",
                                     f"锚点 {len(anchors)} 个，场次边 {len(edges)} 条")
                stage = "GRAPH_BUILT"

            if stage == "GRAPH_BUILT":
                self._crashpoint(study_id, "COMPONENTS_SOLVED")
                components, anchors = engine.analyze_components(frozen)
                quarantined = [c["component_id"] for c in components
                               if c["quarantined"]]
                self.store.save_checkpoint(
                    study_id, "COMPONENTS_SOLVED", "RUNNING",
                    {"components": components,
                     "quarantined_component_ids": quarantined})
                self.store.add_event(
                    study_id, "COMPONENTS_SOLVED",
                    f"连通分量 {len(components)} 个，隔离 {len(quarantined)} 组")
                stage = "COMPONENTS_SOLVED"

            if stage == "COMPONENTS_SOLVED":
                self._crashpoint(study_id, "STABILITY_DONE")
                current = json.loads(
                    self.store.get_study(study_id)["result_json"])
                stability = engine._stability(
                    frozen, current["components"],
                    current["graph"]["anchor_exhibit_ids"])
                result_summary = {
                    "tolerance": frozen["tolerance"],
                    "n_sessions": len(frozen["sessions"]),
                    "n_observations": len(frozen["observations"]),
                    "n_exhibits": len({o["exhibit_id"]
                                       for o in frozen["observations"]}),
                    "anchor_exhibit_ids":
                        current["graph"]["anchor_exhibit_ids"],
                    "published_boards": [
                        {"component_id": c["component_id"],
                         "sessions": c["sessions"],
                         "exhibit_count": len(c["board"])}
                        for c in current["components"] if not c["quarantined"]
                    ],
                }
                self.store.save_checkpoint(
                    study_id, "STABILITY_DONE", "RUNNING",
                    {"stability": stability, **result_summary})
                self.store.add_event(
                    study_id, "STABILITY_DONE",
                    f"留一锚点复算 {stability['anchor_runs']} 次，"
                    f"不稳展品 {len(stability['unstable'])} 个")
                stage = "STABILITY_DONE"

            if stage == "STABILITY_DONE":
                self._crashpoint(study_id, "PUBLISHED")
                ok = self.store.mark_published(study_id)
                if not ok:  # 并发/重试下已发布，绝不重复
                    return self.get_status(study_id)
        except Exception as exc:  # 失败态落盘，保留冻结版本，等待恢复/重试
            self.store.mark_failed(study_id, stage, str(exc))
            raise
        return self.get_status(study_id)

    # ----------------------------- 发布与恢复 ----------------------------- #
    def publish(self, study_id: str) -> Dict[str, Any]:
        row = self.store.get_study(study_id)
        if not row:
            raise KeyError(study_id)
        if row["status"] != "STABILITY_DONE":
            return {"published": False, "status": row["status"],
                    "reason": "稳定性分析未完成，不能发布"}
        ok = self.store.mark_published(study_id)
        return {"published": ok, "status": "PUBLISHED" if ok else "PUBLISHED"}

    def resume_all(self) -> List[Dict[str, Any]]:
        """进程启动/手动重试：沿用冻结版本恢复所有未发布研究。"""
        recovered = []
        for row in self.store.resumable_studies():
            if row["status"] == "PUBLISHED":
                continue
            try:
                self.run_pipeline(row["study_id"])
                recovered.append({"study_id": row["study_id"], "ok": True})
            except Exception as exc:
                recovered.append({"study_id": row["study_id"], "ok": False,
                                  "error": str(exc)})
        return recovered

    # ----------------------------- 查询 ----------------------------- #
    def get_status(self, study_id: str) -> Dict[str, Any]:
        row = self.store.get_study(study_id)
        if not row:
            raise KeyError(study_id)
        return {
            "study_id": row["study_id"], "name": row["name"],
            "status": row["status"], "stage": row["stage"],
            "tolerance": row["tolerance"],
            "input_sha256": row["input_sha256"],
            "version": row["version"],
            "error": row["error"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "published_at": row["published_at"],
        }

    def get_result(self, study_id: str) -> Dict[str, Any]:
        row = self.store.get_study(study_id)
        if not row:
            raise KeyError(study_id)
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        return {
            "study_id": study_id, "status": row["status"],
            "stage": row["stage"], "tolerance": row["tolerance"],
            "input_sha256": row["input_sha256"],
            "frozen_input": json.loads(row["input_json"]),
            "result": result,
            "events": self.store.list_events(study_id),
        }

    def list_studies(self) -> List[Dict[str, Any]]:
        return [self.get_status(r["study_id"]) for r in self.store.list_studies()]

    # ----------------------------- 测试辅助 ----------------------------- #
    def set_crashpoint(self, stage: Optional[str]) -> None:
        if stage:
            self.store.set_flag("crash_at", stage)
        else:
            self.store.clear_flag("crash_at")

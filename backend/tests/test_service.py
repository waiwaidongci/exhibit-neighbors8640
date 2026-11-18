"""服务层测试：冻结、检查点恢复、幂等、一次性发布。"""

import json
import os
import tempfile
import unittest

from src.calibration import demo, engine
from src.calibration.service import CalibrationService
from src.calibration.store import Store


class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "test.db")
        self.store = Store(self.db)
        self.svc = CalibrationService(self.store)
        self.payload = demo.build_payload()

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()


class FreezeAndPublishTests(ServiceTestBase):
    def test_freeze_then_full_publish(self):
        out = self.svc.submit(self.payload, run_async=False)
        sid = out["study_id"]
        row = self.store.get_study(sid)
        self.assertEqual(row["status"], "PUBLISHED")
        self.assertEqual(row["stage"], "PUBLISHED")
        # 冻结输入保留且 SHA256 稳定
        frozen = json.loads(row["input_json"])
        self.assertEqual(frozen["tolerance"], self.payload["tolerance"])
        self.assertEqual(len(row["input_sha256"]), 64)
        status = self.svc.get_status(sid)
        self.assertEqual(status["input_sha256"], row["input_sha256"])
        result = self.svc.get_result(sid)
        self.assertTrue(result["result"]["components"])

    def test_idempotent_retry_does_not_duplicate(self):
        first = self.svc.submit(self.payload, idem_key="key-1", run_async=False)
        second = self.svc.submit({**self.payload, "name": "改名也应复用"},
                                 idem_key="key-1", run_async=False)
        self.assertEqual(first["study_id"], second["study_id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(len(self.store.list_studies()), 1)

    def test_publish_is_once_only(self):
        out = self.svc.submit(self.payload, run_async=False)
        sid = out["study_id"]
        # 已发布再发布：不产生第二次发布事件
        events_before = len(self.store.list_events(sid))
        again = self.svc.publish(sid)
        self.assertFalse(again["published"])
        events_after = self.store.list_events(sid)
        pub_events = [e for e in events_after if e["event"] == "PUBLISHED"]
        self.assertEqual(len(pub_events), 1)
        self.assertEqual(len(events_after), events_before)


class CheckpointResumeTests(ServiceTestBase):
    def test_interrupt_and_resume_uses_frozen_version(self):
        out = self.svc.submit(self.payload, run_async=False)
        sid = out["study_id"]
        # 新研究：在图构建前注入中断
        self.svc.set_crashpoint("GRAPH_BUILT")
        crashed_out = self.svc.submit(
            {**self.payload, "name": "会中断的研究"}, run_async=False)
        self.assertEqual(crashed_out["status"], "FAILED")
        csid = crashed_out["study_id"]
        crashed = self.store.get_study(csid)
        self.assertEqual(crashed["stage"], "INTAKE_FROZEN")
        frozen_sha = crashed["input_sha256"]

        # 清除故障并恢复：沿用同一冻结输入，不重新受理
        self.svc.set_crashpoint(None)
        self.svc.run_pipeline(csid)
        recovered = self.store.get_study(csid)
        self.assertEqual(recovered["status"], "PUBLISHED")
        self.assertEqual(recovered["input_sha256"], frozen_sha)
        events = [e["event"] for e in self.store.list_events(csid)]
        self.assertIn("INTERRUPTED", events)
        self.assertEqual(events.count("PUBLISHED"), 1)

    def test_interrupt_at_each_stage_resumes_from_checkpoint(self):
        for stage in ("GRAPH_BUILT", "COMPONENTS_SOLVED",
                      "STABILITY_DONE", "PUBLISHED"):
            with self.subTest(stage=stage):
                self.svc.set_crashpoint(stage)
                out = self.svc.submit(
                    {**self.payload, "name": f"crash-{stage}"}, run_async=False)
                self.assertEqual(out["status"], "FAILED")
                sid = out["study_id"]
                self.svc.set_crashpoint(None)
                self.svc.run_pipeline(sid)
                row = self.store.get_study(sid)
                self.assertEqual(row["status"], "PUBLISHED",
                                 f"stage {stage} 未恢复成功")

    def test_resume_all_skips_published(self):
        done = self.svc.submit(self.payload, run_async=False)
        # 再制造一个中断研究
        self.svc.set_crashpoint("STABILITY_DONE")
        crashed = self.svc.submit(
            {**self.payload, "name": "中断二号"}, run_async=False)
        self.assertEqual(crashed["status"], "FAILED")
        self.svc.set_crashpoint(None)
        recovered = self.svc.resume_all()
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0]["ok"])
        # 已发布研究不被重复处理：发布事件仍只有一次
        events = [e for e in self.store.list_events(done["study_id"])
                  if e["event"] == "PUBLISHED"]
        self.assertEqual(len(events), 1)

    def test_restart_reopens_same_database(self):
        """模拟进程重启：新建 Store/Service 指向同一 DB 文件后恢复。"""
        self.svc.set_crashpoint("COMPONENTS_SOLVED")
        crashed = self.svc.submit(
            {**self.payload, "name": "跨进程中断"}, run_async=False)
        self.assertEqual(crashed["status"], "FAILED")
        # 故障注入持久化在 DB 中；“排障后重启”需先解除
        self.svc.set_crashpoint(None)
        self.store.close()

        store2 = Store(self.db)
        svc2 = CalibrationService(store2)
        recovered = svc2.resume_all()
        self.assertEqual(len(recovered), 1)
        self.assertTrue(recovered[0]["ok"])
        studies = store2.list_studies()
        self.assertTrue(all(s["status"] == "PUBLISHED" for s in studies))
        store2.close()


class InputFreezeTests(ServiceTestBase):
    def test_post_submit_input_changes_do_not_affect_study(self):
        payload = json.loads(json.dumps(self.payload))
        out = self.svc.submit(payload, run_async=False)
        sid = out["study_id"]
        sha_before = self.store.get_study(sid)["input_sha256"]
        # 调用方事后篡改内存对象
        payload["tolerance"] = 9.9
        payload["observations"][0]["score"] = 1.0
        row = self.store.get_study(sid)
        frozen = json.loads(row["input_json"])
        self.assertEqual(frozen["tolerance"], demo.TOLERANCE)
        self.assertEqual(row["input_sha256"], sha_before)


if __name__ == "__main__":
    unittest.main()

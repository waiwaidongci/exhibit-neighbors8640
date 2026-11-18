"""HTTP 端到端测试：受理冻结、轮询、结果与依据、幂等、静态页面、恢复端点。"""

import json
import os
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from src.calibration import demo
from src.calibration import server as server_mod


def _request(method, url, body=None, headers=None):
    data = None
    req_headers = headers or {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers = {"Content-Type": "application/json", **req_headers}
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpEndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.db = os.path.join(cls.tmp.name, "http.db")
        server_mod.reset_singletons()
        server_mod.get_service(cls.db)  # 固定测试数据库
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server_mod.Handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever,
                                      daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        server_mod.reset_singletons()
        cls.tmp.cleanup()

    def _wait_terminal(self, sid, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            code, body = _request("GET", f"{self.base}/api/studies/{sid}")
            self.assertEqual(code, 200)
            if body["status"] in ("PUBLISHED", "FAILED"):
                return body
            time.sleep(0.1)
        self.fail("研究未在超时内到达终态")

    def test_health_and_static(self):
        code, body = _request("GET", f"{self.base}/health")
        self.assertEqual((code, body), (200, {"ok": True}))
        with urllib.request.urlopen(f"{self.base}/", timeout=5) as resp:
            self.assertEqual(resp.status, 200)
            self.assertIn("校准台".encode(), resp.read())
        with urllib.request.urlopen(f"{self.base}/app.js", timeout=5) as resp:
            self.assertEqual(resp.status, 200)

    def test_full_study_flow(self):
        payload = demo.build_payload()
        code, body = _request("POST", f"{self.base}/api/studies", payload,
                              {"Idempotency-Key": "idem-e2e-1"})
        self.assertEqual(code, 201)
        sid = body["study_id"]
        final = self._wait_terminal(sid)
        self.assertEqual(final["status"], "PUBLISHED")

        code, result = _request(
            "GET", f"{self.base}/api/studies/{sid}/result")
        self.assertEqual(code, 200)
        comps = result["result"]["components"]
        # B 组隔离，其余出榜
        quarantined = [c for c in comps if c["quarantined"]]
        self.assertEqual(len(quarantined), 1)
        cy = quarantined[0]["conflict_cycles"][0]
        self.assertGreater(abs(cy["closure"]), result["tolerance"])
        # 同榜排名含逐展品依据
        a_comp = next(c for c in comps
                      if tuple(c["sessions"]) == ("S-A1", "S-A2", "S-A3"))
        self.assertGreater(len(a_comp["board"]), 0)
        top = a_comp["board"][0]
        self.assertEqual(top["rank"], 1)
        self.assertTrue(all("calibrated_score" in e for e in top["evidence"]))
        # 审计事件覆盖完整检查点
        events = [e["event"] for e in result["events"]]
        for stage in ("INTAKE_FROZEN", "GRAPH_BUILT", "COMPONENTS_SOLVED",
                      "STABILITY_DONE", "PUBLISHED"):
            self.assertIn(stage, events)
        # 稳定性结果可见
        unstable_ids = {u["exhibit_id"]
                        for u in result["result"]["stability"]["unstable"]}
        self.assertIn("D-LOCAL-E1", unstable_ids)
        self.assertIn("C-LOCAL-01", unstable_ids)

    def test_idempotency_key_no_duplicate_publish(self):
        payload = demo.build_payload()
        code1, b1 = _request("POST", f"{self.base}/api/studies", payload,
                             {"Idempotency-Key": "idem-dup"})
        code2, b2 = _request("POST", f"{self.base}/api/studies", payload,
                             {"Idempotency-Key": "idem-dup"})
        self.assertEqual(code1, 201)
        self.assertEqual(code2, 200)
        self.assertTrue(b2["deduplicated"])
        self.assertEqual(b1["study_id"], b2["study_id"])
        self._wait_terminal(b1["study_id"])
        code, studies = _request("GET", f"{self.base}/api/studies")
        self.assertEqual(code, 200)
        self.assertEqual(sum(1 for s in studies["studies"]
                             if s["study_id"] == b1["study_id"]), 1)

    def test_invalid_payload_422(self):
        code, body = _request("POST", f"{self.base}/api/studies",
                              {"sessions": [], "observations": []})
        self.assertEqual(code, 422)
        self.assertIn("error", body)

    def test_missing_study_404(self):
        code, _ = _request("GET", f"{self.base}/api/studies/nonexistent")
        self.assertEqual(code, 404)

    def test_demo_payload_endpoint(self):
        code, body = _request("GET", f"{self.base}/api/demo-payload")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["sessions"]), 11)


if __name__ == "__main__":
    unittest.main()

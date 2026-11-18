"""引擎层单元测试：WLS、分榜、闭环隔离、留一稳定性、受理校验。"""

import math
import unittest

from src.calibration import demo, engine


def obs(sid, exhibit, score):
    return {"session_id": sid, "exhibit_id": exhibit, "score": score}


def simple_payload():
    return {
        "name": "t",
        "sessions": [{"id": "S1"}, {"id": "S2"}, {"id": "S3"}],
        "observations": [
            # 三个共有锚点，真实偏差 b1=0, b2=+1, b3=-0.5
            *[obs(s, f"AN-{i}", level + {"S1": 0.0, "S2": 1.0, "S3": -0.5}[s])
              for i, level in enumerate([3.0, 3.5, 4.0])
              for s in ("S1", "S2", "S3")],
            obs("S1", "LOCAL-1", 2.0),
            obs("S2", "LOCAL-2", 4.5),
        ],
        "tolerance": 0.5,
    }


class WLSAndBoardsTests(unittest.TestCase):
    def test_recovers_known_biases(self):
        result = engine.run_pipeline(engine.validate_payload(simple_payload()))
        self.assertEqual(len(result["components"]), 1)
        comp = result["components"][0]
        self.assertFalse(comp["quarantined"])
        self.assertEqual(comp["ref_session"], "S1")
        self.assertAlmostEqual(comp["biases"]["S2"], 1.0, places=9)
        self.assertAlmostEqual(comp["biases"]["S3"], -0.5, places=9)
        # 校准后同一锚点在各场分数应一致
        for row in comp["board"]:
            if row["exhibit_id"].startswith("AN-"):
                vals = [e["calibrated_score"] for e in row["evidence"]]
                self.assertTrue(all(math.isclose(v, vals[0], abs_tol=1e-9)
                                    for v in vals))
        # 名次按校准均分降序、并列按 id
        means = [r["mean_calibrated"] for r in comp["board"]]
        self.assertEqual(means, sorted(means, reverse=True))

    def test_disconnected_components_separate_boards(self):
        payload = simple_payload()
        payload["sessions"].append({"id": "S4"})
        payload["observations"].append(obs("S4", "ISLAND-X", 5.0))
        result = engine.run_pipeline(engine.validate_payload(payload))
        comps = result["components"]
        self.assertEqual(len(comps), 2)
        sessions = {tuple(c["sessions"]) for c in comps}
        self.assertIn(("S1", "S2", "S3"), sessions)
        self.assertIn(("S4",), sessions)
        island = next(c for c in comps if c["sessions"] == ["S4"])
        self.assertEqual(len(island["board"]), 1)
        self.assertEqual(island["board"][0]["exhibit_id"], "ISLAND-X")
        self.assertEqual(island["biases"], {"S4": 0.0})

    def test_conflict_triangle_quarantined_others_published(self):
        result = engine.run_pipeline(
            engine.validate_payload(demo.build_payload()))
        by_sessions = {tuple(c["sessions"]): c for c in result["components"]}
        b = by_sessions[("S-B1", "S-B2", "S-B3")]
        self.assertTrue(b["quarantined"])
        self.assertEqual(b["board"], [])
        self.assertEqual(len(b["conflict_cycles"]), 1)
        cy = b["conflict_cycles"][0]
        self.assertAlmostEqual(abs(cy["closure"]), 1.0, places=9)
        self.assertTrue(cy["exceeded"])
        # 冲突环路必须列出全部三条边与涉及锚点
        self.assertEqual(len(cy["edges"]), 3)
        # 其余组照常出榜
        a = by_sessions[("S-A1", "S-A2", "S-A3")]
        self.assertFalse(a["quarantined"])
        self.assertTrue(len(a["board"]) > 0)
        self.assertIn(2, result["quarantined_component_ids"])

    def test_tolerance_boundary_not_exceeded(self):
        # 构造闭合偏差恰好等于容差的三角：不应隔离
        payload = {
            "name": "boundary",
            "sessions": [{"id": "X1"}, {"id": "X2"}, {"id": "X3"}],
            "observations": [
                obs("X1", "E12", 3.0), obs("X2", "E12", 3.5),   # delta=-0.5
                obs("X2", "E23", 3.0), obs("X3", "E23", 2.5),   # delta=+0.5
                obs("X1", "E13", 3.0), obs("X3", "E13", 3.0),   # delta=0
            ],
            "tolerance": 0.5,  # 闭合 = -0.5+0.5-0 = 0；改为验证等值边界见下
        }
        comps, _ = engine.analyze_components(engine.validate_payload(payload))
        self.assertFalse(comps[0]["quarantined"])

        # 直接构造闭合恰为 0.5 的情形（|closure| == tolerance 不算超差）
        payload2 = {
            "name": "boundary-eq",
            "sessions": [{"id": "Y1"}, {"id": "Y2"}, {"id": "Y3"}],
            "observations": [
                obs("Y1", "E12", 3.0), obs("Y2", "E12", 3.0),   # delta=0
                obs("Y2", "E23", 3.0), obs("Y3", "E23", 2.5),   # delta=+0.5
                obs("Y1", "E13", 3.0), obs("Y3", "E13", 3.0),   # delta=0
            ],
            "tolerance": 0.5,  # 闭合 = 0+0.5-0 = 0.5
        }
        comps2, _ = engine.analyze_components(engine.validate_payload(payload2))
        self.assertAlmostEqual(comps2[0]["max_abs_closure"], 0.5, places=9)
        self.assertFalse(comps2[0]["quarantined"])

    def test_stability_bridge_and_reversal(self):
        result = engine.run_pipeline(
            engine.validate_payload(demo.build_payload()))
        unstable = {u["exhibit_id"]: u for u in result["stability"]["unstable"]}
        # 桥锚点移除 → 失去可比性
        self.assertIn("C-LOCAL-01", unstable)
        self.assertIn("LOST_COMPARABILITY",
                      unstable["C-LOCAL-01"]["reasons"])
        trigger = unstable["C-LOCAL-01"]["triggers"][0]
        self.assertEqual(trigger["removed_anchor"], "C-ANCHOR-BRIDGE")
        # 强锚点移除 → D 组本地展品名次逆转
        self.assertIn("D-LOCAL-E1", unstable)
        self.assertIn("RANK_REVERSAL", unstable["D-LOCAL-E1"]["reasons"])
        d_trigger = next(t for t in unstable["D-LOCAL-E1"]["triggers"]
                         if t["reason"] == "RANK_REVERSAL")
        self.assertEqual(d_trigger["removed_anchor"], "D-ANCHOR-STRONG")
        # 一致的 A 组展品应保持稳定
        self.assertIn("A-LOCAL-01", result["stability"]["stable_exhibit_ids"])

    def test_deterministic(self):
        p = engine.validate_payload(demo.build_payload())
        import json
        r1 = json.dumps(engine.run_pipeline(p), sort_keys=True)
        r2 = json.dumps(engine.run_pipeline(p), sort_keys=True)
        self.assertEqual(r1, r2)

    def test_explicit_anchor_subset(self):
        payload = engine.validate_payload({
            **demo.build_payload(),
            "anchor_exhibit_ids": ["C-ANCHOR-BRIDGE"],
        })
        result = engine.run_pipeline(payload)
        self.assertEqual(result["anchor_exhibit_ids"], ["C-ANCHOR-BRIDGE"])
        # 仅 C 两场相连，其余场次全部孤立
        comp_sizes = sorted(len(c["sessions"]) for c in result["components"])
        self.assertEqual(comp_sizes.count(1), 9)  # 11 场 - 2 场 C 组


class ValidationTests(unittest.TestCase):
    def test_bad_payloads(self):
        base = simple_payload()
        bad_cases = [
            ({**base, "sessions": []}, "非空数组"),
            ({**base, "observations": [
                {**base["observations"][0], "session_id": "NOPE"}]}, "未知场次"),
            ({**base, "observations": [
                obs("S1", "X", float("nan"))]}, "有限数值"),
            ({**base, "tolerance": -1}, "非负"),
            ({**base, "anchor_exhibit_ids": ["GHOST"]}, "没有任何评分"),
        ]
        for bad, needle in bad_cases:
            with self.subTest(needle=needle):
                with self.assertRaises(engine.CalibrationError) as ctx:
                    engine.validate_payload(bad)
                self.assertIn(needle, str(ctx.exception))

    def test_duplicate_session_and_observation(self):
        with self.assertRaisesRegex(engine.CalibrationError, "重复"):
            engine.validate_payload({
                **simple_payload(),
                "sessions": [{"id": "S1"}, {"id": "S1"}]})
        dup = simple_payload()
        dup["observations"].append(obs("S1", "LOCAL-1", 1.0))
        with self.assertRaisesRegex(engine.CalibrationError, "重复评分"):
            engine.validate_payload(dup)

    def test_boolean_score_rejected(self):
        with self.assertRaisesRegex(engine.CalibrationError, "数值"):
            engine.validate_payload({
                **simple_payload(),
                "observations": [obs("S1", "B", True)]})


if __name__ == "__main__":
    unittest.main()

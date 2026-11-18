"""固定演示数据（确定性，无随机数依赖）。

结构
----
- 组A（一致、出榜）：S-A1/A2/A3，真实偏差 0 / +0.8 / -0.6，共有锚点连成链且
  含一个闭合良好的环（闭合偏差≈0），用于验证 WLS 能还原真实偏差。
- 组B（冲突、隔离）：S-B1/B2/B3 三角，三条边各用独立锚点并直接指定边差，
  闭合偏差 = 1.0 > 容差 0.5，整组隔离并报告冲突环路；其余组照常出榜。
- 组C（失去可比性）：S-C1/C2 仅靠唯一锚点 C-ANCHOR-BRIDGE 相连，移除它两场
  断连，组内展品全部标不稳（LOST_COMPARABILITY）。
- 组D（名次逆转）：S-D1/D2 两个强度不同的锚点；移除强锚点后弱锚点给出的
  场次偏差估计反转，D-LOCAL 两件展品的同榜名次翻转（RANK_REVERSAL）。
- 孤立场 S-SOLO：无任何共有展品，独立成榜，无法与其他场次同榜。
"""

from __future__ import annotations

from typing import Any, Dict, List

TOLERANCE = 0.5

_SESSIONS = [
    ("S-A1", "甲城一号场"), ("S-A2", "甲城二号场"), ("S-A3", "甲城三号场"),
    ("S-B1", "乙城一号场"), ("S-B2", "乙城二号场"), ("S-B3", "乙城三号场"),
    ("S-C1", "丙城一号场"), ("S-C2", "丙城二号场"),
    ("S-D1", "丁城一号场"), ("S-D2", "丁城二号场"),
    ("S-SOLO", "戊城孤立场"),
]

_BIAS = {
    "S-A1": 0.0, "S-A2": 0.8, "S-A3": -0.6,
    "S-B1": 0.0,
    "S-C1": 0.0, "S-C2": 0.4,
    "S-D1": 0.0, "S-D2": 0.4,
    "S-SOLO": 0.0,
}


def _true_level(code: str) -> float:
    """由展品编号确定性生成 2.0~4.5 的真实水平。"""
    digits = [int(ch) for ch in code if ch.isdigit()]
    base = (sum(digits) * 37) % 26
    return 2.0 + base / 10.0


def _add(obs: List[Dict[str, Any]], sid: str, exhibit: str,
         score: float) -> None:
    obs.append({"session_id": sid, "exhibit_id": exhibit,
                "score": round(score, 3)})


def build_payload() -> Dict[str, Any]:
    sessions = [{"id": sid, "name": name} for sid, name in _SESSIONS]
    obs: List[Dict[str, Any]] = []

    # ---------------- 组 A：一致组（含闭合良好的环） ----------------
    a_shared = ["A-ANCHOR-01", "A-ANCHOR-02", "A-ANCHOR-03"]
    for exhibit in a_shared:  # 三场共有：同时贡献三条边，构成环
        t = _true_level(exhibit)
        for sid in ("S-A1", "S-A2", "S-A3"):
            _add(obs, sid, exhibit, t + _BIAS[sid])
    # A1/A2 独有锚点（不与 A3 共展，避免改变环结构）
    for exhibit in ("A-ANCHOR-10", "A-ANCHOR-11"):
        t = _true_level(exhibit)
        for sid in ("S-A1", "S-A2"):
            _add(obs, sid, exhibit, t + _BIAS[sid])
    # 单场独有展品，只在各自场次出现，不充当锚点
    for k in range(1, 5):
        _add(obs, "S-A1", f"A-LOCAL-{k:02d}",
             _true_level(f"A-LOCAL-{k:02d}") + _BIAS["S-A1"])
    for k in range(5, 8):
        _add(obs, "S-A2", f"A-LOCAL-{k:02d}",
             _true_level(f"A-LOCAL-{k:02d}") + _BIAS["S-A2"])
    for k in range(8, 11):
        _add(obs, "S-A3", f"A-LOCAL-{k:02d}",
             _true_level(f"A-LOCAL-{k:02d}") + _BIAS["S-A3"])

    # ---------------- 组 B：冲突三角，三条边独立锚点 ----------------
    # 边差约定 delta(a,b)=mean(score_a-score_b)：
    #   B1-B2 = -0.9（B2 松 0.9），B2-B3 = +1.9，B1-B3 = 0.0
    # 闭合（B1→B2→B3→B1）= -0.9 + 1.9 - 0.0 = 1.0 > 0.5 容差
    edge_specs = [
        ("B-ANCHOR-12", "S-B1", "S-B2", -0.9),
        ("B-ANCHOR-23", "S-B2", "S-B3", 1.9),
        ("B-ANCHOR-13", "S-B1", "S-B3", 0.0),
    ]
    for exhibit, a, b, delta in edge_specs:
        base = _true_level(exhibit)
        _add(obs, a, exhibit, base)
        _add(obs, b, exhibit, base - delta)  # score_a - score_b = delta
    # B 组单场展品（隔离后不出榜，但保证组内有可评展品）
    for k in range(1, 4):
        _add(obs, "S-B1", f"B-LOCAL-{k:02d}", _true_level(f"B-LOCAL-{k:02d}"))
        _add(obs, "S-B2", f"B-LOCAL-{k:02d}",
             _true_level(f"B-LOCAL-{k:02d}") + 0.9)

    # ---------------- 组 C：唯一桥锚点，移除即断连 ----------------
    bridge = "C-ANCHOR-BRIDGE"
    t = _true_level(bridge)
    _add(obs, "S-C1", bridge, t + _BIAS["S-C1"])
    _add(obs, "S-C2", bridge, t + _BIAS["S-C2"])
    # C-LOCAL 只在单场出现，避免产生第二条跨场边
    for k in range(1, 3):
        _add(obs, "S-C1", f"C-LOCAL-{k:02d}",
             _true_level(f"C-LOCAL-{k:02d}"))
    for k in range(3, 5):
        _add(obs, "S-C2", f"C-LOCAL-{k:02d}",
             _true_level(f"C-LOCAL-{k:02d}") + _BIAS["S-C2"])

    # ---------------- 组 D：留一锚点名次逆转 ----------------
    # 两个单展品锚点给出相反口径：
    #   D-ANCHOR-STRONG：delta(D1,D2)=-0.4（D2 偏松）
    #   D-ANCHOR-WEAK  ：delta(D1,D2)=+0.9（D1 反而偏高）
    # 基线边差为两者均值 +0.25（b2=-0.25）；移除 STRONG 后仅剩 WEAK，
    # b2 变为 -0.9，D-LOCAL 两件单场展品的同榜名次翻转。
    _add(obs, "S-D1", "D-ANCHOR-STRONG", 3.2)
    _add(obs, "S-D2", "D-ANCHOR-STRONG", 3.6)  # delta = -0.4
    _add(obs, "S-D1", "D-ANCHOR-WEAK", 3.0)
    _add(obs, "S-D2", "D-ANCHOR-WEAK", 2.1)    # delta = +0.9
    # 两件本地展品分属不同单场：偏差估计只平移 D2 展品，从而可逆转
    _add(obs, "S-D1", "D-LOCAL-E1", 3.6)
    _add(obs, "S-D2", "D-LOCAL-E2", 3.2)

    # ---------------- 孤立场 ----------------
    for k in range(1, 4):
        exhibit = f"SOLO-{k:02d}"
        _add(obs, "S-SOLO", exhibit, _true_level(exhibit) + 3.0)

    return {
        "name": "十一月跨场校准演示研究",
        "sessions": sessions,
        "observations": obs,
        "tolerance": TOLERANCE,
        # 不提供 anchor_exhibit_ids：自动以全部共有展品为锚点
    }


if __name__ == "__main__":
    import json
    print(json.dumps(build_payload(), ensure_ascii=False, indent=2))

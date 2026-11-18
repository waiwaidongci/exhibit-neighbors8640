"""跨试展场评分校准引擎（仅依赖标准库）。

口径
----
设场次 s 的偏差为 b_s（该场整体偏松则 b_s 为正），展品 e 的真实水平为 t_e。
观测模型：score(o) = t_e + b_s + 噪声。
共有展品（锚点）在两场 a,b 同时展出时给出观测差
    d_ab = mean(score_a - score_b)，权重 w = 共有锚点数。
连通分量内以引用场（编号最小者，b_ref=0）为基准，加权最小二乘求解 b_s：
    min Σ_edges w * (d_ab - (b_a - b_b))^2
不同连通分量互不连通，无法跨分量比较，分别出榜。
对生成树基本回路（fundamental cycle basis）计算闭合偏差；任一回路 |closure|
超过受理时冻结的容差，则整个分量隔离不出榜，并报告冲突环路，其余分量照常。

稳定性：逐一移除锚点重算；展品失去可比性（分量隔离/掉出连通榜）或在同榜内
与其他展品发生名次逆转（均值大小关系翻转）即标为不稳。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

EPS = 1e-9
DEFAULT_TOLERANCE = 0.5
MAX_ANCHOR_RUNS_DEFAULT = 200


class CalibrationError(ValueError):
    """输入或结构不满足校准前提。"""


# --------------------------------------------------------------------------- #
# 受理校验与规范化（冻结前的唯一入口，保证冻结载荷自洽）
# --------------------------------------------------------------------------- #
def validate_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        raise CalibrationError("载荷必须是对象")
    sessions = payload.get("sessions")
    observations = payload.get("observations")
    if not isinstance(sessions, list) or not sessions:
        raise CalibrationError("sessions 必须是非空数组")
    if not isinstance(observations, list) or not observations:
        raise CalibrationError("observations 必须是非空数组")

    session_ids: List[str] = []
    seen_sessions = set()
    for i, s in enumerate(sessions):
        if not isinstance(s, dict):
            raise CalibrationError(f"sessions[{i}] 必须是对象")
        sid = s.get("id")
        if not isinstance(sid, str) or not sid.strip():
            raise CalibrationError(f"sessions[{i}].id 必须是非空字符串")
        if sid in seen_sessions:
            raise CalibrationError(f"场次 id 重复: {sid}")
        seen_sessions.add(sid)
        session_ids.append(sid)

    seen_obs: set = set()
    exhibits: Dict[str, Dict[str, Any]] = {}
    norm_obs: List[Dict[str, Any]] = []
    for i, o in enumerate(observations):
        if not isinstance(o, dict):
            raise CalibrationError(f"observations[{i}] 必须是对象")
        sid = o.get("session_id")
        exhibit = o.get("exhibit_id")
        score = o.get("score")
        if sid not in seen_sessions:
            raise CalibrationError(f"observations[{i}] 引用未知场次: {sid!r}")
        if not isinstance(exhibit, str) or not exhibit.strip():
            raise CalibrationError(f"observations[{i}].exhibit_id 必须是非空字符串")
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            raise CalibrationError(f"observations[{i}].score 必须是数值")
        if not math.isfinite(score):
            raise CalibrationError(f"observations[{i}].score 必须是有限数值")
        key = (sid, exhibit)
        if key in seen_obs:
            raise CalibrationError(f"同一场次对同一展品重复评分: {sid}/{exhibit}")
        seen_obs.add(key)
        exhibits.setdefault(exhibit, {"exhibit_id": exhibit})
        norm_obs.append(
            {"session_id": sid, "exhibit_id": exhibit, "score": float(score)}
        )

    tolerance = payload.get("tolerance", DEFAULT_TOLERANCE)
    if isinstance(tolerance, bool) or not isinstance(tolerance, (int, float)):
        raise CalibrationError("tolerance 必须是非负数值")
    if not math.isfinite(tolerance) or tolerance < 0:
        raise CalibrationError("tolerance 必须是非负有限数值")

    explicit_anchors = payload.get("anchor_exhibit_ids")
    anchors: Optional[List[str]] = None
    if explicit_anchors is not None:
        if not isinstance(explicit_anchors, list) or not explicit_anchors:
            raise CalibrationError("anchor_exhibit_ids 必须是非空数组或省略")
        anchors = []
        for a in explicit_anchors:
            if not isinstance(a, str) or not a.strip():
                raise CalibrationError("锚点展品 id 必须是非空字符串")
            if a not in exhibits:
                raise CalibrationError(f"锚点展品没有任何评分记录: {a}")
            if a in anchors:
                raise CalibrationError(f"锚点展品重复: {a}")
            anchors.append(a)

    max_runs = payload.get("max_anchor_runs", MAX_ANCHOR_RUNS_DEFAULT)
    if not isinstance(max_runs, int) or isinstance(max_runs, bool) or max_runs <= 0:
        raise CalibrationError("max_anchor_runs 必须是正整数")

    return {
        "name": payload.get("name") or "未命名研究",
        "sessions": [{"id": s["id"], "name": s.get("name") or s["id"]}
                     for s in sessions],
        "observations": norm_obs,
        "tolerance": float(tolerance),
        "anchor_exhibit_ids": anchors,
        "max_anchor_runs": max_runs,
    }


# --------------------------------------------------------------------------- #
# 图构建：展品 -> 场次对边
# --------------------------------------------------------------------------- #
def _anchor_set(payload: Dict[str, Any],
                excluded: Optional[Sequence[str]] = None) -> List[str]:
    excluded = set(excluded or ())
    if payload.get("anchor_exhibit_ids"):
        return [a for a in payload["anchor_exhibit_ids"] if a not in excluded]
    multi: Dict[str, set] = {}
    for o in payload["observations"]:
        if o["exhibit_id"] in excluded:
            continue
        multi.setdefault(o["exhibit_id"], set()).add(o["session_id"])
    return sorted(e for e, ss in multi.items() if len(ss) >= 2)


def build_edges(payload: Dict[str, Any],
                excluded_anchors: Optional[Sequence[str]] = None
                ) -> Tuple[List[Dict[str, Any]], List[str]]:
    """返回无向边列表（按场次 id 规范化 a<b）与实际使用的锚点。"""
    anchors = _anchor_set(payload, excluded_anchors)
    anchor_set = set(anchors)
    # exhibit -> session -> score
    by_exhibit: Dict[str, Dict[str, float]] = {}
    for o in payload["observations"]:
        if o["exhibit_id"] not in anchor_set:
            continue
        by_exhibit.setdefault(o["exhibit_id"], {})[o["session_id"]] = o["score"]

    # (a,b) a<b -> 累加器
    acc: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for exhibit in sorted(anchor_set):
        sess_scores = by_exhibit.get(exhibit, {})
        sessions_here = sorted(sess_scores)
        for i in range(len(sessions_here)):
            for j in range(i + 1, len(sessions_here)):
                a, b = sessions_here[i], sessions_here[j]
                d = sess_scores[a] - sess_scores[b]
                edge = acc.setdefault((a, b), {
                    "a": a, "b": b, "weight": 0.0, "wd": 0.0,
                    "anchor_evidence": [],
                })
                edge["weight"] += 1.0
                edge["wd"] += d
                edge["anchor_evidence"].append({
                    "anchor_exhibit_id": exhibit,
                    "delta": d,
                    "score_a": sess_scores[a],
                    "score_b": sess_scores[b],
                })

    edges = []
    for key in sorted(acc):
        e = acc[key]
        e["delta"] = e["wd"] / e["weight"]
        e["weight"] = int(e["weight"])
        del e["wd"]
        edges.append(e)
    return edges, anchors


def _components(session_ids: List[str],
                edges: List[Dict[str, Any]]) -> List[List[str]]:
    adj: Dict[str, List[str]] = {s: [] for s in session_ids}
    for e in edges:
        adj[e["a"]].append(e["b"])
        adj[e["b"]].append(e["a"])
    seen: set = set()
    comps: List[List[str]] = []
    for start in session_ids:
        if start in seen:
            continue
        queue = [start]
        seen.add(start)
        comp: List[str] = []
        while queue:
            u = queue.pop(0)
            comp.append(u)
            for v in sorted(adj[u]):
                if v not in seen:
                    seen.add(v)
                    queue.append(v)
        comps.append(sorted(comp))
    return comps


# --------------------------------------------------------------------------- #
# 加权最小二乘：Laplacian 正规方程，去引用场行列后高斯消元
# --------------------------------------------------------------------------- #
def _solve_gaussian(a: List[List[float]],
                    b: List[float]) -> List[float]:
    n = len(b)
    if n == 0:
        return []
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(m[r][col]))
        if abs(m[pivot][col]) < 1e-12:
            raise CalibrationError("偏差方程组奇异（场次连通性异常）")
        if pivot != col:
            m[col], m[pivot] = m[pivot], m[col]
        piv = m[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = m[r][col] / piv
            if factor == 0.0:
                continue
            for k in range(col, n + 1):
                m[r][k] -= factor * m[col][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def solve_biases(comp_sessions: List[str],
                 comp_edges: List[Dict[str, Any]]) -> Tuple[str, Dict[str, float]]:
    ref = comp_sessions[0]  # 已排序，最小 id 作引用场
    others = [s for s in comp_sessions if s != ref]
    idx = {s: i for i, s in enumerate(others)}
    n = len(others)
    mat = [[0.0] * n for _ in range(n)]
    rhs = [0.0] * n
    for e in comp_edges:
        u, v, w, d = e["a"], e["b"], float(e["weight"]), e["delta"]
        if u in idx:
            i = idx[u]
            mat[i][i] += w
            rhs[i] += w * d
        if v in idx:
            j = idx[v]
            mat[j][j] += w
            rhs[j] -= w * d
        if u in idx and v in idx:
            mat[i][j] -= w
            mat[j][i] -= w
    solved = _solve_gaussian(mat, rhs) if n else []
    biases = {ref: 0.0}
    for s, val in zip(others, solved):
        biases[s] = val
    return ref, biases


# --------------------------------------------------------------------------- #
# 生成树基本回路与闭合偏差
# --------------------------------------------------------------------------- #
def _cycle_basis(comp_sessions: List[str],
                 comp_edges: List[Dict[str, Any]],
                 tolerance: float) -> List[Dict[str, Any]]:
    adj: Dict[str, List[Tuple[str, int]]] = {s: [] for s in comp_sessions}
    for ei, e in enumerate(comp_edges):
        adj[e["a"]].append((e["b"], ei))
        adj[e["b"]].append((e["a"], ei))

    root = comp_sessions[0]
    parent: Dict[str, Optional[str]] = {root: None}
    parent_edge: Dict[str, Optional[int]] = {root: None}
    depth = {root: 0}
    queue = [root]
    while queue:
        u = queue.pop(0)
        for v, ei in sorted(adj[u]):
            if v not in parent:
                parent[v] = u
                parent_edge[v] = ei
                depth[v] = depth[u] + 1
                queue.append(v)

    edge_by_id = {ei: e for ei, e in enumerate(comp_edges)}

    def directed_contribution(edge: Dict[str, Any], x: str, y: str) -> float:
        """沿 x->y 方向走过该边对闭合偏差的贡献。"""
        d = edge["delta"]  # mean(score_a - score_b)，a<b
        return d if (x, y) == (edge["a"], edge["b"]) else -d

    cycles: List[Dict[str, Any]] = []
    # 树边集合 = 各非根节点入树所用边；其余边即为弦，每条弦对应一个基本回路
    tree_edges = {pe for pe in parent_edge.values() if pe is not None}
    chord_ids = [ei for ei in range(len(comp_edges)) if ei not in tree_edges]

    for cycle_no, ei in enumerate(chord_ids, start=1):
        chord = edge_by_id[ei]
        u, v = chord["a"], chord["b"]
        # 节点路径 u -> LCA -> v
        up, vp = u, v
        path_u: List[str] = []
        path_v: List[str] = []
        du, dv = depth[up], depth[vp]
        while du > dv:
            path_u.append(up)
            up = parent[up]
            du -= 1
        while dv > du:
            path_v.append(vp)
            vp = parent[vp]
            dv -= 1
        while up != vp:
            path_u.append(up)
            path_v.append(vp)
            up, vp = parent[up], parent[vp]
        lca = up
        node_path = path_u + [lca] + list(reversed(path_v))

        closure = 0.0
        edge_walk: List[Dict[str, Any]] = []
        for x, y in zip(node_path, node_path[1:]):
            walked = next(ed for ed in comp_edges
                          if {ed["a"], ed["b"]} == {x, y})
            sign = 1 if (x, y) == (walked["a"], walked["b"]) else -1
            closure += directed_contribution(walked, x, y)
            edge_walk.append({
                "a": walked["a"], "b": walked["b"],
                "traversed": [x, y], "sign": sign,
                "delta": walked["delta"], "weight": walked["weight"],
                "anchor_exhibit_ids":
                    [ev["anchor_exhibit_id"] for ev in walked["anchor_evidence"]],
            })
        # 弦边沿 v -> u 闭合回路
        sign_chord = 1 if (v, u) == (chord["a"], chord["b"]) else -1
        closure += directed_contribution(chord, v, u)
        edge_walk.append({
            "a": chord["a"], "b": chord["b"],
            "traversed": [v, u], "sign": sign_chord,
            "delta": chord["delta"], "weight": chord["weight"],
            "anchor_exhibit_ids":
                [ev["anchor_exhibit_id"] for ev in chord["anchor_evidence"]],
        })
        full_path = node_path + [u]
        cycles.append({
            "cycle_no": cycle_no,
            "sessions": full_path,
            "edges": edge_walk,
            "closure": closure,
            "abs_closure": abs(closure),
            "tolerance": tolerance,
            "exceeded": abs(closure) > tolerance + EPS,
        })
    return cycles


# --------------------------------------------------------------------------- #
# 单分量完整求解
# --------------------------------------------------------------------------- #
def _board(payload: Dict[str, Any], comp_sessions: List[str],
           biases: Dict[str, float]) -> List[Dict[str, Any]]:
    in_comp = set(comp_sessions)
    per_exhibit: Dict[str, List[Dict[str, Any]]] = {}
    for o in payload["observations"]:
        if o["session_id"] not in in_comp:
            continue
        cal = o["score"] - biases[o["session_id"]]
        per_exhibit.setdefault(o["exhibit_id"], []).append({
            "session_id": o["session_id"],
            "raw_score": o["score"],
            "session_bias": biases[o["session_id"]],
            "calibrated_score": cal,
        })
    rows = []
    for exhibit, ev_list in per_exhibit.items():
        mean_cal = sum(e["calibrated_score"] for e in ev_list) / len(ev_list)
        rows.append({
            "exhibit_id": exhibit,
            "mean_calibrated": mean_cal,
            "n_observations": len(ev_list),
            "evidence": sorted(ev_list, key=lambda e: e["session_id"]),
        })
    rows.sort(key=lambda r: (-r["mean_calibrated"], r["exhibit_id"]))
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def analyze_components(payload: Dict[str, Any],
                       excluded_anchors: Optional[Sequence[str]] = None
                       ) -> Tuple[List[Dict[str, Any]], List[str]]:
    session_ids = [s["id"] for s in payload["sessions"]]
    edges, anchors_used = build_edges(payload, excluded_anchors)
    comps = _components(session_ids, edges)
    tolerance = payload["tolerance"]

    out: List[Dict[str, Any]] = []
    for cid, comp_sessions in enumerate(comps, start=1):
        comp_set = set(comp_sessions)
        comp_edges = [e for e in edges
                      if e["a"] in comp_set and e["b"] in comp_set]
        ref, biases = solve_biases(comp_sessions, comp_edges)
        for e in comp_edges:
            e["residual"] = e["delta"] - (biases[e["a"]] - biases[e["b"]])
        cycles = _cycle_basis(comp_sessions, comp_edges, tolerance)
        conflict = [c for c in cycles if c["exceeded"]]
        quarantined = bool(conflict)
        out.append({
            "component_id": cid,
            "sessions": comp_sessions,
            "ref_session": ref,
            "edges": [_public_edge(e) for e in comp_edges],
            "biases": {s: biases[s] for s in comp_sessions},
            "cycle_basis": cycles,
            "max_abs_closure": max((c["abs_closure"] for c in cycles),
                                   default=0.0),
            "quarantined": quarantined,
            "conflict_cycles": conflict,
            "board": [] if quarantined else _board(payload, comp_sessions, biases),
        })
    return out, anchors_used


def _public_edge(e: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "a": e["a"], "b": e["b"], "delta": e["delta"],
        "weight": e["weight"], "residual": e.get("residual"),
        "anchor_evidence": e["anchor_evidence"],
    }


# --------------------------------------------------------------------------- #
# 留一锚点稳定性
# --------------------------------------------------------------------------- #
def _stability(payload: Dict[str, Any],
               baseline: List[Dict[str, Any]],
               anchors: List[str]) -> Dict[str, Any]:
    max_runs = payload["max_anchor_runs"]
    skipped = anchors[max_runs:]
    targets = anchors[:max_runs]

    # 基线：展品 -> (component_id, mean, board rank map)
    base_loc: Dict[str, Tuple[int, float]] = {}
    base_board: Dict[int, Dict[str, float]] = {}
    for comp in baseline:
        if comp["quarantined"]:
            continue
        means = {row["exhibit_id"]: row["mean_calibrated"]
                 for row in comp["board"]}
        base_board[comp["component_id"]] = means
        for ex, mean in means.items():
            base_loc[ex] = (comp["component_id"], mean)

    unstable: Dict[str, Dict[str, Any]] = {}

    def flag(exhibit: str, anchor: str, reason: str, detail: str) -> None:
        rec = unstable.setdefault(exhibit, {
            "exhibit_id": exhibit, "reasons": [], "triggers": [],
        })
        if reason not in rec["reasons"]:
            rec["reasons"].append(reason)
        # 同一展品、同一被移除锚点、同一原因只保留一条触发记录
        for tr in rec["triggers"]:
            if tr["removed_anchor"] == anchor and tr["reason"] == reason:
                if detail not in tr["detail"]:
                    tr["detail"] += "；" + detail
                return
        rec["triggers"].append({
            "removed_anchor": anchor, "reason": reason, "detail": detail,
        })

    for anchor in targets:
        rerun, _ = analyze_components(payload, excluded_anchors=[anchor])
        rerun_comp = {c["component_id"]: c for c in rerun}
        # 重跑后分量编号可能变化（边减少改变遍历），按场次集合匹配
        comp_by_sessions = {
            tuple(c["sessions"]): c for c in rerun
        }
        rerun_loc: Dict[str, Tuple[int, float, bool]] = {}
        rerun_boards: Dict[int, Dict[str, float]] = {}
        for c in rerun:
            if c["quarantined"]:
                continue
            means = {row["exhibit_id"]: row["mean_calibrated"]
                     for row in c["board"]}
            rerun_boards[c["component_id"]] = means
            for ex, mean in means.items():
                rerun_loc[ex] = (c["component_id"], mean, False)

        for exhibit, (base_cid, base_mean) in base_loc.items():
            base_comp = next(c for c in baseline
                             if c["component_id"] == base_cid)
            rerun_comp_match = comp_by_sessions.get(tuple(base_comp["sessions"]))
            if rerun_comp_match is None or rerun_comp_match["quarantined"]:
                flag(exhibit, anchor, "LOST_COMPARABILITY",
                     "移除锚点后所属分量被隔离，无法同榜比较")
                continue
            r_cid = rerun_comp_match["component_id"]
            r_means = rerun_boards.get(r_cid, {})
            if exhibit not in r_means:
                flag(exhibit, anchor, "LOST_COMPARABILITY",
                     "移除锚点后展品掉出连通分量，失去可比场次")
                continue
            # 同榜名次逆转：与任一仍同榜展品的均值大小关系翻转
            for other, other_base_mean in base_board[base_cid].items():
                if other == exhibit or other not in r_means:
                    continue
                base_diff = base_mean - other_base_mean
                new_diff = r_means[exhibit] - r_means[other]
                if abs(base_diff) > EPS and abs(new_diff) > EPS and \
                        base_diff * new_diff < 0:
                    flag(exhibit, anchor, "RANK_REVERSAL",
                         f"与展品 {other} 的先后关系翻转"
                         f"（基线差 {base_diff:+.4f} → 重算差 {new_diff:+.4f}）")

    stable = sorted(e for e in base_loc if e not in unstable)
    return {
        "anchor_runs": len(targets),
        "skipped_anchor_exhibit_ids": skipped,
        "unstable": [unstable[e] for e in sorted(unstable)],
        "stable_exhibit_ids": stable,
    }


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def run_pipeline(payload: Dict[str, Any]) -> Dict[str, Any]:
    """对已校验的冻结载荷执行完整校准，返回可 JSON 序列化结果。"""
    components, anchors = analyze_components(payload)
    stability = _stability(payload, components, anchors)
    return {
        "tolerance": payload["tolerance"],
        "n_sessions": len(payload["sessions"]),
        "n_observations": len(payload["observations"]),
        "n_exhibits": len({o["exhibit_id"]
                           for o in payload["observations"]}),
        "anchor_exhibit_ids": anchors,
        "components": components,
        "quarantined_component_ids": [c["component_id"] for c in components
                                      if c["quarantined"]],
        "published_boards": [
            {
                "component_id": c["component_id"],
                "sessions": c["sessions"],
                "exhibit_count": len(c["board"]),
            }
            for c in components if not c["quarantined"]
        ],
        "stability": stability,
    }

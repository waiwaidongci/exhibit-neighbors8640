"""试展评分跨场校准（纯标准库）。

模块划分：
- engine:  共有展品锚点图、加权最小二乘场次偏差、连通分榜、闭环隔离、留一锚点稳定性
- store:   SQLite 冻结输入/检查点/事件/幂等键
- service:受理冻结、分阶段检查点、中断恢复、一次性发布
- server:  http.server 提供的 REST + 静态页面
- demo:    固定演示数据（含一致组、孤立场、冲突三角与名次逆转样例）
"""

__all__ = ["engine", "store", "service", "server", "demo"]

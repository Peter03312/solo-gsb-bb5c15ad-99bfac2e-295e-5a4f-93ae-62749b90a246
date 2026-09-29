"""破坏项对账：基线审查 vs 当前审查的破坏性发现差异对账。

稳定身份 = 端点 + 请求/响应方向 + 规则 + 语义定位（location），
与发现项 ID、列表顺序无关——同一份文档重新审查、发现序号变化不影响配对。

同身份的重复发现按出现次数一一配对（多重集差）：
  - 配对成功   → 延续（continuing）：基线与当前都存在
  - 当前多出   → 新增（added）：当前审查新出现的破坏项
  - 基线多出   → 已消失（disappeared）：基线存在、当前已不复现

仅对账破坏性发现（severity == "breaking"）；每条结果保留两侧原发现 ID，
便于前端跳回对应审查定位。
"""
from __future__ import annotations

from typing import Any

BREAKING = "breaking"


def _norm_findings(findings: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """复制发现列表并补齐 id（历史数据无引擎序号时按 1 起始位置补齐）。

    与 groups.build_impact_groups 的回退规则保持一致，保证对账结果中的
    发现 ID 与前端 / 聚合视图看到的是同一套编号。
    """
    items: list[dict[str, Any]] = []
    for i, f in enumerate(findings or []):
        g = dict(f)
        if not isinstance(g.get("id"), int):
            g["id"] = i + 1
        items.append(g)
    return items


def finding_identity(f: dict[str, Any]) -> tuple[str, str, str, str]:
    """发现的稳定身份：端点 / 方向 / 规则 / 语义定位。

    刻意不包含发现 ID、严重度与旧/新值：ID 与顺序不应影响配对；
    严重度变化（如破坏 → 兼容）会使该发现退出破坏项对账范围，体现为"已消失"。
    """
    return (
        str(f.get("endpoint") or ""),
        str(f.get("direction") or ""),
        str(f.get("rule_id") or ""),
        str(f.get("location") or ""),
    )


def _by_identity(findings: list[dict[str, Any]]) -> dict[tuple, list[dict[str, Any]]]:
    """按稳定身份分组；组内按发现 id 排序，保证配对结果确定。"""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for f in findings:
        groups.setdefault(finding_identity(f), []).append(f)
    for g in groups.values():
        g.sort(key=lambda x: x["id"])
    return groups


def _entry(
    finding: dict[str, Any],
    current_finding_id: int | None,
    baseline_finding_id: int | None,
) -> dict[str, Any]:
    """对账条目：相关发现 + 两侧原发现 ID（缺失侧为 None）。"""
    return {
        "finding": finding,
        "current_finding_id": current_finding_id,
        "baseline_finding_id": baseline_finding_id,
    }


def build_reconciliation(
    baseline_findings: list[dict[str, Any]] | None,
    current_findings: list[dict[str, Any]] | None,
) -> dict[str, Any]:
    """对账基线与当前审查的破坏性发现，输出新增 / 延续 / 已消失三类及数量。"""
    baseline = [
        f for f in _norm_findings(baseline_findings) if f.get("severity") == BREAKING
    ]
    current = [
        f for f in _norm_findings(current_findings) if f.get("severity") == BREAKING
    ]

    base_groups = _by_identity(baseline)
    cur_groups = _by_identity(current)

    added: list[dict[str, Any]] = []
    continuing: list[dict[str, Any]] = []
    disappeared: list[dict[str, Any]] = []

    # 同身份按出现次数一一配对：min(基线数, 当前数) 对延续，余者分别为新增/消失
    for ident in sorted(set(base_groups) | set(cur_groups)):
        bs = base_groups.get(ident, [])
        cs = cur_groups.get(ident, [])
        paired = min(len(bs), len(cs))
        for i in range(paired):
            continuing.append(_entry(cs[i], cs[i]["id"], bs[i]["id"]))
        for c in cs[paired:]:
            added.append(_entry(c, c["id"], None))
        for b in bs[paired:]:
            disappeared.append(_entry(b, None, b["id"]))

    # 输出顺序确定：延续 / 新增按当前侧发现 id，消失按基线侧发现 id
    added.sort(key=lambda e: e["current_finding_id"])
    continuing.sort(key=lambda e: e["current_finding_id"])
    disappeared.sort(key=lambda e: e["baseline_finding_id"])

    return {
        "summary": {
            "added": len(added),
            "continuing": len(continuing),
            "disappeared": len(disappeared),
            "current_breaking": len(current),
            "baseline_breaking": len(baseline),
        },
        "added": added,
        "continuing": continuing,
        "disappeared": disappeared,
    }

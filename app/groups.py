"""差异发现的根因聚合：把逐条发现组织成有向影响组。

分组规则：
- **端点 × 方向是硬边界**：同一组件（含循环引用场景）在请求侧与响应侧
  各自成组，绝不跨方向合并；
- **同源归并**：共享 / 循环组件被多处引用时，引擎会按引用点对同一组件
  指针重复报告同一规则——rule_id、JSON Pointer 与 $ref 锚点完全一致的
  发现视为同一根因的不同表现，折叠进同一节点；
- **祖先吸收**：父路径被整体替换（类型整体变更、schema 变为布尔等）或
  整体删除时，其 JSON Pointer 后代发现只是这次变更的下游影响，被父发现
  吸收，形成"主因 → 受影响条目"的有向结构（父 → 子）。

每组返回稳定组标识（端点/方向/规则/锚点指针的 SHA-256 短哈希）、主因、
受影响条目（含吸收关系与深度）以及可展开定位（语义路径 + 双侧指针）。
"""
from __future__ import annotations

import hashlib
from typing import Any

# 父路径"整体替换"类规则：命中时其指针后代全部视为下游影响
WHOLESALE_REPLACE_RULES = {
    "type-changed",                  # 类型整体变更（如 object → string）
    "schema-became-false",           # 子树整体失效（不允许任何值）
    "schema-false-relaxed",          # false → 普通 / true
    "schema-constraints-relaxed",    # 普通 → true（整体放宽为任意值）
    "schema-constraints-tightened",  # true → 普通（整体收紧为具体约束）
}

# 父路径"整体删除"类规则（指针即被删子树的根）。
# parameter-removed 刻意不在此列：其指针指向 parameters 容器数组而非单个
# 参数，若参与吸收会误并同操作下其他参数的独立发现。
WHOLESALE_REMOVE_RULES = {
    "path-removed",
    "operation-removed",
    "request-body-removed",
    "response-removed",
    "media-type-removed",
    "response-media-type-removed",
    "property-removed",
    "property-removed-request",
    "property-removed-writeonly",
    "server-removed",
}

WHOLESALE_RULES = WHOLESALE_REPLACE_RULES | WHOLESALE_REMOVE_RULES

_SEV_RANK = {"breaking": 0, "deprecation": 1, "non_breaking": 2, "info": 3}


# ---------------------------------------------------------------------- #
# 指针工具
# ---------------------------------------------------------------------- #
def _tokens(pointer: Any) -> list[str] | None:
    """JSON Pointer → 段列表；空 / 非法返回 None。"""
    if not isinstance(pointer, str) or not pointer:
        return None
    frag = pointer.split("#", 1)[1] if "#" in pointer else pointer
    if not frag.startswith("/") or frag == "/":
        return None
    return frag.lstrip("/").split("/")


def _ancestor_depth(parent: dict[str, Any], child: dict[str, Any]) -> int | None:
    """parent 的指针是否为 child 的严格祖先（旧 / 新任一侧命中即算）。

    返回命中侧的祖先段数（用于"最近祖先"裁决），否则 None。
    """
    best: int | None = None
    for side in ("old_json_path", "new_json_path"):
        a = _tokens(parent.get(side))
        b = _tokens(child.get(side))
        if a and b and len(a) < len(b) and b[: len(a)] == a:
            best = len(a) if best is None else max(best, len(a))
    return best


# ---------------------------------------------------------------------- #
# 归并键与组标识
# ---------------------------------------------------------------------- #
def _is_shared_component(f: dict[str, Any]) -> bool:
    """发现是否锚定在共享组件内（带 $ref 锚点）。"""
    return bool(f.get("old_ref") or f.get("new_ref"))


def _cluster_key(f: dict[str, Any]) -> tuple:
    """同源归并键：同一组件指针 + 同一规则 + 同一锚点 = 同一根因。"""
    return (
        f.get("rule_id"),
        f.get("old_json_path"),
        f.get("new_json_path"),
        f.get("old_ref"),
        f.get("new_ref"),
    )


def _worst_severity(findings: list[dict[str, Any]]) -> str:
    return min(
        (f.get("severity") for f in findings),
        key=lambda s: _SEV_RANK.get(s, 9),
        default="info",
    )


def _group_id(
    endpoint: str | None, direction: str | None, primary: dict[str, Any]
) -> str:
    """稳定组标识：端点 / 方向 / 规则 / 锚点指针的 SHA-256 短哈希。"""
    anchor = (
        primary.get("new_json_path")
        or primary.get("old_json_path")
        or primary.get("location")
        or ""
    )
    basis = "|".join(
        [endpoint or "", direction or "", str(primary.get("rule_id") or ""), anchor]
    )
    return "g-" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:12]


def _location_of(f: dict[str, Any]) -> dict[str, Any]:
    """可展开定位：语义路径 + 双侧 JSON Pointer + $ref 锚点。"""
    return {
        "finding_id": f.get("id"),
        "location": f.get("location"),
        "old_json_path": f.get("old_json_path"),
        "new_json_path": f.get("new_json_path"),
        "old_ref": f.get("old_ref"),
        "new_ref": f.get("new_ref"),
    }


def _entry(
    f: dict[str, Any], relation: str, depth: int, absorbed_by: int | None
) -> dict[str, Any]:
    e = dict(f)
    e["relation"] = relation      # primary | absorbed | same_cause
    e["depth"] = depth            # 主因为 0，被吸收的后代按树深递增
    e["absorbed_by"] = absorbed_by
    return e


class _Node:
    """分组节点：一个代表性发现 + 其同源副本（共享/循环组件的重复报告）。"""

    __slots__ = ("rep", "same_cause")

    def __init__(self, rep: dict[str, Any]):
        self.rep = rep
        self.same_cause: list[dict[str, Any]] = []


# ---------------------------------------------------------------------- #
# 分区内分组
# ---------------------------------------------------------------------- #
def _group_partition(
    members: list[dict[str, Any]], endpoint: str | None, direction: str | None
) -> list[dict[str, Any]]:
    """在单个（端点 × 方向）分区内构建有向影响组。"""
    # ---- 同源归并：共享 / 循环组件的重复报告折叠为一个节点 ----
    by_key: dict[tuple, list[dict[str, Any]]] = {}
    for f in members:
        if _is_shared_component(f):
            by_key.setdefault(_cluster_key(f), []).append(f)

    clustered: set[int] = set()
    nodes: list[_Node] = []
    for fs in by_key.values():
        if len(fs) < 2:
            continue
        fs.sort(key=lambda x: x["id"])
        node = _Node(fs[0])
        node.same_cause = fs[1:]
        nodes.append(node)
        clustered.update(x["id"] for x in fs)
    for f in members:
        if f["id"] not in clustered:
            nodes.append(_Node(f))

    # ---- 祖先吸收：整体替换 / 删除的父路径吸收指针后代（有向边 父→子）----
    parent_of: dict[int, int] = {}
    for j, child in enumerate(nodes):
        best: tuple[int, int] | None = None  # (祖先段数, -rep.id)：最近祖先优先
        for i, parent in enumerate(nodes):
            if i == j or parent.rep.get("rule_id") not in WHOLESALE_RULES:
                continue
            depth = _ancestor_depth(parent.rep, child.rep)
            if depth is None:
                continue
            cand = (depth, -parent.rep["id"])
            if best is None or cand > best:
                best = cand
                parent_of[j] = i

    children: dict[int, list[int]] = {}
    roots: list[int] = []
    for j in range(len(nodes)):
        p = parent_of.get(j)
        if p is None:
            roots.append(j)
        else:
            children.setdefault(p, []).append(j)

    # ---- 组装分组 ----
    groups: list[dict[str, Any]] = []
    for r in roots:
        node = nodes[r]
        primary = node.rep
        affected: list[dict[str, Any]] = [
            _entry(f, "same_cause", 0, primary["id"]) for f in node.same_cause
        ]
        # 广度优先展开被吸收的后代（父子边有向：父 → 子；严格前缀保证无环）
        queue: list[tuple[int, int]] = [(r, 0)]
        while queue:
            idx, depth = queue.pop(0)
            for c in sorted(children.get(idx, []), key=lambda c: nodes[c].rep["id"]):
                cn = nodes[c]
                affected.append(
                    _entry(cn.rep, "absorbed", depth + 1, nodes[idx].rep["id"])
                )
                affected.extend(
                    _entry(f, "same_cause", depth + 1, cn.rep["id"])
                    for f in cn.same_cause
                )
                queue.append((c, depth + 1))

        all_members = [primary, *affected]
        groups.append({
            "group_id": _group_id(endpoint, direction, primary),
            "endpoint": endpoint,
            "direction": direction,
            "category": primary.get("category"),
            "rule_id": primary.get("rule_id"),
            "change": primary.get("change"),
            "severity": _worst_severity(all_members),
            "primary_severity": primary.get("severity"),
            "member_count": len(all_members),
            "breaking_count": sum(
                1 for m in all_members if m.get("severity") == "breaking"
            ),
            "primary": _entry(primary, "primary", 0, None),
            "affected": affected,
            "locations": [_location_of(primary)]
            + [_location_of(e) for e in affected],
        })
    return groups


# ---------------------------------------------------------------------- #
# 入口
# ---------------------------------------------------------------------- #
def build_impact_groups(findings: list[dict[str, Any]]) -> dict[str, Any]:
    """把逐条发现聚合为有向影响组，供审查详情的"根因聚合"视图使用。"""
    items: list[dict[str, Any]] = []
    for i, f in enumerate(findings or []):
        g = dict(f)
        if not isinstance(g.get("id"), int):
            g["id"] = i + 1  # 历史数据无引擎序号时按 1 起始位置补齐
        items.append(g)

    # 端点 × 方向：硬边界分区（请求 / 响应 / 结构性互不合并）
    partitions: dict[tuple, list[dict[str, Any]]] = {}
    for f in items:
        key = (f.get("endpoint"), f.get("direction"))
        partitions.setdefault(key, []).append(f)

    groups: list[dict[str, Any]] = []
    for (endpoint, direction), members in partitions.items():
        groups.extend(_group_partition(members, endpoint, direction))

    groups.sort(
        key=lambda g: (
            _SEV_RANK.get(g["severity"], 9),
            g["endpoint"] or "",
            -g["member_count"],
            g["primary"]["id"],
        )
    )

    # 稳定组标识冲突兜底（同键即同组，理论上不会冲突）
    used: set[str] = set()
    for g in groups:
        base = g["group_id"]
        gid, n = base, 2
        while gid in used:
            gid = f"{base}-{n}"
            n += 1
        used.add(gid)
        g["group_id"] = gid

    return {
        "total_findings": len(items),
        "group_count": len(groups),
        "affected_count": len(items) - len(groups),
        "groups": groups,
    }

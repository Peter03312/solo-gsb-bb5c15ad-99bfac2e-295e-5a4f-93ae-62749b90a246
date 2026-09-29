"""组件可达性审计：从 paths / webhooks 实际入口出发，沿本地 $ref 传递追踪 components。

判定规则：
- **入口**：`paths` 下每个 path item 与 `webhooks`（OpenAPI 3.1）下每个 webhook；
- **传递可达**：沿文档内 `$ref` 逐层追踪，visited 集合去重——A→B→A 形式的
  循环引用只访问一次，不会无限递归；外部文件引用不属于本地可达性范围，跳过；
- **security 也算可达**：全局（根级）与操作级 `security` 按名称引用的
  `components/securitySchemes` 视为可达（含被引用 pathItem / 内联 callback
  中的操作级 security）；
- **未触达**：`components` 中声明但从未被入口触达的组件，按类型 / 名称列出；
- **无效本地引用**：`$ref` 目标不存在、或 security 引用了未声明的
  securityScheme 名称时，逐条给出位置与原因；
- **新版独有**：新版未触达且旧版文档中不存在同名同类型组件的，单独标记。

与 groups / reconcile 一致：纯函数、无额外存储，读取审查时实时计算。
"""
from __future__ import annotations

import re
from typing import Any

from .refs import PointerError, decode_token, encode_token, normalize_pointer, resolve_pointer

HTTP_METHODS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")

# components 类型的展示顺序（已知类型优先，其余非扩展键按文档顺序补充）
SECTION_ORDER = (
    "schemas", "parameters", "examples", "requestBodies", "responses",
    "headers", "securitySchemes", "links", "callbacks", "pathItems",
)

# /components/{section}/{name} 形式的顶层组件指针
_COMPONENT_RE = re.compile(r"^/components/([^/]+)/([^/]+)$")


def _local_pointer(ref: Any) -> str | None:
    """把文档内 $ref 归一化为 JSON Pointer；非本地引用返回 None。

    本地引用仅指 `#/...` 片段形式（及罕见的裸 `/...` 指针）；带文件 / URI
    前缀的外部引用（如 `./other.yaml#/...`）不属于本地可达性范围，跳过——
    与"仅解析文档内 $ref"的整体约定一致。
    """
    if not isinstance(ref, str) or not ref.startswith(("#", "/")):
        return None
    try:
        return normalize_pointer(ref)
    except PointerError:
        return None


# ---------------------------------------------------------------------- #
# 子树扫描
# ---------------------------------------------------------------------- #
def _iter_refs(node: Any, pointer: str, out: list[tuple[str, str]]) -> None:
    """递归收集子树内全部 $ref：(所在节点指针, $ref 字符串)。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append((pointer, ref))
        for k, v in node.items():
            if k == "$ref":
                continue
            _iter_refs(v, f"{pointer}/{encode_token(k)}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _iter_refs(v, f"{pointer}/{i}", out)


def _iter_operation_security(
    node: Any, pointer: str, out: list[tuple[str, list[Any]]]
) -> None:
    """递归收集子树中全部操作级 security：(security 数组指针, requirement 列表)。

    含 HTTP 方法键的 dict 视为 Path Item——内联 callback 中嵌套的 path item、
    components/pathItems 中被引用的 path item 同样覆盖。
    """
    if isinstance(node, dict):
        for method in HTTP_METHODS:
            op = node.get(method)
            if isinstance(op, dict) and isinstance(op.get("security"), list):
                out.append((f"{pointer}/{method}/security", op["security"]))
        for k, v in node.items():
            _iter_operation_security(v, f"{pointer}/{encode_token(k)}", out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _iter_operation_security(v, f"{pointer}/{i}", out)


def _security_names(req_list: list[Any]) -> list[str]:
    """Security Requirement 对象列表 → 按名称引用的 securityScheme 名。"""
    names: list[str] = []
    for req in req_list:
        if isinstance(req, dict):
            names.extend(str(k) for k in req)
    return names


def _ordered_sections(comps: dict[str, Any]) -> list[str]:
    """components 下参与统计的类型键：已知类型优先，跳过 x- 扩展。"""
    known = [s for s in SECTION_ORDER if s in comps]
    rest = [
        k for k in comps
        if k not in SECTION_ORDER and isinstance(k, str) and not k.startswith("x-")
    ]
    return known + rest


def _component_keys(doc: dict[str, Any]) -> set[tuple[str, str]]:
    """文档 components 中声明的全部 (类型, 名称)。"""
    keys: set[tuple[str, str]] = set()
    comps = doc.get("components")
    if isinstance(comps, dict):
        for section in _ordered_sections(comps):
            holder = comps.get(section)
            if isinstance(holder, dict):
                for name in holder:
                    keys.add((section, str(name)))
    return keys


# ---------------------------------------------------------------------- #
# 单侧审计
# ---------------------------------------------------------------------- #
def audit_document(doc: dict[str, Any]) -> dict[str, Any]:
    """审计单侧文档：可达组件数、未触达组件列表（类型/名称）与无效本地引用。"""
    visited: set[str] = set()
    broken: list[dict[str, Any]] = []
    broken_seen: set[tuple[str, str]] = set()
    # securityScheme 名称 -> 引用位置列表（全局 / 操作级 security）
    security_refs: dict[str, list[str]] = {}

    def record_broken(at: str, ref: str, reason: str) -> None:
        key = (at, ref)
        if key in broken_seen:
            return
        broken_seen.add(key)
        broken.append({"ref": ref, "at": at, "reason": reason})

    queue: list[str] = []

    def record_security(at: str, req_list: list[Any]) -> None:
        for name in _security_names(req_list):
            if name not in security_refs:
                security_refs[name] = []
                # 按名称引用 → 对应 securitySchemes 组件入队追踪
                queue.append(f"/components/securitySchemes/{encode_token(name)}")
            if at not in security_refs[name]:
                security_refs[name].append(at)

    # 全局 security（根级）
    if isinstance(doc.get("security"), list):
        record_security("/security", doc["security"])

    # BFS：从 paths / webhooks 入口出发，沿本地 $ref 传递；visited 保证
    # 循环引用只访问一次
    for key in ("paths", "webhooks"):
        if isinstance(doc.get(key), dict):
            queue.append(f"/{key}")
    while queue:
        pointer = queue.pop()
        if pointer in visited:
            continue
        visited.add(pointer)
        try:
            node = resolve_pointer(doc, pointer)
        except PointerError:
            continue  # 入口指针本身不可解析（理论上不会发生）

        # 操作级 security（含内联 callback / 被引用 pathItem 中的操作）
        sec_hits: list[tuple[str, list[Any]]] = []
        _iter_operation_security(node, pointer, sec_hits)
        for at, req_list in sec_hits:
            record_security(at, req_list)

        refs: list[tuple[str, str]] = []
        _iter_refs(node, pointer, refs)
        for at, ref in refs:
            target = _local_pointer(ref)
            if target is None:
                continue  # 外部文件引用：不属于本地可达性范围
            if not target:
                continue  # 指向文档根，无组件意义
            try:
                resolve_pointer(doc, target)
            except PointerError as exc:
                record_broken(at, ref, f"本地引用目标不存在：{exc}")
                continue
            if target not in visited:
                queue.append(target)

    # security 按名称引用的 securitySchemes：未声明的给出明确提示
    comps = doc.get("components")
    schemes = comps.get("securitySchemes") if isinstance(comps, dict) else None
    for name, ats in security_refs.items():
        if not (isinstance(schemes, dict) and name in schemes):
            for at in ats:
                record_broken(
                    at, name,
                    f"security 引用了未在 components.securitySchemes 中声明的名称 '{name}'",
                )

    # 可达的顶层组件（/components/{section}/{name}）
    reachable: set[tuple[str, str]] = set()
    for p in visited:
        m = _COMPONENT_RE.match(p)
        if m:
            reachable.add((decode_token(m.group(1)), decode_token(m.group(2))))

    unreachable: list[dict[str, Any]] = []
    if isinstance(comps, dict):
        for section in _ordered_sections(comps):
            holder = comps.get(section)
            if not isinstance(holder, dict):
                continue
            for name in holder:
                if (section, str(name)) not in reachable:
                    unreachable.append({
                        "section": section,
                        "name": str(name),
                        "pointer": (
                            f"/components/{encode_token(section)}/{encode_token(name)}"
                        ),
                    })

    by_section: dict[str, int] = {}
    for item in unreachable:
        by_section[item["section"]] = by_section.get(item["section"], 0) + 1

    return {
        "reachable_count": len(reachable),
        "unreachable": unreachable,
        "unreachable_count": len(unreachable),
        "unreachable_by_section": by_section,
        "broken_refs": broken,
    }


# ---------------------------------------------------------------------- #
# 双侧对比
# ---------------------------------------------------------------------- #
def build_reachability(
    old_doc: dict[str, Any], new_doc: dict[str, Any]
) -> dict[str, Any]:
    """对旧版、新版分别审计，并标出"新版独有"的未使用组件。"""
    old = audit_document(old_doc)
    new = audit_document(new_doc)

    old_keys = _component_keys(old_doc)
    for item in old["unreachable"]:
        item["new_only"] = False
    new_only = 0
    for item in new["unreachable"]:
        # 新版独有：新版未触达，且旧版文档中不存在同名同类型组件
        flag = (item["section"], item["name"]) not in old_keys
        item["new_only"] = flag
        if flag:
            new_only += 1

    return {
        "old": old,
        "new": new,
        "summary": {
            "old_unreachable": old["unreachable_count"],
            "new_unreachable": new["unreachable_count"],
            "new_only": new_only,
            "old_broken_refs": len(old["broken_refs"]),
            "new_broken_refs": len(new["broken_refs"]),
        },
    }

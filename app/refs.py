"""$ref 解析与"引用展开树"构建。

- resolve_ref：解析单个 JSON Pointer（含 URL/URI 片段的本地部分）。
- build_ref_tree：从任意指针开始展开文档结构，沿 $ref 递归，遇到已经在
  当前展开栈中的目标时标记为 circular（循环引用）而不会无限递归。
"""
from __future__ import annotations

from typing import Any

# 视为"内联 schema 容器"的键，其值可以是 schema 或 schema 数组
SCHEMA_KEYS_SINGLE = {
    "schema",
    "additionalProperties",
    "not",
    "contains",
}
SCHEMA_KEYS_MULTI = {
    "allOf",
    "anyOf",
    "oneOf",
    "prefixItems",
}
# 值为 name -> schema 的映射
SCHEMA_KEYS_MAP = {
    "properties",
    "definitions",
    "$defs",
    "patternProperties",
    "dependencies",
    "dependentSchemas",
}
# 端点结构下的容器
OPERATION_KEYS = ("get", "put", "post", "delete", "patch", "options", "head", "trace")
PARAMETER_LOCATIONS = ("query", "header", "path", "cookie")

MAX_TREE_NODES = 6000  # 超大文档的展开预算，防止恶意/异常文档耗尽资源


class PointerError(KeyError):
    """JSON Pointer 无法解析。"""


def decode_token(token: str) -> str:
    return token.replace("~1", "/").replace("~0", "~")


def encode_token(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def parse_ref(ref: str) -> str:
    """把 $ref 归一化为文档内 JSON Pointer（去掉 URI 前缀与 #）。"""
    if not isinstance(ref, str) or not ref:
        raise PointerError("非法的 $ref（必须是非空字符串）")
    fragment = ref
    if "#" in fragment:
        fragment = fragment.split("#", 1)[1]
    if fragment in ("", "/"):
        return ""
    if not fragment.startswith("/"):
        # 老风格 #/definitions/... 才支持；裸名/外部文件不支持
        raise PointerError(f"不支持的外部引用：{ref}")
    return fragment  # /a/b/0 形式


def normalize_pointer(pointer: str) -> str:
    """把 $ref / JSON Pointer 统一为 /a/b/0 形式（仅文档内引用）。"""
    if not isinstance(pointer, str) or not pointer:
        return ""
    fragment = pointer.split("#", 1)[1] if "#" in pointer else pointer
    if fragment in ("", "/"):
        return ""
    if not fragment.startswith("/"):
        raise PointerError(f"不支持的外部引用：{pointer}")
    return fragment


def resolve_pointer(doc: Any, pointer: str) -> Any:
    pointer = normalize_pointer(pointer)
    if pointer == "":
        return doc
    current = doc
    for raw_token in pointer.lstrip("/").split("/"):
        token = decode_token(raw_token)
        if isinstance(current, list):
            try:
                idx = int(token)
            except ValueError as exc:
                raise PointerError(f"指针段 '{token}' 不能用作数组下标") from exc
            if not 0 <= idx < len(current):
                raise PointerError(f"数组下标越界：{idx}（长度 {len(current)}）")
            current = current[idx]
        elif isinstance(current, dict):
            if token not in current:
                raise PointerError(f"对象中不存在键 '{token}'")
            current = current[token]
        else:
            raise PointerError(f"无法继续解析指针段 '{token}'：当前节点不是对象/数组")
    return current


def resolve_ref(doc: Any, ref: str) -> tuple[Any, str]:
    """解析 $ref，返回 (目标节点, 规范化指针)。"""
    pointer = normalize_pointer(ref)
    return resolve_pointer(doc, pointer), pointer


def _is_ref(node: Any) -> bool:
    return isinstance(node, dict) and isinstance(node.get("$ref"), str)


def _short_name(pointer: str) -> str:
    if not pointer:
        return "(root)"
    return decode_token(pointer.rstrip("/").split("/")[-1])


def _meta_line(node: Any) -> str:
    """生成节点摘要行上的类型/字段信息。"""
    if not isinstance(node, dict):
        if isinstance(node, list):
            return f"array[{len(node)}]"
        return type(node).__name__
    bits: list[str] = []
    if "type" in node:
        t = node["type"]
        bits.append(",".join(t) if isinstance(t, list) else str(t))
    elif any(k in node for k in ("allOf", "anyOf", "oneOf")):
        bits.append(next(k for k in ("allOf", "anyOf", "oneOf") if k in node))
    if node.get("format"):
        bits.append(str(node["format"]))
    if node.get("enum"):
        bits.append(f"enum×{len(node['enum'])}")
    if isinstance(node.get("properties"), dict):
        bits.append(f"{len(node['properties'])} props")
    if "items" in node:
        bits.append("array")
    if node.get("nullable"):
        bits.append("nullable")
    return " ".join(bits)


def _node_kind(node: Any) -> str:
    if not isinstance(node, dict):
        return "value"
    if "$ref" in node:
        return "ref"
    if "type" in node or any(
        k in node for k in ("allOf", "anyOf", "oneOf", "properties", "items", "schema")
    ):
        return "schema"
    if "content" in node:
        return "content"
    if node.get("responses") is not None:
        return "operation"
    return "object"


def _iter_children(
    doc: dict[str, Any],
    node: Any,
    pointer: str,
    *,
    under_parameters: bool = False,
):
    """产出 (label, child_pointer, child, hint)。"""
    if isinstance(node, list):
        for i, item in enumerate(node):
            label = f"[{i}]"
            cp = f"{pointer}/{i}"
            if isinstance(item, dict):
                if item.get("name"):
                    label += f" {item['name']}"
                if item.get("in"):
                    label += f"  ({item['in']})"
            yield label, cp, item, "list-item"
        return

    if not isinstance(node, dict):
        return

    # 1) $ref 最先显示
    if isinstance(node.get("$ref"), str):
        try:
            target, tp = resolve_ref(doc, node["$ref"])
            yield "$ref → " + node["$ref"], tp, target, "ref-link"
        except PointerError:
            yield "$ref → " + node["$ref"] + "  (无法解析)", "", None, "ref-broken"
        # 同级的兄弟字段（OpenAPI 3.1 允许 ref 与兄弟字段并存）
        for k, v in node.items():
            if k == "$ref":
                continue
            yield k, f"{pointer}/{encode_token(k)}", v, "sibling"
        return

    # 2) parameters 下的参数对象按 name/in 固定顺序展示字段
    if under_parameters and isinstance(node, dict) and "name" in node and "in" in node:
        order = ["name", "in", "description", "required", "deprecated",
                 "allowEmptyValue", "style", "explode", "schema", "example", "examples"]
        for k in order:
            if k in node:
                v = node[k]
                yield k, f"{pointer}/{encode_token(k)}", v, "param-field"
        for k, v in node.items():
            if k not in order:
                yield k, f"{pointer}/{encode_token(k)}", v, "param-field"
        return

    # 3) 常规键遍历，schema 键优先
    priority = [
        "summary", "description", "operationId", "deprecated", "tags",
        "parameters", "requestBody", "responses",
        "allOf", "anyOf", "oneOf", "schema", "properties", "items",
        "additionalProperties", "required", "type", "format", "enum",
        "nullable", "content", "components", "paths", "info",
    ]
    seen: set[str] = set()
    ordered_keys = [k for k in priority if k in node] + [
        k for k in node if k not in priority
    ]
    for k in ordered_keys:
        if k in seen:
            continue
        seen.add(k)
        v = node[k]
        cp = f"{pointer}/{encode_token(k)}"
        if k in SCHEMA_KEYS_SINGLE and isinstance(v, dict):
            yield k, cp, v, "schema-slot"
        elif k in SCHEMA_KEYS_MULTI and isinstance(v, list):
            yield k, cp, v, "schema-list"
        elif k in SCHEMA_KEYS_MAP and isinstance(v, dict):
            for name, sub in v.items():
                yield f"{name}", f"{cp}/{encode_token(name)}", sub, "property"
        elif k == "items" and isinstance(v, (dict, list)):
            yield k, cp, v, "schema-slot"
        elif k == "parameters" and isinstance(v, list):
            yield k, cp, v, "parameters"
        elif k in SCHEMA_KEYS_MAP:
            yield k, cp, v, "object"
        elif k in SCHEMA_KEYS_SINGLE or k in SCHEMA_KEYS_MULTI:
            # 布尔 schema（additionalProperties: true/false）或空容器
            yield k, cp, v, "value"
        elif k == "content" and isinstance(v, dict):
            for mt, media in v.items():
                yield mt, f"{cp}/{encode_token(mt)}", media, "media-type"
        elif k == "responses" and isinstance(v, dict):
            for code, resp in v.items():
                yield str(code), f"{cp}/{encode_token(str(code))}", resp, "response"
        elif k in ("paths", "components") and isinstance(v, dict):
            for name, sub in v.items():
                yield name, f"{cp}/{encode_token(name)}", sub, "root-group"
        elif k in OPERATION_KEYS and isinstance(v, dict):
            yield k.upper(), cp, v, "operation"
        elif isinstance(v, dict) and pointer.endswith("/paths"):
            yield k, cp, v, "path-item"
        elif isinstance(v, dict):
            # 泛型对象：只展开看起来有结构的，否则标为叶子由前端内联显示
            if any(isinstance(sv, (dict, list)) for sv in v.values()) or len(v) <= 8:
                yield k, cp, v, "object"
            else:
                yield k, cp, v, "leaf-object"
        elif isinstance(v, list):
            yield k, cp, v, "list"
        else:
            yield k, cp, v, "value"


def build_ref_tree(
    doc: dict[str, Any],
    root_pointer: str = "",
    *,
    max_nodes: int = MAX_TREE_NODES,
) -> dict[str, Any]:
    """构建展开树。

    返回：
      {
        "pointer": "...", "name": "...", "kind": "...", "summary": "...",
        "ref": "$ref 值" | None,
        "circular": bool, "truncated": bool,
        "children": [ ... 递归 ... ]
      }
    """
    try:
        root = resolve_pointer(doc, root_pointer)
    except PointerError as exc:
        return {
            "pointer": root_pointer,
            "name": root_pointer or "(root)",
            "kind": "error",
            "summary": f"指针无法解析：{exc}",
            "ref": None,
            "circular": False,
            "truncated": False,
            "children": [],
        }

    budget = {"n": 0}

    def walk(node: Any, pointer: str, stack: tuple[str, ...], depth: int) -> dict[str, Any]:
        budget["n"] += 1
        truncated = budget["n"] >= max_nodes

        # 解析 $ref 目标，循环检测以"目标指针"为准
        ref_value = node.get("$ref") if isinstance(node, dict) else None
        target_pointer: str | None = None
        target_node: Any = node
        if ref_value:
            try:
                target_node, target_pointer = resolve_ref(doc, ref_value)
            except PointerError as exc:
                return {
                    "pointer": pointer,
                    "name": _short_name(pointer),
                    "kind": "ref-broken",
                    "summary": f"无法解析的 $ref：{ref_value}（{exc}）",
                    "ref": ref_value,
                    "circular": False,
                    "truncated": False,
                    "children": [],
                }
            if target_pointer in stack:
                return {
                    "pointer": target_pointer,
                    "name": _short_name(target_pointer),
                    "kind": "ref",
                    "summary": f"⟳ 循环引用 {ref_value}",
                    "ref": ref_value,
                    "circular": True,
                    "truncated": False,
                    "children": [],
                }

        effective = target_node if target_pointer is not None else node
        eff_pointer = target_pointer if target_pointer is not None else pointer
        next_stack = (
            stack + (target_pointer,) if target_pointer is not None else stack
        )

        # 原始叶子
        if isinstance(effective, (str, int, float, bool)) or effective is None:
            return {
                "pointer": eff_pointer,
                "name": _short_name(eff_pointer),
                "kind": "value",
                "summary": _render_scalar(effective),
                "ref": ref_value,
                "circular": False,
                "truncated": False,
                "children": [],
            }

        children: list[dict[str, Any]] = []
        if not truncated and depth < 120 and isinstance(effective, (dict, list)):
            for label, cp, child, hint in _iter_children(
                doc, effective, eff_pointer,
                under_parameters=eff_pointer.endswith("parameters")
                or "/parameters/" in eff_pointer,
            ):
                if child is None and hint == "ref-broken":
                    children.append({
                        "pointer": "",
                        "name": label,
                        "kind": "ref-broken",
                        "summary": "无法解析",
                        "ref": None,
                        "circular": False,
                        "truncated": False,
                        "children": [],
                    })
                    continue
                child_node = walk(child, cp, next_stack, depth + 1)
                child_node["name"] = label
                child_node["hint"] = hint
                children.append(child_node)

        name = _short_name(eff_pointer)
        return {
            "pointer": eff_pointer,
            "name": name,
            "kind": "ref" if ref_value else _node_kind(effective),
            "summary": _meta_line(effective) if isinstance(effective, dict) else (
                f"array[{len(effective)}]" if isinstance(effective, list) else ""
            ),
            "ref": ref_value,
            "circular": False,
            "truncated": truncated,
            "children": children,
        }

    tree = walk(root, root_pointer, (), 0)
    tree["name"] = root_pointer or "(document root)"
    return tree


def expand_path_to_pointer(
    tree: dict[str, Any], target_pointer: str
) -> list[str]:
    """在已构建的展开树中找出从根到 target_pointer 的节点名路径。

    返回路径上每个节点的 name（供前端逐层展开）；找不到则返回 []。
    """
    path: list[str] = []

    def visit(node: dict[str, Any]) -> bool:
        if node.get("pointer") == target_pointer:
            return True
        for child in node.get("children", []):
            if visit(child):
                path.append(child.get("name", ""))
                return True
        return False

    if visit(tree):
        path.reverse()
        return [tree.get("name", "")] + path
    return []


def _render_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    text = str(value)
    return text if len(text) <= 80 else text[:77] + "..."


def list_root_pointers(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """供前端"选择根节点"使用：列出 paths / components 各类型分组。"""
    groups: list[dict[str, Any]] = []

    if isinstance(doc.get("paths"), dict):
        groups.append({
            "group": "paths",
            "items": [
                {"label": p, "pointer": f"/paths/{encode_token(p)}"}
                for p in doc["paths"]
            ],
        })

    comps = doc.get("components")
    if isinstance(comps, dict):
        # 已知类型优先，其余（含 3.1 新增类型）按文档顺序补充；跳过 x- 扩展
        known = [
            "schemas", "parameters", "requestBodies", "responses", "headers",
            "securitySchemes", "examples", "callbacks", "links", "pathItems",
        ]
        ordered = [s for s in known if s in comps] + [
            k for k in comps
            if k not in known and isinstance(k, str) and not k.startswith("x-")
        ]
        for section in ordered:
            holder = comps.get(section)
            if isinstance(holder, dict) and holder:
                groups.append({
                    "group": f"components/{section}",
                    "items": [
                        {"label": n, "pointer": f"/components/{section}/{encode_token(n)}"}
                        for n in holder
                    ],
                })

    groups.append({"group": "document", "items": [{"label": "(根文档)", "pointer": ""}]})
    return groups

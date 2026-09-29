"""响应实例预览 OpenAPI Link 的后续调用参数。

给定一次已发生调用的响应实例（源方法 + 路径模板 + 状态码 + JSON 响应体 +
响应头），在选定侧（旧版 / 新版）文档的源操作响应中按

    精确状态码 → 范围码（1XX/2XX/…）→ default

的顺序定位响应对象，逐条预览其 links：

- Link 目标：唯一 operationId 或本地 operationRef（#/paths/…/{method}）；
  operationId 不唯一、operationRef 指向外部 / 非 paths 操作均逐条报错。
- 响应、Link、参数的本地 $ref 链循环安全（检测到环即报错，不无限递归）；
  外部引用（文件 / URL / 裸名）一律报错。兼容 3.1 中 $ref 的兄弟字段
  （兄弟字段覆盖引用目标的同名字段）。
- 取值表达式：JSON 常量（含 null——null 视为已提供而非缺失）、
  完整的 ``$response.body#/JSONPointer``（指针解码 ~0/~1）与
  ``$response.header.名称``（头名忽略大小写）；其余运行时表达式
  （$request.* / $statusCode 等）逐条报"不支持"。
- 目标参数：路径级 parameters 与操作级 parameters 按 (位置, 名称) 合并，
  操作级覆盖路径级；Link 键可为 ``位置.名称``，裸名称须在目标参数中唯一，
  否则逐条报歧义 / 未知参数；最终仍缺少必填值（含 path 参数）逐条报错。

纯函数实时计算：不发请求、不入库、不改变审查结论。
"""
from __future__ import annotations

import json
from typing import Any

from .diff import HTTP_METHODS
from .refs import (
    PointerError,
    decode_token,
    encode_token,
    normalize_pointer,
    resolve_pointer,
)
from .samplecheck import find_operation

PARAMETER_LOCATIONS = ("path", "query", "header", "cookie")
_LOCATION_SET = set(PARAMETER_LOCATIONS)
_METHOD_SET = {m.upper() for m in HTTP_METHODS}

_MATCH_EXACT = "exact"
_MATCH_RANGE = "range"
_MATCH_DEFAULT = "default"


class LinkPreviewError(ValueError):
    """请求级错误（源操作 / 响应缺失、入参格式错误），映射为 HTTP 400。"""


# ---------------------------------------------------------------------- #
# $ref 链解析：仅本地、循环安全、外部引用报错、3.1 兄弟字段覆盖
# ---------------------------------------------------------------------- #
def _classify_ref(ref: str) -> tuple[str, str]:
    """把 $ref 分类为 ('local'|'external', fragment)。

    fragment 为 # 之后的部分（本地引用形如 /paths/...）。
    """
    if "#" in ref:
        before, fragment = ref.split("#", 1)
    else:
        before, fragment = "", ref
    if before:
        return "external", fragment
    if fragment in ("", "/"):
        return "local", ""
    if not fragment.startswith("/"):
        return "external", fragment
    return "local", fragment


def _deref_object(
    doc: dict[str, Any], node: Any, what: str
) -> tuple[Any, str | None, str | None]:
    """沿本地 $ref 链解析对象。

    返回 (解析后的节点, 最终目标指针或 None, 错误原因或 None)。
    - 外部引用 / 无法解析 / 链上成环 → 返回错误；
    - 3.1 兄弟字段：引用目标为底，链上各节点的兄弟字段按由内向外覆盖。
    """
    chain: list[tuple[str, dict[str, Any]]] = []
    current = node
    seen: set[str] = set()

    while isinstance(current, dict) and isinstance(current.get("$ref"), str):
        ref = current["$ref"]
        kind, _fragment = _classify_ref(ref)
        if kind == "external":
            return None, None, f"{what}使用了不支持的外部引用：{ref}"
        try:
            pointer = normalize_pointer(ref)
            target = resolve_pointer(doc, pointer)
        except PointerError as exc:
            return None, None, f"{what}的 $ref {ref} 无法解析：{exc}"
        if pointer in seen:
            cycle = " → ".join(chain_p for chain_p, _ in chain) + f" → {pointer}"
            return None, None, f"{what}的本地 $ref 链存在循环：{cycle}"
        seen.add(pointer)
        chain.append((pointer, current))
        current = target

    if chain:
        # 由内向外叠加兄弟字段：最外层（链上第一个）$ref 的兄弟字段优先级最高
        if isinstance(current, dict):
            merged: dict[str, Any] = dict(current)
            for _pointer, ref_node in reversed(chain):
                for key, value in ref_node.items():
                    if key != "$ref":
                        merged[key] = value
            current = merged
        return current, chain[-1][0], None
    return current, None, None


# ---------------------------------------------------------------------- #
# 入参规范化
# ---------------------------------------------------------------------- #
def _parse_body(raw: Any) -> Any:
    """响应体：直接给 JSON 值，或给 JSON 文本字符串；缺省为 null。"""
    if raw is None:
        return None
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise LinkPreviewError(
                f"JSON 响应体格式错误：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）。"
            ) from exc
    return raw


def _normalize_status(raw: Any) -> int:
    if isinstance(raw, bool):
        raise LinkPreviewError("状态码必须是 100–599 的整数。")
    if isinstance(raw, int):
        code = raw
    elif isinstance(raw, str) and raw.strip().isdigit():
        code = int(raw.strip())
    else:
        raise LinkPreviewError("状态码必须是 100–599 的整数。")
    if not 100 <= code <= 599:
        raise LinkPreviewError("状态码必须是 100–599 的整数。")
    return code


# ---------------------------------------------------------------------- #
# operationId 索引（path item / operation 均解析本地 $ref）
# ---------------------------------------------------------------------- #
def _build_operation_index(doc: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    """operationId → 目标列表（长度 > 1 即不唯一）。外部 path 引用跳过。"""
    index: dict[str, list[dict[str, str]]] = {}
    paths = doc.get("paths")
    if not isinstance(paths, dict):
        return index
    for template, raw_item in paths.items():
        if not isinstance(raw_item, dict):
            continue
        item, _, err = _deref_object(doc, raw_item, "路径项")
        if err or not isinstance(item, dict):
            continue
        for method in HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            op_resolved, _, err = _deref_object(doc, op, "操作")
            if err or not isinstance(op_resolved, dict):
                continue
            oid = op_resolved.get("operationId")
            if isinstance(oid, str) and oid.strip():
                index.setdefault(oid.strip(), []).append(
                    {
                        "method": method.upper(),
                        "path": template,
                        "pointer": f"/paths/{encode_token(template)}/{method}",
                    }
                )
    return index


# ---------------------------------------------------------------------- #
# 目标参数：路径级 + 操作级，按 (位置, 名称) 合并，操作级覆盖
# ---------------------------------------------------------------------- #
def _resolve_parameter_list(
    doc: dict[str, Any],
    raw_params: Any,
    level: str,
    errors: list[str],
) -> list[tuple[tuple[str, str], dict[str, Any]]]:
    """解析一层 parameters（路径级或操作级），返回有序的 ((in,name), 参数) 列表。"""
    out: list[tuple[tuple[str, str], dict[str, Any]]] = []
    if raw_params is None:
        return out
    if not isinstance(raw_params, list):
        errors.append(f"目标操作的{level} parameters 必须是数组。")
        return out
    for i, raw_param in enumerate(raw_params):
        if not isinstance(raw_param, dict):
            errors.append(f"目标操作的{level} parameters[{i}] 不是参数对象。")
            continue
        param, _, err = _deref_object(doc, raw_param, "参数")
        if err:
            errors.append(f"目标操作的{level} parameters[{i}]：{err}")
            continue
        if not isinstance(param, dict):
            errors.append(f"目标操作的{level} parameters[{i}] 不是参数对象。")
            continue
        name = param.get("name")
        location = param.get("in")
        if not isinstance(name, str) or not name or location not in _LOCATION_SET:
            errors.append(
                f"目标操作的{level} parameters[{i}] 缺少合法的 name / in"
                "（in 须为 path/query/header/cookie）。"
            )
            continue
        out.append(((location, name), param))
    return out


def _collect_target_parameters(
    doc: dict[str, Any], method: str, template: str
) -> tuple[list[tuple[str, str]], dict[tuple[str, str], dict[str, Any]], list[str]]:
    """合并路径级与操作级参数；操作级同名（同位置）覆盖路径级。

    返回 (有序键列表, 参数映射, 错误列表)。
    """
    errors: list[str] = []
    paths = doc.get("paths")
    raw_item = paths.get(template) if isinstance(paths, dict) else None

    path_params: list[tuple[tuple[str, str], dict[str, Any]]] = []
    if isinstance(raw_item, dict):
        item, _, err = _deref_object(doc, raw_item, "路径项")
        if err:
            errors.append(err)
        elif isinstance(item, dict):
            path_params = _resolve_parameter_list(
                doc, item.get("parameters"), "路径级", errors
            )
            op = item.get(method.lower())
            raw_op_params = op.get("parameters") if isinstance(op, dict) else None
        else:
            raw_op_params = None
    else:
        raw_op_params = None

    # operationRef 指向的操作对象直接取参数（path item 已在上面处理）
    op_params = _resolve_parameter_list(doc, raw_op_params, "操作级", errors)

    ordered: list[tuple[str, str]] = []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for key, param in path_params + op_params:
        if key not in merged:
            ordered.append(key)
        merged[key] = param  # 操作级后写入，覆盖路径级
    return ordered, merged, errors


# ---------------------------------------------------------------------- #
# 运行时表达式取值
# ---------------------------------------------------------------------- #
def _eval_value(
    raw: Any,
    body: Any,
    headers_lower: dict[str, str],
) -> tuple[Any, str | None, str | None]:
    """计算 Link 参数 / requestBody 的取值。

    返回 (值, 来源 constant|response.body|response.header, 错误原因)。
    """
    if not isinstance(raw, str) or not raw.startswith("$"):
        # JSON 常量：对象 / 数组 / 数字 / 布尔 / 普通字符串 / null（null 不算缺失）
        return raw, "constant", None

    expr = raw
    body_prefix = "$response.body#"
    if expr.startswith(body_prefix):
        pointer = expr[len(body_prefix):]
        if pointer and not pointer.startswith("/"):
            return None, None, (
                f"不支持的运行时表达式：{expr}"
                "（$response.body# 后须为 JSON Pointer，如 $response.body#/data/id）。"
            )
        try:
            value = resolve_pointer(body, pointer)
        except PointerError as exc:
            return None, None, f"表达式 {expr} 取值失败：{exc}"
        return value, "response.body", None

    header_prefix = "$response.header."
    if expr.startswith(header_prefix):
        name = expr[len(header_prefix):]
        if not name:
            return None, None, (
                f"不支持的运行时表达式：{expr}（$response.header. 后须给出头名称）。"
            )
        if name.lower() not in headers_lower:
            return None, None, f"响应实例中不存在响应头 {name}（忽略大小写匹配）。"
        return headers_lower[name.lower()], "response.header", None

    return None, None, (
        f"不支持的运行时表达式：{expr}"
        "（仅支持 $response.body#/JSONPointer 与 $response.header.名称）。"
    )


# ---------------------------------------------------------------------- #
# 单条 Link 预览
# ---------------------------------------------------------------------- #
def _resolve_link_key(
    key: str,
    params: dict[tuple[str, str], dict[str, Any]],
) -> tuple[tuple[str, str] | None, str | None]:
    """解析 Link 参数键：'位置.名称' 或唯一裸名称。"""
    if "." in key:
        location, name = key.split(".", 1)
        if location not in _LOCATION_SET:
            return None, (
                f"Link 键 {key!r} 的参数位置 {location!r} 非法"
                "（须为 path/query/header/cookie）。"
            )
        if not name:
            return None, f"Link 键 {key!r} 缺少参数名称。"
        if (location, name) not in params:
            return None, f"目标操作不存在参数 {location}.{name}。"
        return (location, name), None

    matches = [(loc, pname) for (loc, pname) in params if pname == key]
    if not matches:
        return None, f"目标操作不存在名称为 {key} 的参数。"
    if len(matches) > 1:
        located = "、".join(f"{loc}.{pname}" for loc, pname in matches)
        return None, (
            f"Link 键 {key!r} 有歧义：目标操作中同名参数有 {located}，"
            "请使用 '位置.名称' 形式（如 query.id）。"
        )
    return matches[0], None


def _is_required(param: dict[str, Any], location: str) -> bool:
    # OpenAPI 规定 path 参数恒为必填；其余以 required: true 为准
    return location == "path" or param.get("required") is True


def _preview_link(
    doc: dict[str, Any],
    index: int,
    link_name: str,
    raw_link: Any,
    response_pointer: str | None,
    op_index: dict[str, list[dict[str, str]]],
    body: Any,
    headers_lower: dict[str, str],
) -> dict[str, Any]:
    link_pointer = (
        f"{response_pointer}/links/{encode_token(link_name)}"
        if response_pointer
        else f"links/{encode_token(link_name)}"
    )
    result: dict[str, Any] = {
        "index": index,
        "name": link_name,
        "link_pointer": link_pointer,
        "ref": raw_link.get("$ref") if isinstance(raw_link, dict) else None,
        "target": None,
        "parameters": [],
        "resolved_path": None,
        "errors": [],
    }
    errors: list[str] = result["errors"]

    link, _link_ptr, err = _deref_object(doc, raw_link, "Link")
    if err:
        errors.append(err)
        return result
    if not isinstance(link, dict):
        errors.append("Link 必须是对象（含 operationId 或 operationRef）。")
        return result

    operation_id = link.get("operationId")
    operation_ref = link.get("operationRef")
    has_oid = operation_id is not None
    has_oref = operation_ref is not None
    if has_oid and has_oref:
        errors.append("Link 同时指定了 operationId 与 operationRef，目标不唯一。")
        return result
    if not has_oid and not has_oref:
        errors.append("Link 未指定 operationId 或 operationRef，无法确定目标操作。")
        return result

    target: dict[str, str] | None = None
    if has_oid:
        if not isinstance(operation_id, str) or not operation_id.strip():
            errors.append("Link 的 operationId 必须是非空字符串。")
            return result
        operation_id = operation_id.strip()
        candidates = op_index.get(operation_id, [])
        if not candidates:
            errors.append(f"operationId {operation_id!r} 在本文档中不存在。")
            return result
        if len(candidates) > 1:
            located = "、".join(f"{c['method']} {c['path']}" for c in candidates)
            errors.append(
                f"operationId {operation_id!r} 在本文档中不唯一（{located}），"
                "无法确定唯一目标操作。"
            )
            return result
        target = dict(candidates[0])
        result["operation_id"] = operation_id
    else:
        if not isinstance(operation_ref, str) or not operation_ref.strip():
            errors.append("Link 的 operationRef 必须是非空字符串。")
            return result
        kind, _fragment = _classify_ref(operation_ref)
        if kind == "external":
            errors.append(f"operationRef 必须是本地引用，不支持外部引用：{operation_ref}")
            return result
        try:
            pointer = normalize_pointer(operation_ref)
            node = resolve_pointer(doc, pointer)
        except PointerError as exc:
            errors.append(f"operationRef {operation_ref} 无法解析：{exc}")
            return result
        parts = pointer.lstrip("/").split("/") if pointer else []
        if (
            len(parts) != 3
            or parts[0] != "paths"
            or parts[2].upper() not in _METHOD_SET
        ):
            errors.append(
                f"operationRef {operation_ref} 未指向 paths 下的具体操作"
                "（应为 #/paths/路径模板/方法 形式）。"
            )
            return result
        template = decode_token(parts[1])
        method = parts[2].upper()
        if not isinstance(doc.get("paths"), dict) or template not in doc["paths"]:
            errors.append(f"operationRef 引用的路径模板 {template} 不存在。")
            return result
        if not isinstance(node, dict):
            errors.append(f"operationRef {operation_ref} 指向的不是操作对象。")
            return result
        target = {"method": method, "path": template, "pointer": pointer}
        result["operation_ref"] = operation_ref

    result["target"] = {
        "method": target["method"],
        "path": target["path"],
        "pointer": target["pointer"],
    }

    # ---- 目标参数（路径级 + 操作级合并）----
    ordered_keys, params, param_errors = _collect_target_parameters(
        doc, target["method"].lower(), target["path"]
    )
    errors.extend(param_errors)

    # 名称 → 位置列表（供裸名歧义判断）
    provided: dict[tuple[str, str], dict[str, Any]] = {}

    mappings = link.get("parameters")
    if mappings is not None:
        if not isinstance(mappings, dict):
            errors.append("Link 的 parameters 必须是映射对象（参数键 → 取值表达式）。")
        else:
            for key, raw_value in mappings.items():
                if not isinstance(key, str):
                    errors.append("Link 的 parameters 中存在非字符串键。")
                    continue
                resolved_key, key_err = _resolve_link_key(key, params)
                if key_err:
                    errors.append(key_err)
                    continue
                value, source, val_err = _eval_value(raw_value, body, headers_lower)
                if val_err:
                    location, name = resolved_key  # type: ignore[misc]
                    errors.append(f"参数 {location}.{name} 的取值：{val_err}")
                    continue
                expression = raw_value if isinstance(raw_value, str) and raw_value.startswith("$") else None
                provided[resolved_key] = {  # type: ignore[index]
                    "value": value,
                    "value_source": source,
                    "expression": expression,
                }

    # ---- 输出参数清单 + 必填检查（null 已提供，不报缺失）----
    param_rows: list[dict[str, Any]] = []
    path_values: dict[str, str] = {}
    for location, name in ordered_keys:
        param = params[(location, name)]
        given = provided.get((location, name))
        row = {
            "name": name,
            "in": location,
            "required": _is_required(param, location),
            "provided": given is not None,
            "value": given["value"] if given else None,
            "value_source": given["value_source"] if given else None,
            "expression": given["expression"] if given else None,
        }
        param_rows.append(row)
        if row["required"] and given is None:
            errors.append(
                f"目标参数 {location}.{name} 为必填，但 Link 未提供取值。"
            )
        if location == "path" and given is not None:
            path_values[name] = given["value"]
    result["parameters"] = param_rows

    # 预览拼好的路径（仅当全部 path 参数都有值；纯展示，不发请求）
    needed_path = {name for loc, name in ordered_keys if loc == "path"}
    if needed_path and needed_path <= set(path_values):
        resolved = target["path"]
        for name, value in path_values.items():
            resolved = resolved.replace(
                "{" + name + "}", "" if value is None else str(value)
            )
        result["resolved_path"] = resolved

    # ---- Link.requestBody（同样只支持常量与两类表达式）----
    if "requestBody" in link:
        value, source, rb_err = _eval_value(
            link["requestBody"], body, headers_lower
        )
        if rb_err:
            errors.append(f"Link.requestBody 的取值：{rb_err}")
        else:
            rb_expression = (
                link["requestBody"]
                if isinstance(link["requestBody"], str)
                and link["requestBody"].startswith("$")
                else None
            )
            result["request_body"] = {
                "value": value,
                "value_source": source,
                "expression": rb_expression,
            }

    return result


# ---------------------------------------------------------------------- #
# 顶层入口
# ---------------------------------------------------------------------- #
def _match_response(
    responses: dict[str, Any], status_code: int
) -> tuple[str | None, str | None]:
    """依次按精确码、范围码、default 匹配，返回 (匹配键, 匹配方式)。"""
    keys = {str(k): k for k in responses}
    exact = str(status_code)
    if exact in keys:
        return exact, _MATCH_EXACT
    range_key = f"{exact[0]}XX"
    for key in keys:
        if key.upper() == range_key:
            return key, _MATCH_RANGE
    if "default" in keys:
        return "default", _MATCH_DEFAULT
    return None, None


def build_link_preview(doc: dict[str, Any], req: dict[str, Any]) -> dict[str, Any]:
    """根据响应实例预览源操作各 Link 的后续调用参数（纯函数、无副作用）。"""
    side = str(req.get("side") or "old").strip().lower()
    if side not in ("old", "new"):
        raise LinkPreviewError("side 必须是 old 或 new。")

    method = str(req.get("method") or "").strip().upper()
    if not method:
        raise LinkPreviewError("缺少 method（源操作的 HTTP 方法）。")
    if method not in _METHOD_SET:
        raise LinkPreviewError(f"不支持的 HTTP 方法：{req.get('method')}。")

    template = str(req.get("path") or "").strip()
    if not template:
        raise LinkPreviewError("缺少 path（源操作的路径模板）。")
    if not template.startswith("/"):
        raise LinkPreviewError(f"路径模板必须以 / 开头：{template}。")

    status_code = _normalize_status(req.get("status_code", req.get("statusCode")))
    body = _parse_body(req.get("body"))
    raw_headers = req.get("headers")
    if raw_headers is None:
        raw_headers = {}
    if not isinstance(raw_headers, dict):
        raise LinkPreviewError("headers 必须是对象（头名称 → 头值）。")
    headers_lower = {str(k).lower(): v for k, v in raw_headers.items()}

    # ---- 源操作 / 响应定位 ----
    operation = find_operation(doc, method, template)
    if operation is None:
        raise LinkPreviewError(f"{'旧版' if side == 'old' else '新版'}中不存在操作 {method} {template}。")

    responses = operation.get("responses")
    if not isinstance(responses, dict):
        raise LinkPreviewError(
            f"源操作 {method} {template} 未定义 responses，无法匹配状态码 {status_code}。"
        )
    matched_key, match_type = _match_response(responses, status_code)
    if matched_key is None:
        available = "、".join(str(k) for k in responses) or "（空）"
        raise LinkPreviewError(
            f"源操作 {method} {template} 没有可匹配状态码 {status_code} 的响应"
            f"（依次尝试精确码 {status_code}、范围码 {status_code // 100}XX、default）；"
            f"已声明：{available}。"
        )

    raw_response = responses[matched_key]
    response, response_pointer, err = _deref_object(doc, raw_response, "响应")
    if err:
        raise LinkPreviewError(err)
    if not isinstance(response, dict):
        raise LinkPreviewError(f"响应 {matched_key} 不是合法的响应对象。")
    if response_pointer is None:
        response_pointer = (
            f"/paths/{encode_token(template)}/{method.lower()}"
            f"/responses/{encode_token(matched_key)}"
        )

    source_pointer = f"/paths/{encode_token(template)}/{method.lower()}"

    # ---- 逐条 Link ----
    op_index = _build_operation_index(doc)
    raw_links = response.get("links")
    link_items: list[dict[str, Any]] = []
    if raw_links is not None and not isinstance(raw_links, dict):
        link_items.append(
            {
                "index": 1,
                "name": None,
                "link_pointer": f"{response_pointer}/links",
                "ref": None,
                "target": None,
                "parameters": [],
                "resolved_path": None,
                "errors": ["响应的 links 必须是映射对象（Link 名称 → Link 定义）。"],
            }
        )
    elif isinstance(raw_links, dict):
        for i, (link_name, raw_link) in enumerate(raw_links.items(), start=1):
            link_items.append(
                _preview_link(
                    doc,
                    i,
                    str(link_name),
                    raw_link,
                    response_pointer,
                    op_index,
                    body,
                    headers_lower,
                )
            )

    error_count = sum(1 for item in link_items if item["errors"])
    return {
        "side": side,
        "source": {
            "method": method,
            "path": template,
            "pointer": source_pointer,
            "status_code": status_code,
            "response_key": matched_key,
            "match_type": match_type,
            "response_pointer": response_pointer,
        },
        "summary": {
            "links": len(link_items),
            "ok": len(link_items) - error_count,
            "errors": error_count,
        },
        "links": link_items,
        "note": "仅为 Link 后续调用参数预览：不发送请求、不入库、不改变审查结论。",
    }

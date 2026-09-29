"""响应实例 → OpenAPI Link 后续调用参数预览。

在审查详情中选择旧 / 新版文档、源操作（HTTP 方法 + 路径模板），提交一次
响应实例（状态码、JSON 响应体、响应头），后端：

1. 按 **精确码 → 范围码（1XX…5XX）→ default** 的顺序匹配源操作的响应；
2. 逐条取出该响应的 Link（支持 components 内 `$ref`，循环安全）；
3. 解析 Link 目标：唯一 `operationId` 或文档内 `operationRef`（外部引用报错）；
4. 合并目标操作的**路径级 + 操作级 parameters**（同位置同名操作级覆盖），
   把 Link `parameters` 表达式解析成实际值（JSON 常量 /
   `$response.body#/JSONPointer` / `$response.header.名称`），并预览 `requestBody`；
5. 逐条报告：目标方法 / 路径、每个参数的值、未知参数 / 歧义键 / 缺少的必填值 /
   不支持的表达式 / 无法解析的指针或头 / 外部或失效 `$ref`。

**只做静态预览**：不发送任何请求、不入库、不改变审查结论。
"""
from __future__ import annotations

from typing import Any

from .diff import HTTP_METHODS
from .refs import PointerError, decode_token, encode_token, resolve_pointer

_METHOD_SET = {m.upper() for m in HTTP_METHODS}
_PARAM_LOCATIONS = ("path", "query", "header", "cookie")


class LinkPreviewError(ValueError):
    """文件性 / 请求级错误（无法进行预览），映射为 HTTP 400。"""


# ---------------------------------------------------------------------- #
# 本地 $ref（显式拒绝外部引用；链循环安全终止）
# ---------------------------------------------------------------------- #
def _local_ref_pointer(ref: Any) -> str:
    """把本地 $ref 归一化为 JSON Pointer；外部 / 非法引用直接报错。"""
    if not isinstance(ref, str) or not ref:
        raise LinkPreviewError("非法的 $ref（必须是非空字符串）。")
    if "#" in ref:
        before, fragment = ref.split("#", 1)
        if before.strip():
            # http://… / ./other.yaml / urn:… 等带文档前缀的一律是外部引用
            raise LinkPreviewError(f"不支持的外部引用：{ref}（仅支持本文档内 #/… 引用）")
    else:
        fragment = ref
    if fragment in ("", "/"):
        return ""
    if fragment.startswith("/"):
        return fragment
    raise LinkPreviewError(f"不支持的外部引用：{ref}（仅支持本文档内 #/… 引用）")


def _deref_chain(doc: dict[str, Any], node: Any) -> tuple[Any, str, str | None]:
    """沿本地 $ref 链解引用。

    返回 (有效节点, 有效节点指针, 错误码+消息)。循环引用安全终止（报
    circular-ref 而不无限递归）；外部 / 无法解析的引用同样终止并报错。
    """
    pointer = ""
    seen: set[str] = set()
    last_ref: str | None = None
    while isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        last_ref = ref
        try:
            target_pointer = _local_ref_pointer(ref)
        except LinkPreviewError:
            return node, pointer, ("external-ref", f"不支持的外部引用：{ref}（仅支持本文档内引用）")
        if target_pointer in seen:
            return node, pointer, ("circular-ref", f"$ref 链存在循环：{ref}")
        seen.add(target_pointer)
        try:
            node = resolve_pointer(doc, target_pointer)
        except PointerError as exc:
            return node, pointer, ("unresolved-ref", f"$ref 目标无法解析：{ref}（{exc}）")
        pointer = target_pointer
    return node, pointer, None


# ---------------------------------------------------------------------- #
# 源操作 / 响应匹配
# ---------------------------------------------------------------------- #
def _find_source_operation(
    doc: dict[str, Any], method: str, template: str, side_label: str
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """返回 (操作对象, 操作指针, 有效 path item)。"""
    paths = doc.get("paths")
    if not isinstance(paths, dict) or template not in paths or not isinstance(paths[template], dict):
        raise LinkPreviewError(f"{side_label}文档中不存在路径模板 {template}。")
    item_node = paths[template]
    item, item_pointer, item_err = _deref_chain(doc, item_node)
    if item_err is not None:
        code, message = item_err
        ref_note = "（路径项引用）"
        raise LinkPreviewError(_ref_message(code, message, ref_note))
    if not isinstance(item, dict):
        raise LinkPreviewError(f"{side_label}路径模板 {template} 的定义不是路径项对象。")
    method_key = method.lower()
    if method_key not in item or not isinstance(item.get(method_key), dict):
        available = ", ".join(m.upper() for m in HTTP_METHODS if isinstance(item.get(m), dict))
        raise LinkPreviewError(
            f"{side_label}路径模板 {template} 上不存在操作 {method.upper()}。"
            + (f" 已定义的方法：{available}。" if available else " 该路径未定义任何操作。")
        )
    op_node = item[method_key]
    op, op_pointer, op_err = _deref_chain(doc, op_node)
    declared_pointer = f"/paths/{encode_token(template)}/{method.lower()}"
    if op_err is not None:
        code, message = op_err
        raise LinkPreviewError(_ref_message(code, message, f"（操作 {method.upper()} {template}）"))
    if not isinstance(op, dict):
        raise LinkPreviewError(f"{side_label}操作 {method.upper()} {template} 的定义不是对象。")
    return op, op_pointer or declared_pointer, item


def _ref_message(code: str, message: str, where: str) -> str:
    if code == "external-ref":
        return message + where
    if code == "circular-ref":
        return message + where
    return message + where


def _match_response(
    doc: dict[str, Any], op: dict[str, Any], status_code: int, op_pointer: str
) -> dict[str, Any]:
    """精确码 → 范围码 → default；返回响应描述（含解引用后的响应对象）。"""
    responses = op.get("responses")
    if not isinstance(responses, dict):
        raise LinkPreviewError("源操作没有 responses 对象，无法预览 Link。")

    # YAML 里状态码可能解析成整数，统一按字符串处理
    keys = {str(k): k for k in responses.keys()}
    lower_keys = {k.lower(): k for k in keys}
    exact = str(status_code)
    range_key = f"{status_code // 100}XX"

    if exact in keys:
        match, matched_key = "exact", exact
    elif 100 <= status_code <= 599 and range_key.lower() in lower_keys:
        # 范围码按 OAS 规范写作 2XX，小写 2xx 也容错匹配
        match = "range"
        matched_key = keys[lower_keys[range_key.lower()]]
    elif "default" in keys:
        match, matched_key = "default", "default"
    else:
        available = ", ".join(keys) or "（空）"
        raise LinkPreviewError(
            f"源操作没有可匹配状态码 {status_code} 的响应（依次尝试精确码 {exact}、"
            f"范围码 {range_key}、default）。"
            f"已声明的响应键：{', '.join(keys) or '（空）'}。"
        )

    declared_pointer = f"{op_pointer}/responses/{encode_token(matched_key)}"
    node = responses[keys[matched_key]]
    response, eff_pointer, err = _deref_chain(doc, node)
    if err is not None:
        code, message = err
        raise LinkPreviewError(_ref_message(code, message, f"（响应 {matched_key}）"))
    if not isinstance(response, dict):
        raise LinkPreviewError(f"匹配到的响应 {matched_key} 定义不是响应对象。")
    return {
        "key": matched_key,
        "match": match,
        "declared_pointer": declared_pointer,
        "pointer": eff_pointer or declared_pointer,
        "node": response,
    }


# ---------------------------------------------------------------------- #
# 目标操作索引（operationId 唯一；operationRef 本地校验）
# ---------------------------------------------------------------------- #
def _iter_entry_operations(doc: dict[str, Any]):
    """产出 (operationId|None, METHOD, 路径/名称, 操作对象, 容器类型)。"""
    for container_name, kind in (("paths", "path"), ("webhooks", "webhook")):
        container = doc.get(container_name)
        if not isinstance(container, dict):
            continue
        for template, raw_item in container.items():
            if not isinstance(raw_item, dict):
                continue
            item, _, err = _deref_chain(doc, raw_item)
            if err or not isinstance(item, dict):
                continue
            for m in HTTP_METHODS:
                op = item.get(m)
                if isinstance(op, dict):
                    eff_op, _, oerr = _deref_chain(doc, op)
                    if oerr or not isinstance(eff_op, dict):
                        continue
                    op_id = eff_op.get("operationId")
                    yield op_id if isinstance(op_id, str) else None, m.upper(), template, eff_op, kind


def _build_operation_id_index(doc: dict[str, Any]) -> dict[str, list[dict[str, str]]]:
    index: dict[str, list[dict[str, str]]] = {}
    for op_id, method, template, _op, kind in _iter_entry_operations(doc):
        if op_id is None:
            continue
        if kind == "path":
            pointer = f"/paths/{encode_token(template)}/{method.lower()}"
        else:
            pointer = f"/webhooks/{encode_token(template)}/{method.lower()}"
        index.setdefault(op_id, []).append(
            {"method": method, "path": template, "pointer": pointer, "kind": kind}
        )
    return index


def _describe_operation_ref(doc: dict[str, Any], ref: str) -> dict[str, Any]:
    """解析本地 operationRef 并确认落点是操作对象。"""
    try:
        pointer = _local_ref_pointer(ref)
    except LinkPreviewError as exc:
        return {"error": ("operation-ref-external", str(exc))}
    if not pointer:
        return {"error": ("operation-ref-invalid", f"operationRef {ref} 指向文档根，不是操作对象。")}
    try:
        node = resolve_pointer(doc, pointer)
    except PointerError as exc:
        return {"error": ("operation-ref-unresolved", f"operationRef 无法解析：{ref}（{exc}）")}
    if not isinstance(node, dict):
        return {"error": ("operation-ref-not-operation", f"operationRef {ref} 的目标不是操作对象。")}

    tokens = [t for t in pointer.split("/") if t != ""]
    method = decode_token(tokens[-1]).upper() if tokens else ""
    path = ""
    kind = "operationRef"
    if len(tokens) >= 3 and tokens[0] in ("paths", "webhooks"):
        method = decode_token(tokens[-1]).upper()
        path = decode_token(tokens[1])
        kind = "path" if tokens[0] == "paths" else "webhook"

    if method not in _METHOD_SET or "responses" not in node:
        return {
            "error": (
                "operation-ref-not-operation",
                f"operationRef {ref} 的目标不是 HTTP 操作对象（末段须为 get/post/… 且含 responses）。",
            )
        }
    return {
        "method": method,
        "path": path,
        "pointer": pointer,
        "kind": kind,
    }


# ---------------------------------------------------------------------- #
# 目标参数（路径级 + 操作级合并，操作级覆盖）
# ---------------------------------------------------------------------- #
def _resolve_parameter(
    doc: dict[str, Any], raw_param: Any
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    """解引用单个参数对象（循环安全）；返回 (参数对象, 错误)。"""
    node, _pointer, err = _deref_chain(doc, raw_param)
    if err is not None:
        code, message = err
        return None, (f"parameter-{code}", message)
    if not isinstance(node, dict):
        return None, ("parameter-invalid", "参数定义不是对象。")
    name = node.get("name")
    location = node.get("in")
    if not isinstance(name, str) or not isinstance(location, str):
        return None, ("parameter-invalid", "参数对象缺少有效的 name / in 字段。")
    return node, None


def _collect_target_parameters(
    doc: dict[str, Any], item: dict[str, Any], op: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """合并 path item 级与操作级 parameters：同 (in,name) 操作级覆盖。

    返回 (参数列表[保持声明顺序，覆盖项保留操作级位置], 参数级错误列表)。
    """
    errors: list[dict[str, str]] = []
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []

    def consume(raw_list: Any, level: str) -> None:
        if not isinstance(raw_list, list):
            return
        for raw in raw_list:
            param, perr = _resolve_parameter(doc, raw)
            if perr is not None:
                code, message = perr
                errors.append(
                    {
                        "code": code,
                        "message": f"目标操作的{('路径级' if level == 'path' else '操作级')}参数无法使用：{message}",
                    }
                )
                continue
            key = (param["in"], param["name"])  # type: ignore[index]
            if key not in merged:
                order.append(key)
            merged[key] = {"node": param, "level": level}  # type: ignore[index]

    consume(item.get("parameters"), "path")
    consume(op.get("parameters"), "operation")

    params: list[dict[str, Any]] = []
    for location, name in order:
        node = merged[(location, name)]["node"]
        level = merged[(location, name)]["level"]
        required = bool(node.get("required")) or location == "path"
        params.append(
            {
                "name": name,
                "in": location,
                "required": required,
                "level": level,
                "deprecated": bool(node.get("deprecated", False)),
            }
        )
    return params, errors


# ---------------------------------------------------------------------- #
# 运行时表达式求值（只支持 $response.body#… 与 $response.header.…）
# ---------------------------------------------------------------------- #
class _ExpressionError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _eval_expression(expression: str, body: Any, headers_ci: dict[str, Any]) -> Any:
    if expression.startswith("$response.body#"):
        pointer = expression[len("$response.body#"):]
        if pointer and not pointer.startswith("/"):
            raise _ExpressionError(
                "unsupported-expression",
                f"不支持的表达式 {expression}：$response.body 后须为 #/ 开头的 JSON Pointer。",
            )
        try:
            return resolve_pointer(body, pointer or "")
        except PointerError as exc:
            raise _ExpressionError(
                "expression-not-resolved",
                f"响应体指针 {pointer or '#'} 无法取值：{exc}",
            ) from exc

    if expression.startswith("$response.header."):
        header_name = expression[len("$response.header."):]
        if not header_name:
            raise _ExpressionError(
                "unsupported-expression",
                f"不支持的表达式 {expression}：缺少响应头名称。",
            )
        if header_name.lower() not in headers_ci:
            raise _ExpressionError(
                "header-not-found",
                f"提交的响应实例中没有响应头 {header_name}（头名称忽略大小写）。",
            )
        return headers_ci[header_name.lower()]

    if expression.startswith("$response.body"):
        raise _ExpressionError(
            "unsupported-expression",
            f"不支持的表达式 {expression}：$response.body 须以 #JSONPointer 形式给出（如 $response.body#/id）。",
        )
    raise _ExpressionError(
        "unsupported-expression",
        f"不支持的表达式 {expression}：仅支持 JSON 常量、$response.body#/JSONPointer 与 "
        "$response.header.名称（$request.* / $method 等其余表达式不在支持范围）。",
    )


def _resolve_value(raw: Any, body: Any, headers_ci: dict[str, Any]) -> dict[str, Any]:
    """把 Link 参数 / requestBody 原值解析为实例值。

    返回 {value, expression, provided, errors:[{code,message}]}。
    字符串以 $ 开头视为运行时表达式；其余一律按 JSON 常量处理（null 是
    合法常量，算"已提供"）。
    """
    if isinstance(raw, str) and raw.startswith("$"):
        try:
            value = _eval_expression(raw, body, headers_ci)
        except _ExpressionError as exc:
            return {
                "value": None,
                "expression": raw,
                "provided": False,
                "errors": [{"code": exc.code, "message": exc.message}],
            }
        return {"value": value, "expression": raw, "provided": True, "errors": []}
    return {"value": raw, "expression": None, "provided": True, "errors": []}


# ---------------------------------------------------------------------- #
# 单条 Link 预览
# ---------------------------------------------------------------------- #
def _err(code: str, message: str) -> dict[str, str]:
    return {"code": code, "message": message}


def _preview_link(
    doc: dict[str, Any],
    link_name: str,
    raw_link: Any,
    declared_pointer: str,
    target_index: dict[str, list[dict[str, str]]],
    target_params_cache: dict[str, tuple[list[dict[str, Any]], list[dict[str, str]]]],
    body: Any,
    headers_ci: dict[str, Any],
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": link_name,
        "link_pointer": declared_pointer,
        "ref": raw_link.get("$ref") if isinstance(raw_link, dict) and raw_link.get("$ref") else None,
        "errors": [],
        "target": None,
        "parameters": [],
        "unknown_keys": [],
        "missing_required": [],
        "request_body": None,
    }

    link, eff_pointer, err = _deref_chain(doc, raw_link)
    if err:
        code, message = err
        result["errors"].append(_err(f"link-{code}", f"Link {link_name}：{message}"))
        return result
    if eff_pointer:
        result["link_pointer"] = eff_pointer
    if not isinstance(link, dict):
        result["errors"].append(_err("link-invalid", f"Link {link_name} 的定义不是对象。"))
        return result

    # ---- 目标解析 ----
    op_id = link.get("operationId")
    op_ref = link.get("operationRef")
    has_id = isinstance(op_id, str)
    has_ref = isinstance(op_ref, str)
    target: dict[str, Any] | None = None

    if has_id and has_ref:
        result["errors"].append(
            _err(
                "operation-both-id-and-ref",
                f"Link {link_name} 同时给出 operationId 与 operationRef，二者互斥。",
            )
        )
    elif has_ref:
        desc = _describe_operation_ref(doc, op_ref)
        if "error" in desc:
            code, message = desc["error"]
            result["errors"].append(_err(code, f"Link {link_name}：{message}"))
        else:
            target = {
                "kind": "operationRef",
                "operation_id": None,
                "operation_ref": op_ref,
                "method": desc["method"],
                "path": desc["path"],
                "pointer": desc["pointer"],
            }
    elif has_id:
        hits = target_index.get(op_id, [])
        if not hits:
            result["errors"].append(
                _err("operation-id-not-found", f"Link {link_name}：operationId '{op_id}' 在文档中不存在。")
            )
        elif len(hits) > 1:
            candidates = "; ".join(f"{h['method']} {h['path']}" for h in hits)
            result["errors"].append(
                _err(
                    "operation-id-ambiguous",
                    f"Link {link_name}：operationId '{op_id}' 不唯一，命中 {len(hits)} 个操作（{candidates}）。",
                )
            )
        else:
            h = hits[0]
            target = {
                "kind": "operationId",
                "operation_id": op_id,
                "operation_ref": None,
                "method": h["method"],
                "path": h["path"],
                "pointer": h["pointer"],
            }
    else:
        result["errors"].append(
            _err(
                "link-no-target",
                f"Link {link_name} 既没有 operationId 也没有 operationRef，无法确定后续调用目标。",
            )
        )
    result["target"] = target

    if target is None:
        # 目标不可用：参数映射无从校验，但仍尝试解析 requestBody 表达式以便报错
        if "requestBody" in link:
            resolved = _resolve_value(link["requestBody"], body, headers_ci)
            rb = {"provided": True, **{k: resolved[k] for k in ("value", "expression")},
                  "errors": resolved["errors"]}
            result["request_body"] = rb
            result["errors"].extend(rb["errors"])
        return result

    # ---- 目标参数（按目标指针缓存合并结果；path item 取目标指针推导）----
    cache_key = target["pointer"]
    if cache_key not in target_params_cache:
        target_item = _target_path_item(doc, target["pointer"], target["kind"])
        target_op = resolve_pointer(doc, target["pointer"])
        target_params_cache[cache_key] = _collect_target_parameters(
            doc, target_item or {}, target_op if isinstance(target_op, dict) else {}
        )
    params, param_errors = target_params_cache[cache_key]
    for pe in param_errors:
        result["errors"].append(_err(pe["code"], f"Link {link_name}：{pe['message']}"))

    by_key = {(p["in"], p["name"]): p for p in params}
    bare_index: dict[str, list[dict[str, Any]]] = {}
    for p in params:
        bare_index.setdefault(p["name"], []).append(p)

    assigned: dict[tuple[str, str], dict[str, Any]] = {}

    raw_parameters = link.get("parameters")
    if raw_parameters is not None and not isinstance(raw_parameters, dict):
        result["errors"].append(
            _err("link-parameters-invalid", f"Link {link_name} 的 parameters 必须是参数名 → 值的映射对象。")
        )
        raw_parameters = {}

    for key, raw_value in (raw_parameters or {}).items():
        resolved = _resolve_value(raw_value, body, headers_ci)
        location: str | None = None
        name = key
        if "." in key:
            prefix, rest = key.split(".", 1)
            if prefix not in _PARAM_LOCATIONS:
                result["unknown_keys"].append(
                    {
                        "key": key,
                        "value": resolved["value"],
                        "expression": resolved["expression"],
                        "errors": [
                            _err(
                                "unknown-parameter",
                                f"参数键 {key} 的位置 '{prefix}' 非法（应为 path/query/header/cookie）。",
                            ),
                            *resolved["errors"],
                        ],
                    }
                )
                for e in resolved["errors"]:
                    result["errors"].append(_err(e["code"], f"Link {link_name} 参数键 {key}：{e['message']}"))
                continue
            location, name = prefix, rest

        if location is None:
            candidates = bare_index.get(name, [])
            if len(candidates) > 1:
                locs = ", ".join(f"{c['in']}.{c['name']}" for c in candidates)
                result["unknown_keys"].append(
                    {
                        "key": key,
                        "value": resolved["value"],
                        "expression": resolved["expression"],
                        "errors": [
                            _err(
                                "ambiguous-parameter",
                                f"裸参数名 '{name}' 在目标操作中不唯一（{locs}），请改用 位置.名称 形式。",
                            ),
                            *resolved["errors"],
                        ],
                    }
                )
                for e in resolved["errors"]:
                    result["errors"].append(_err(e["code"], f"Link {link_name} 参数键 {key}：{e['message']}"))
                continue
            if not candidates:
                result["unknown_keys"].append(
                    {
                        "key": key,
                        "value": resolved["value"],
                        "expression": resolved["expression"],
                        "errors": [
                            _err(
                                "unknown-parameter",
                                f"目标操作 {target['method']} {target['path'] or target['pointer']} "
                                f"没有参数 '{name}'。",
                            ),
                            *resolved["errors"],
                        ],
                    }
                )
                for e in resolved["errors"]:
                    result["errors"].append(_err(e["code"], f"Link {link_name} 参数键 {key}：{e['message']}"))
                continue
            matched = candidates[0]
            location, name = matched["in"], matched["name"]

        param_def = by_key.get((location, name))
        if param_def is None:
            result["unknown_keys"].append(
                {
                    "key": key,
                    "value": resolved["value"],
                    "expression": resolved["expression"],
                    "errors": [
                        _err(
                            "unknown-parameter",
                            f"目标操作 {target['method']} {target['path'] or target['pointer']} "
                            f"没有 {location} 位置的参数 '{name}'。",
                        ),
                        *resolved["errors"],
                    ],
                }
            )
            for e in resolved["errors"]:
                result["errors"].append(_err(e["code"], f"Link {link_name} 参数键 {key}：{e['message']}"))
            continue

        assigned[(location, name)] = {
            "name": name,
            "in": location,
            "required": param_def["required"],
            "level": param_def["level"],
            "provided": resolved["provided"],
            "value": resolved["value"],
            "expression": resolved["expression"],
            "errors": resolved["errors"],
        }

    # ---- 输出目标全部参数（未赋值的标 provided=false），并汇总必填缺失 ----
    parameters_out: list[dict[str, Any]] = []
    missing_required: list[dict[str, str]] = []
    for p in params:
        slot = assigned.get((p["in"], p["name"]))
        if slot is None:
            slot = {
                "name": p["name"],
                "in": p["in"],
                "required": p["required"],
                "level": p["level"],
                "provided": False,
                "value": None,
                "expression": None,
                "errors": [],
            }
        for e in slot["errors"]:
            result["errors"].append(_err(e["code"], f"Link {link_name} 参数 {slot['in']}.{slot['name']}：{e['message']}"))
        parameters_out.append(slot)
        if p["required"] and not slot["provided"]:
            missing_required.append({"name": p["name"], "in": p["in"]})
    result["parameters"] = parameters_out
    result["missing_required"] = missing_required

    # ---- requestBody（非参数；同样支持常量 / $response.* 表达式）----
    if "requestBody" in link:
        resolved = _resolve_value(link["requestBody"], body, headers_ci)
        result["request_body"] = {
            "provided": True,
            "value": resolved["value"],
            "expression": resolved["expression"],
            "errors": resolved["errors"],
        }
        for e in resolved["errors"]:
            result["errors"].append(_err(e["code"], f"Link {link_name} requestBody：{e['message']}"))

    return result


def _target_path_item(doc: dict[str, Any], target_pointer: str, kind: str) -> dict[str, Any] | None:
    """根据目标操作指针取回其所属 path item（用于路径级参数合并）。"""
    tokens = [t for t in target_pointer.split("/") if t != ""]
    if len(tokens) >= 3 and tokens[0] in ("paths", "webhooks"):
        item_pointer = "/" + "/".join(tokens[:2])
        try:
            raw = resolve_pointer(doc, item_pointer)
        except PointerError:
            return None
        item, _, err = _deref_chain(doc, raw)
        return item if not err and isinstance(item, dict) else None
    return None


# ---------------------------------------------------------------------- #
# 入口
# ---------------------------------------------------------------------- #
def build_link_preview(
    doc: dict[str, Any],
    *,
    side: str,
    method: str,
    path: str,
    status_code: int,
    response_body: Any,
    headers: dict[str, Any] | None,
) -> dict[str, Any]:
    side_label = "旧版" if side == "old" else "新版"
    method_upper = (method or "").strip().upper()
    if method_upper not in _METHOD_SET:
        raise LinkPreviewError(
            f"非法 HTTP 方法：{method!r}（支持 {', '.join(sorted(_METHOD_SET))}）。"
        )
    template = (path or "").strip()
    if not template.startswith("/"):
        raise LinkPreviewError("路径模板须以 / 开头（如 /pets/{id}）。")
    if not isinstance(status_code, int) or isinstance(status_code, bool) or not (100 <= status_code <= 599):
        raise LinkPreviewError("状态码须为 100–599 之间的整数。")

    headers = headers or {}
    if not isinstance(headers, dict):
        raise LinkPreviewError("响应头必须是 JSON 对象（头名称 → 值）。")
    headers_ci = {str(k).lower(): v for k, v in headers.items()}

    op, op_pointer, item = _find_source_operation(doc, method_upper, template, side_label)
    matched = _match_response(doc, op, status_code, op_pointer)
    response_node = matched["node"]

    target_index = _build_operation_id_index(doc)
    target_params_cache: dict[str, tuple[list[dict[str, Any]], list[dict[str, str]]]] = {}

    links_raw = response_node.get("links")
    links_out: list[dict[str, Any]] = []
    if isinstance(links_raw, dict):
        for link_name, raw_link in links_raw.items():
            declared = f"{matched['declared_pointer']}/links/{encode_token(str(link_name))}"
            links_out.append(
                _preview_link(
                    doc,
                    str(link_name),
                    raw_link,
                    declared,
                    target_index,
                    target_params_cache,
                    response_body,
                    headers_ci,
                )
            )

    links_with_errors = sum(1 for l in links_out if l["errors"] or l["unknown_keys"])
    links_missing_required = sum(1 for l in links_out if l["missing_required"])
    return {
        "side": side,
        "source": {"method": method_upper, "path": template},
        "status_code": status_code,
        "matched_response": {
            "key": matched["key"],
            "match": matched["match"],
            "pointer": matched["pointer"],
            "declared_pointer": matched["declared_pointer"],
        },
        "links_count": len(links_out),
        "links_with_errors": links_with_errors,
        "links_missing_required": links_missing_required,
        "links": links_out,
        "note": "仅基于提交的响应实例做静态预览：不发送请求、不入库、不改变审查结论。",
    }

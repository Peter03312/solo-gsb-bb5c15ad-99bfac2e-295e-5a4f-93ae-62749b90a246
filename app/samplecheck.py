"""请求样本回放：用一批真实请求体样本检验"旧版接受而新版拒绝"的风险。

每条样本包含：旧版操作的 HTTP 方法 + 路径模板、Content-Type、JSON 请求体。
后端在旧版 / 新版文档中按**相同方法与路径模板**查找操作，取对应媒体类型的
requestBody schema，对 JSON 体做一个**受限子集的 JSON Schema 校验器**校验。

校验状态（每侧独立）：
  accepted       —— 覆盖范围内的约束全部满足
  rejected       —— 存在覆盖范围内的硬违反（带失败字段路径）
  undeterminable —— 仅遇到无法解析的引用或覆盖范围外约束（不计风险）
  missing        —— 操作 / 媒体类型 / 请求体在该侧不存在

风险定义：**旧版 accepted 且新版 rejected**（如新版操作或媒体类型消失）。
旧版自身拒绝的样本不计风险；任一侧无法判定也不计风险。

覆盖的校验关键字：type、required、enum、properties、items、allOf、本地 $ref。
循环 $ref 沿引用链安全终止（重复进入的分支视为通过）；其余 JSON Schema 约束
（minimum/maximum、minLength/maxLength、pattern、format、oneOf/anyOf、
additionalProperties 等）属于范围外：只有当它们在当前实例上**实际构成约束
或可能被违反**时，才把该侧标记为无法判定，避免漏报"假通过"。

纯函数实时计算、不入库，结果只供本次查看，不改变审查结论。
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .diff import HTTP_METHODS
from .refs import PointerError, normalize_pointer, resolve_pointer

# 状态
ACCEPTED = "accepted"
REJECTED = "rejected"
UNDETERMINABLE = "undeterminable"
MISSING = "missing"
ERROR = "error"  # 仅用于样本级格式错误（不参与双侧校验）

_METHOD_SET = {m.upper() for m in HTTP_METHODS}

# 校验步骤预算，防止异常/恶意结构耗尽资源（触发后按无法判定处理）
MAX_STEPS = 20000
_MAX_DEPTH = 100


class SampleCheckError(ValueError):
    """请求体级别的文件性错误（samples 缺失 / 非数组 / 为空），映射为 HTTP 400。"""


class _BudgetExceeded(Exception):
    """校验步骤预算耗尽：当前侧按无法判定处理。"""


# ---------------------------------------------------------------------- #
# JSON 值类型
# ---------------------------------------------------------------------- #
def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    # 注意：JSON 中 1.0 解析为 float，按 JSON Schema 属于 number 而非 integer
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "unknown"


# ---------------------------------------------------------------------- #
# 操作 / 请求体 / 媒体类型查找（按相同方法 + 路径模板精确匹配）
# ---------------------------------------------------------------------- #
def _deref(doc: dict[str, Any], node: Any) -> tuple[Any, str | None]:
    """解一层本地 $ref，返回 (目标节点, 规范化指针)；非引用原样返回。"""
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        ref = node["$ref"]
        try:
            return resolve_pointer(doc, normalize_pointer(ref)), normalize_pointer(ref)
        except (PointerError, KeyError):
            return node, None
    return node, None


def _find_path_item(doc: dict[str, Any], template: str) -> Any | None:
    paths = doc.get("paths")
    if not isinstance(paths, dict) or template not in paths:
        return None
    item, _ = _deref(doc, paths[template])
    return item if isinstance(item, dict) else None


def find_operation(doc: dict[str, Any], method: str, template: str) -> Any | None:
    """按相同方法与路径模板精确查找操作对象；找不到返回 None。"""
    item = _find_path_item(doc, template)
    if item is None:
        return None
    op = item.get(method.lower())
    if not isinstance(op, dict):
        return None
    op, _ = _deref(doc, op)
    return op if isinstance(op, dict) else None


def _normalize_media_type(content_type: str) -> str:
    """application/json; charset=utf-8 → application/json（小写、去参数、去空白）。"""
    return (content_type or "").split(";", 1)[0].strip().lower()


def find_request_schema(
    doc: dict[str, Any], method: str, template: str, content_type: str
) -> tuple[str, Any]:
    """定位操作请求体中指定媒体类型的 schema。

    返回 (状态, schema)，状态：
      ok              —— 精确媒体类型命中，schema 可能为 None（未声明 schema）
      op-missing      —— 操作不存在
      body-missing    —— 操作存在但没有 requestBody
      media-missing   —— requestBody 中没有该媒体类型
    """
    op = find_operation(doc, method, template)
    if op is None:
        return "op-missing", None
    rb = op.get("requestBody")
    if rb is None:
        return "body-missing", None
    rb, _ = _deref(doc, rb)
    if not isinstance(rb, dict):
        return "body-missing", None
    content = rb.get("content")
    if not isinstance(content, dict):
        return "body-missing", None
    target = _normalize_media_type(content_type)
    if target not in {_normalize_media_type(mt) for mt in content}:
        return "media-missing", None
    # 用规范化后的键取回（媒体类型键大小写 / 参数可能与样本不同）
    media = next(
        content[mt] for mt in content
        if _normalize_media_type(mt) == target
    )
    media, _ = _deref(doc, media)
    if not isinstance(media, dict):
        return "ok", None
    schema = media.get("schema")
    return "ok", schema


# ---------------------------------------------------------------------- #
# 校验结果收集
# ---------------------------------------------------------------------- #
class _Result:
    """一次单侧校验的收集器：失败（硬违反）与无法判定项。"""

    def __init__(self) -> None:
        self.failures: list[dict[str, str]] = []
        self.unknowns: list[dict[str, str]] = []
        self.steps = 0

    def fail(self, path: str, rule: str, message: str) -> None:
        self.failures.append({"path": path, "rule": rule, "message": message})

    def unknown(self, path: str, keyword: str, message: str) -> None:
        self.unknowns.append({"path": path, "keyword": keyword, "message": message})

    def tick(self) -> None:
        self.steps += 1
        if self.steps > MAX_STEPS:
            raise _BudgetExceeded()


# ---------------------------------------------------------------------- #
# 范围外关键字：仅在它们对当前实例实际可能构成约束时记无法判定
# ---------------------------------------------------------------------- #
# 只要出现在实例可能经过的分支上即记无法判定的关键字（断言语义不在覆盖范围内）
_ALWAYS_UNKNOWN = {"$recursiveRef", "$recursiveAnchor"}
# 类型条件型：仅当实例属于相应类型时才可能构成约束
_NUMERIC_KEYWORDS = ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
                     "multipleOf")
_STRING_KEYWORDS = ("minLength", "maxLength", "pattern", "format")
_ARRAY_KEYWORDS = ("minItems", "maxItems", "uniqueItems", "contains",
                   "minContains", "maxContains", "unevaluatedItems")
_OBJECT_KEYWORDS = ("minProperties", "maxProperties", "patternProperties",
                    "additionalProperties", "unevaluatedProperties",
                    "propertyNames", "dependentSchemas", "dependencies",
                    "dependentRequired")
_COMPOSITION_KEYWORDS = ("oneOf", "anyOf", "not", "if", "then", "else")


def _enum_equal(a: Any, b: Any) -> bool:
    """JSON 意义上的相等：bool 与 int 严格区分（True ≠ 1）；int 与 float 按数值比较。"""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    return a == b


def _check_out_of_scope(schema: dict[str, Any], value: Any, path: str,
                        result: _Result) -> None:
    """检测范围外约束在当前实例上是否可能生效；可能则记无法判定。"""
    jtype = _json_type(value)

    for kw in _ALWAYS_UNKNOWN:
        if kw in schema:
            result.unknown(path, kw, f"覆盖范围外的约束 {kw}，无法判定该实例是否满足。")

    if "const" in schema:
        result.unknown(path, "const", "覆盖范围外的约束 const，无法判定。")

    if jtype in ("integer", "number"):
        for kw in _NUMERIC_KEYWORDS:
            if kw not in schema:
                continue
            limit = schema[kw]
            violated = False
            if not isinstance(limit, (int, float)) or isinstance(limit, bool):
                # draft-04 中 exclusiveMinimum/Maximum 为布尔（配合 minimum 用），
                # 语义在覆盖范围外；配合的 minimum 分支会单独标记。
                if kw in ("exclusiveMinimum", "exclusiveMaximum") and isinstance(
                    limit, bool
                ):
                    partner = "minimum" if kw == "exclusiveMinimum" else "maximum"
                    if partner in schema:
                        continue
                violated = True
            elif kw == "minimum":
                violated = value < limit
            elif kw == "maximum":
                violated = value > limit
            elif kw == "exclusiveMinimum":
                violated = value <= limit
            elif kw == "exclusiveMaximum":
                violated = value >= limit
            elif kw == "multipleOf":
                violated = (
                    limit == 0
                    or not math.isclose(float(value) % float(limit), 0.0,
                                        abs_tol=1e-9)
                )
            if violated:
                result.unknown(
                    path, kw,
                    f"范围外约束 {kw}={limit!r} 可能被当前值 {value!r} 违反，无法判定。",
                )

    if jtype == "string":
        for kw in _STRING_KEYWORDS:
            if kw not in schema:
                continue
            if kw == "format":
                # format 断言语义未知（OpenAPI 中多为注解），无法确定是否拒绝
                result.unknown(path, "format",
                               f"范围外约束 format={schema['format']!r}，无法判定。")
                continue
            limit = schema[kw]
            violated = False
            if kw == "minLength" and isinstance(limit, (int, float)):
                violated = len(value) < limit
            elif kw == "maxLength" and isinstance(limit, (int, float)):
                violated = len(value) > limit
            elif kw == "pattern":
                try:
                    violated = re.search(str(limit), value) is None
                except re.error:
                    violated = True
            if violated:
                result.unknown(
                    path, kw,
                    f"范围外约束 {kw}={limit!r} 可能被当前字符串违反，无法判定。",
                )

    if jtype == "array":
        for kw in _ARRAY_KEYWORDS:
            if kw not in schema:
                continue
            violated = False
            n = len(value)
            if kw == "minItems" and isinstance(schema[kw], (int, float)):
                violated = n < schema[kw]
            elif kw == "maxItems" and isinstance(schema[kw], (int, float)):
                violated = n > schema[kw]
            elif kw == "uniqueItems" and schema[kw] is True:
                violated = len({_hashable(v) for v in value}) < n
            elif kw == "contains":
                # contains 是否被满足需按其子 schema 逐元素判定（范围外），保守标记
                violated = True
            elif kw in ("minContains", "maxContains"):
                violated = True  # 与 contains 配套，断言语义在覆盖范围外
            if violated:
                result.unknown(
                    path, kw,
                    f"范围外约束 {kw} 可能被当前数组违反，无法判定。",
                )

    if jtype == "object":
        for kw in _OBJECT_KEYWORDS:
            if kw not in schema:
                continue
            setting = schema[kw]
            violated = False
            if kw == "minProperties" and isinstance(setting, (int, float)):
                violated = len(value) < setting
            elif kw == "maxProperties" and isinstance(setting, (int, float)):
                violated = len(value) > setting
            elif kw == "additionalProperties":
                declared = set(schema.get("properties") or {})
                extras = [k for k in value if k not in declared]
                if not extras:
                    violated = False
                elif setting is False:
                    violated = True  # false：多余字段会被拒绝
                elif isinstance(setting, dict):
                    violated = True  # schema：多余字段受范围外约束限制
                # True 或缺省：多余字段放行，不标记
            elif kw == "patternProperties":
                declared = set(schema.get("properties") or {})
                for k in value:
                    if k in declared:
                        continue
                    try:
                        if any(
                            isinstance(p, str) and re.search(p, k)
                            for p in setting
                        ):
                            violated = True
                            break
                    except re.error:
                        violated = True
                        break
            elif kw in ("unevaluatedProperties",):
                # 动态收集已计算属性，语义在覆盖范围外：有字段即保守标记
                violated = len(value) > 0
            elif kw in ("dependentSchemas", "dependencies"):
                # 仅当触发属性存在时子 schema 才生效
                triggered = [
                    dep for dep in setting
                    if isinstance(dep, str) and dep in value
                    and isinstance(setting.get(dep), dict)
                ]
                violated = bool(triggered)
            elif kw == "dependentRequired":
                triggered = [
                    dep for dep in setting
                    if isinstance(dep, str) and dep in value
                ]
                violated = bool(triggered)
            elif kw == "propertyNames":
                violated = len(value) > 0
            if violated:
                result.unknown(
                    path, kw,
                    f"范围外约束 {kw} 可能拒绝当前对象的部分字段，无法判定。",
                )

    for kw in _COMPOSITION_KEYWORDS:
        if kw in schema:
            result.unknown(
                path, kw,
                f"覆盖范围外的组合/条件约束 {kw}，无法判定当前实例是否满足。",
            )


def _hashable(v: Any) -> Any:
    if isinstance(v, dict):
        return tuple(sorted((k, _hashable(val)) for k, val in v.items()))
    if isinstance(v, list):
        return tuple(_hashable(x) for x in v)
    return v


# ---------------------------------------------------------------------- #
# 核心校验：type / required / enum / properties / items / allOf / 本地 $ref
# ---------------------------------------------------------------------- #
def _allowed_types(schema: dict[str, Any]) -> set[str]:
    """schema 声明允许的类型；无 type 时返回空集（不限制）。

    兼容 OpenAPI 3.0 的 nullable: true 与 3.1 的 type: [..., "null"]。
    """
    t = schema.get("type")
    types: set[str] = set()
    if isinstance(t, str):
        types.add(t)
    elif isinstance(t, list):
        types.update(str(x) for x in t)
    if schema.get("nullable") is True:
        types.add("null")
    return types


def _type_ok(allowed: set[str], value: Any) -> bool:
    if not allowed:
        return True
    jt = _json_type(value)
    if jt == "integer":
        return "integer" in allowed or "number" in allowed
    return jt in allowed


def _validate(
    doc: dict[str, Any],
    node: Any,
    value: Any,
    path: str,
    result: _Result,
    ref_stack: tuple[tuple[int, str], ...],
    depth: int,
) -> None:
    result.tick()
    if depth > _MAX_DEPTH:
        result.unknown(path, "$depth", "嵌套深度超出校验预算，无法判定。")
        return

    # 布尔 schema：true 任意通过；false 不允许任何实例
    if node is True:
        return
    if node is False:
        result.fail(path, "boolean-schema", "schema 为 false，不允许任何值。")
        return
    if not isinstance(node, dict):
        return

    # ---- 本地 $ref：循环引用沿引用链安全终止 ----
    # ref_stack 记录「当前数据节点 → 引用指针」链：
    #   同一数据节点上再次进入同一指针才是真正的递归（A→B→A 自引用）；
    #   同一组件在*不同*数据字段*上被复用不属于循环，仍正常校验。
    ref = node.get("$ref")
    if isinstance(ref, str):
        try:
            pointer = normalize_pointer(ref)
            target = resolve_pointer(doc, pointer)
        except (PointerError, KeyError):
            result.unknown(
                path or "$", "$ref",
                f"无法解析的引用 {ref}，该分支无法判定（不计风险）。",
            )
            # 3.1 中 $ref 的兄弟字段仍需校验
            _validate_mapping(doc, {k: v for k, v in node.items() if k != "$ref"},
                              value, path, result, ref_stack, depth)
            return
        frame = (id(value), pointer)
        siblings = {k: v for k, v in node.items() if k != "$ref"}
        if frame in ref_stack:
            # A→B→A 自引用回到同一数据节点：安全终止该分支（视为通过）。
            # 3.1 兄弟关键字仍生效（兄弟不会重新引入该 $ref）。
            if siblings:
                _validate_mapping(doc, siblings, value, path, result,
                                  ref_stack, depth)
            return
        _validate(doc, target, value, path, result,
                  ref_stack + (frame,), depth + 1)
        # OpenAPI 3.1：$ref 可与兄弟关键字并存，兄弟字段同样生效
        if siblings:
            _validate_mapping(doc, siblings, value, path, result,
                              ref_stack, depth)
        return

    _validate_mapping(doc, node, value, path, result, ref_stack, depth)


def _validate_mapping(
    doc: dict[str, Any],
    schema: dict[str, Any],
    value: Any,
    path: str,
    result: _Result,
    ref_stack: tuple[str, ...],
    depth: int,
) -> None:
    result.tick()

    allowed = _allowed_types(schema)
    type_matched = _type_ok(allowed, value)

    # ---- type（硬约束；类型不符则依赖类型的结构检查不再进行）----
    if not type_matched:
        expected = "/".join(sorted(allowed)) or "any"
        result.fail(
            path or "$", "type",
            f"类型应为 {expected}，实际为 {_json_type(value)}。",
        )
        # allOf 仍可能含独立的必填/枚举断言吗？类型已不符，继续只可能刷噪音；
        # 但 allOf 属覆盖范围，按合取语义其类型断言同样失败 → 一条足够。
        return

    # ---- enum（硬约束）----
    if isinstance(schema.get("enum"), list):
        if not any(_enum_equal(value, candidate) for candidate in schema["enum"]):
            result.fail(
                path or "$", "enum",
                f"值 {_short_repr(value)} 不在枚举允许的取值中。",
            )

    # ---- 范围外关键字（在类型匹配的前提下检测是否可能生效）----
    _check_out_of_scope(schema, value, path or "$", result)

    # ---- required（仅对对象有意义）----
    if isinstance(value, dict) and isinstance(schema.get("required"), list):
        for name in schema["required"]:
            if isinstance(name, str) and name not in value:
                result.fail(
                    _join_path(path, name), "required",
                    f"缺少必填字段 {name}。",
                )

    # ---- properties（递归校验已提供的字段；多余字段由 additionalProperties 管）----
    if isinstance(value, dict) and isinstance(schema.get("properties"), dict):
        for name, sub_schema in schema["properties"].items():
            if name in value:
                _validate(doc, sub_schema, value[name], _join_path(path, name),
                          result, ref_stack, depth + 1)

    # ---- items ----
    if isinstance(value, list) and "items" in schema:
        items = schema["items"]
        if isinstance(items, list):
            # 3.1 prefixItems 风格的数组形式 items 不在覆盖范围
            result.unknown(path or "$", "items",
                           "数组形式的 items（prefixItems 语义）不在覆盖范围，无法判定。")
        else:
            for i, element in enumerate(value):
                _validate(doc, items, element, f"{path}[{i}]" if path else f"[{i}]",
                          result, ref_stack, depth + 1)

    # ---- allOf（合取：每个分支都必须满足）----
    if isinstance(schema.get("allOf"), list):
        for i, branch in enumerate(schema["allOf"]):
            _validate(doc, branch, value, path, result,
                      ref_stack, depth + 1)


def _join_path(base: str, key: str) -> str:
    """字段路径：合法标识符用 .name，否则用 ["key"]（JSONPath 风格）。"""
    if key.isidentifier():
        token = key
    else:
        token = f"[{json.dumps(key, ensure_ascii=False)}]"
    return f"{base}.{token}" if base else token


def _short_repr(value: Any, limit: int = 60) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# ---------------------------------------------------------------------- #
# 单侧校验入口
# ---------------------------------------------------------------------- #
def validate_against(doc: dict[str, Any], schema: Any, body: Any) -> dict[str, Any]:
    """用文档内 schema 校验一个已解析的 JSON 值。

    返回 {status: accepted|rejected|undeterminable, failures, unknowns}。
    schema 为 None（媒体类型未声明 schema）时视为接受（OpenAPI 中等于无约束）。
    """
    result = _Result()
    if schema is None:
        return {"status": ACCEPTED, "failures": [], "unknowns": []}
    try:
        _validate(doc, schema, body, "$", result, (), 0)
    except _BudgetExceeded:
        result.unknown("$", "$budget",
                       "结构过于复杂，超出校验步骤预算，无法判定（不计风险）。")

    if result.failures:
        status = REJECTED
    elif result.unknowns:
        status = UNDETERMINABLE
    else:
        status = ACCEPTED
    return {
        "status": status,
        "failures": result.failures,
        "unknowns": result.unknowns,
    }


# ---------------------------------------------------------------------- #
# 样本解析
# ---------------------------------------------------------------------- #
def _parse_body(raw: Any) -> tuple[Any, str | None]:
    """把样本的 body 字段解析为 JSON 值。

    支持直接给 JSON 值（对象/数组/标量）或 JSON 文本字符串；
    返回 (解析值, 错误原因)。
    """
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, "JSON 请求体为空。"
        try:
            return json.loads(text), None
        except json.JSONDecodeError as exc:
            return None, f"JSON 请求体格式错误：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）。"
    if raw is None:
        return None, "JSON 请求体为空。"
    if isinstance(raw, (dict, list, int, float, bool)):
        return raw, None
    return None, "JSON 请求体必须是 JSON 值或 JSON 文本。"


def _check_sample(doc_old: dict[str, Any], doc_new: dict[str, Any],
                  raw: Any, index: int) -> dict[str, Any]:
    """校验单条样本，返回逐条结果（含 error 字段表示样本级错误）。"""
    base = {
        "index": index,
        "method": None,
        "path": None,
        "content_type": None,
        "error": None,
        "risk": False,
    }
    if not isinstance(raw, dict):
        base["error"] = "样本必须是包含 method、path、content_type、body 的对象。"
        return base

    method = str(raw.get("method") or "").strip().upper()
    path = str(raw.get("path") or "").strip()
    content_type = str(
        raw.get("content_type") or raw.get("contentType") or ""
    ).strip()
    base["method"] = method or None
    base["path"] = path or None
    base["content_type"] = content_type or None

    if not method:
        base["error"] = "缺少 method（旧版操作的 HTTP 方法）。"
        return base
    if method not in _METHOD_SET:
        base["error"] = f"不支持的 HTTP 方法：{raw.get('method')}。"
        return base
    if not path:
        base["error"] = "缺少 path（旧版操作的路径模板）。"
        return base
    if not path.startswith("/"):
        base["error"] = f"路径模板必须以 / 开头：{path}。"
        return base
    if not content_type:
        base["error"] = "缺少 content_type（如 application/json）。"
        return base

    body, body_error = _parse_body(raw.get("body", raw.get("request_body")))
    if body_error:
        base["error"] = body_error
        return base

    endpoint = f"{method} {path}"
    base["endpoint"] = endpoint

    # ---- 旧版侧 ----
    old_side: dict[str, Any] = {"status": None, "failures": [], "unknowns": []}
    if _find_path_item(doc_old, path) is None:
        base["error"] = f"旧版 paths 中不存在路径模板 {path}，无法定位旧版操作。"
        return base
    old_status, old_schema = find_request_schema(doc_old, method, path, content_type)
    if old_status == "op-missing":
        base["error"] = f"旧版不存在操作 {endpoint}。"
        return base
    if old_status == "body-missing":
        old_side["status"] = REJECTED
        old_side["failures"] = [{
            "path": "$", "rule": "request-body",
            "message": "旧版操作未声明 requestBody，发送请求体会被拒绝。",
        }]
    elif old_status == "media-missing":
        old_side["status"] = REJECTED
        old_side["failures"] = [{
            "path": "$", "rule": "media-type",
            "message": f"旧版请求体不接受媒体类型 {_normalize_media_type(content_type)}。",
        }]
    else:
        old_side.update(validate_against(doc_old, old_schema, body))
    base["old"] = old_side

    # ---- 新版侧：操作或媒体类型消失视为新版拒绝 ----
    new_side: dict[str, Any] = {"status": None, "failures": [], "unknowns": []}
    new_status, new_schema = find_request_schema(doc_new, method, path, content_type)
    if new_status == "op-missing":
        # 区分路径整体消失与操作消失，给出更明确的失败原因
        if _find_path_item(doc_new, path) is None:
            reason = f"新版中路径 {path} 已消失，操作 {endpoint} 不再存在。"
            rule = "path-removed"
        else:
            reason = f"新版路径 {path} 下已无 {method} 操作（操作被删除）。"
            rule = "operation-removed"
        new_side["status"] = REJECTED
        new_side["failures"] = [{"path": "$", "rule": rule, "message": reason}]
    elif new_status == "body-missing":
        new_side["status"] = REJECTED
        new_side["failures"] = [{
            "path": "$", "rule": "request-body",
            "message": "新版操作不再声明 requestBody，发送请求体会被拒绝。",
        }]
    elif new_status == "media-missing":
        new_side["status"] = REJECTED
        new_side["failures"] = [{
            "path": "$", "rule": "media-type",
            "message": (
                f"新版请求体不再接受媒体类型 {_normalize_media_type(content_type)}"
                "（媒体类型消失，将收到 415）。"
            ),
        }]
    else:
        new_side.update(validate_against(doc_new, new_schema, body))
    base["new"] = new_side

    # ---- 风险：仅旧版接受 + 新版拒绝 ----
    base["risk"] = old_side["status"] == ACCEPTED and new_side["status"] == REJECTED
    return base


# ---------------------------------------------------------------------- #
# 批量入口
# ---------------------------------------------------------------------- #
def build_sample_check(
    old_doc: dict[str, Any],
    new_doc: dict[str, Any],
    samples: Any,
) -> dict[str, Any]:
    """逐条校验请求样本，返回逐条结果与"旧版接受而新版拒绝"风险汇总。

    纯函数、无存储：结果只供本次查看，不改变审查结论。
    """
    if not isinstance(samples, list):
        raise SampleCheckError("请求体必须是样本对象数组（samples: [...]）。")
    if not samples:
        raise SampleCheckError("samples 为空：至少提交一条请求样本。")
    if len(samples) > 200:
        raise SampleCheckError("单次最多提交 200 条请求样本。")

    results = [
        _check_sample(old_doc, new_doc, raw, i)
        for i, raw in enumerate(samples, start=1)
    ]

    errors = [r for r in results if r.get("error")]
    evaluated = [r for r in results if not r.get("error")]
    risks = [r for r in evaluated if r["risk"]]
    # 其余统计互斥（一条样本只归入一个主类别，风险优先）：
    old_rejected: list[dict[str, Any]] = []
    undeterminable: list[dict[str, Any]] = []
    both_accepted: list[dict[str, Any]] = []
    other: list[dict[str, Any]] = []
    for r in evaluated:
        if r["risk"]:
            continue
        old_status = r.get("old", {}).get("status")
        new_status = r.get("new", {}).get("status")
        if old_status == REJECTED:
            old_rejected.append(r)
        elif old_status == UNDETERMINABLE or new_status == UNDETERMINABLE:
            undeterminable.append(r)
        elif old_status == ACCEPTED and new_status == ACCEPTED:
            both_accepted.append(r)
        else:
            other.append(r)

    return {
        "summary": {
            "total": len(results),
            "errors": len(errors),
            "evaluated": len(evaluated),
            "risks": len(risks),
            "old_rejected": len(old_rejected),
            "undeterminable": len(undeterminable),
            "both_accepted": len(both_accepted),
            "other": len(other),
        },
        "risks": [
            {
                "index": r["index"],
                "endpoint": r.get("endpoint"),
                "method": r.get("method"),
                "path": r.get("path"),
                "content_type": r.get("content_type"),
                "new_failures": r.get("new", {}).get("failures", []),
            }
            for r in risks
        ],
        "samples": results,
        "note": "校验结果只供本次查看，不改变原审查结论。",
    }

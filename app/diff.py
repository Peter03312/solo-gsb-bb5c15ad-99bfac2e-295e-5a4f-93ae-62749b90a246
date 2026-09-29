"""语义级 OpenAPI 兼容性规则引擎。

递归解析 $ref（带循环保护），按"请求 / 响应"两个方向应用不同规则：
  - 请求方向：客户端发送什么 → 收紧即破坏（必填变多、枚举变少、范围收窄…）
  - 响应方向：服务端返回什么 → 放宽/漂移即破坏（枚举变多、字段消失、范围扩大…）

发现级别：
  breaking      破坏性变更
  non_breaking  向后兼容的变更
  deprecation   废弃提示（仍可用，未来风险）
  info          信息性变更
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .refs import encode_token, resolve_pointer

HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

BREAKING = "breaking"
NON_BREAKING = "non_breaking"
DEPRECATION = "deprecation"
INFO = "info"


@dataclass
class Finding:
    rule_id: str
    severity: str
    category: str
    direction: str            # request | response | shared | none
    change: str               # added | removed | changed
    endpoint: str | None
    location: str             # 人类可读 JSON 路径（请求/响应载荷相对路径）
    old_json_path: str | None
    new_json_path: str | None
    old_ref: str | None
    new_ref: str | None
    message: str
    old_value: Any = None
    new_value: Any = None
    id: int | None = None


@dataclass
class SInfo:
    """扁平化（allOf 合并、$ref 解包）后的 schema 视图。"""
    types: set[str]
    fmt: str | None
    enum: list[Any] | None
    required: set[str]
    properties: dict[str, Any]
    prop_locs: dict[str, tuple[str, Any]]   # name -> (json_path 段, 节点)
    items: tuple[str, Any] | None
    minimum: float | None
    maximum: float | None
    min_length: int | None
    max_length: int | None
    min_items: int | None
    max_items: int | None
    min_props: int | None
    max_props: int | None
    unique_items: bool | None
    nullable: bool
    additional: Any          # bool / schema / None(未声明)
    compositions: list[tuple[str, list[Any]]]  # (anyOf|oneOf, schemas)
    deprecated: bool
    read_only: bool
    write_only: bool
    boolean: bool | None = None  # 布尔 schema：true=任意 / false=不允许

    def is_object_like(self) -> bool:
        return "object" in self.types or bool(self.properties) or bool(self.required)

    def is_array_like(self) -> bool:
        return "array" in self.types or self.items is not None


class Engine:
    def __init__(self, old: dict[str, Any], new: dict[str, Any]):
        self.old = old
        self.new = new
        self.findings: list[Finding] = []
        self._seq = 0

        # schema 比较的循环保护（键含比较方向，同一组件在请求/响应两侧可分别报告）：
        #   _active  —— 当前 DFS 栈上的节点对（打断环，防止无限递归）
        #   _visited —— 本次比较中已完成的节点对（防止沿循环链重复刷同一差异）
        self._active: set[tuple] = set()
        self._visited: set[tuple] = set()

        self._old_title = str(old.get("info", {}).get("title", "旧版 API"))
        self._new_title = str(new.get("info", {}).get("title", "新版 API"))

    # ------------------------------------------------------------------ #
    # 输出
    # ------------------------------------------------------------------ #
    def add(self, **kw: Any) -> None:
        self._seq += 1
        self.findings.append(Finding(id=self._seq, **kw))

    def compare(self) -> list[dict[str, Any]]:
        self._compare_roots()
        result = [f.__dict__ for f in self.findings]
        return result

    # ------------------------------------------------------------------ #
    # 解引用与扁平化
    # ------------------------------------------------------------------ #
    def _target(self, doc: dict[str, Any], node: Any) -> tuple[Any, str | None]:
        """解一层 $ref，返回 (目标节点, 指针)。"""
        if isinstance(node, dict) and isinstance(node.get("$ref"), str):
            ref = node["$ref"]
            try:
                from .refs import normalize_pointer
                pointer = normalize_pointer(ref)
                return resolve_pointer(doc, pointer), pointer
            except KeyError:
                return node, None
        return node, None

    def _flatten(self, doc: dict[str, Any], node: Any) -> SInfo:
        """递归解 $ref + 合并 allOf（含属性深度合并），合成 SInfo。"""
        info = SInfo(
            types=set(), fmt=None, enum=None, required=set(),
            properties={}, prop_locs={}, items=None,
            minimum=None, maximum=None, min_length=None, max_length=None,
            min_items=None, max_items=None, min_props=None, max_props=None,
            unique_items=None, nullable=False, additional=None,
            compositions=[], deprecated=False, read_only=False, write_only=False,
            boolean=None,
        )
        self._merge_node(doc, node, info, set())
        return info

    def _merge_node(
        self, doc: dict[str, Any], node: Any, info: SInfo, seen: set[str]
    ) -> None:
        node, _ = self._target(doc, node)

        if node is True:
            info.boolean = True
            return
        if node is False:
            info.boolean = False
            return
        if not isinstance(node, dict):
            return

        ref = node.get("$ref")
        if isinstance(ref, str):
            if ref in seen:
                return
            seen = seen | {ref}
            try:
                target = resolve_pointer(doc, ref)
            except KeyError:
                return
            self._merge_node(doc, target, info, seen)
            # $ref 的兄弟字段（3.1）也要参与
            siblings = {k: v for k, v in node.items() if k != "$ref"}
            if siblings:
                self._merge_mapping(doc, siblings, info, seen)
            return

        self._merge_mapping(doc, node, info, seen)

    def _merge_mapping(
        self, doc: dict[str, Any], node: dict[str, Any], info: SInfo, seen: set[str]
    ) -> None:
        t = node.get("type")
        if isinstance(t, str):
            info.types.add(t)
        elif isinstance(t, list):
            info.types.update(str(x) for x in t)

        for key in ("format",):
            if node.get(key):
                info.fmt = node[key]

        if isinstance(node.get("enum"), list):
            info.enum = list(node["enum"]) if info.enum is None else [
                v for v in info.enum if v in node["enum"]
            ]

        if node.get("nullable") is True:
            info.nullable = True
        if isinstance(t, list) and "null" in t:
            info.nullable = True
            info.types.discard("null")

        for req in node.get("required", []) or []:
            info.required.add(req)

        props = node.get("properties")
        if isinstance(props, dict):
            for name, schema in props.items():
                info.properties[name] = schema
                info.prop_locs[name] = (f"properties/{encode_token(name)}", schema)

        if isinstance(node.get("items"), (dict, list)):
            items = node["items"]
            if isinstance(items, list):
                items = items[0] if items else {}
            info.items = ("items", items)

        ap = node.get("additionalProperties")
        if ap is not None:
            if ap is False:
                info.additional = False
            elif isinstance(ap, dict):
                # 合并多个 additionalProperties schema（allOf 里可能多次声明）
                if isinstance(info.additional, dict):
                    info.additional = {"allOf": [info.additional, ap]}
                else:
                    info.additional = ap
            else:
                if info.additional is not False:
                    info.additional = True

        for comp in ("anyOf", "oneOf"):
            if isinstance(node.get(comp), list):
                info.compositions.append((comp, node[comp]))

        for bkey, attr in (
            ("deprecated", "deprecated"),
            ("readOnly", "read_only"),
            ("writeOnly", "write_only"),
            ("uniqueItems", "unique_items"),
        ):
            if node.get(bkey) is True:
                setattr(info, attr, True)

        for nkey, attr in (
            ("minimum", "minimum"), ("maximum", "maximum"),
            ("minLength", "min_length"), ("maxLength", "max_length"),
            ("minItems", "min_items"), ("maxItems", "max_items"),
            ("minProperties", "min_props"), ("maxProperties", "max_props"),
        ):
            if isinstance(node.get(nkey), (int, float)):
                val = node[nkey]
                cur = getattr(info, attr)
                if cur is None:
                    setattr(info, attr, val)
                else:
                    # 下界取大、上界取小（allOf 合取语义）
                    if nkey.startswith("min"):
                        setattr(info, attr, max(cur, val))
                    else:
                        setattr(info, attr, min(cur, val))

        if isinstance(node.get("allOf"), list):
            for sub in node["allOf"]:
                self._merge_node(doc, sub, info, seen)

    # ------------------------------------------------------------------ #
    # 根 / 路径 / 操作
    # ------------------------------------------------------------------ #
    def _compare_roots(self) -> None:
        ov, nv = str(self.old.get("openapi")), str(self.new.get("openapi"))
        if ov != nv:
            self.add(
                rule_id="openapi-version", severity=INFO, category="元数据",
                direction="none", change="changed", endpoint=None,
                location="openapi",
                old_json_path="/openapi", new_json_path="/openapi",
                old_ref=None, new_ref=None,
                message=f"OpenAPI 声明版本由 {ov} 变更为 {nv}。",
                old_value=ov, new_value=nv,
            )

        self._compare_servers()

        old_paths = self.old.get("paths") or {}
        new_paths = self.new.get("paths") or {}
        old_set, new_set = set(old_paths), set(new_paths)

        for p in sorted(new_set - old_set):
            methods = [m.upper() for m in HTTP_METHODS
                       if isinstance(self._safe_deref(self.new, new_paths[p]).get(m), dict)]
            self.add(
                rule_id="path-added", severity=NON_BREAKING, category="路径",
                direction="none", change="added", endpoint=p,
                location=f"paths{p}",
                old_json_path=None, new_json_path=f"/paths{encode_path(p)}",
                old_ref=None, new_ref=_ref_of(new_paths[p]),
                message=f"新增路径 {p}（{', '.join(methods) or '无操作'}），属于纯新增能力。",
            )
        for p in sorted(old_set - new_set):
            methods = [m.upper() for m in HTTP_METHODS
                       if isinstance(self._safe_deref(self.old, old_paths[p]).get(m), dict)]
            self.add(
                rule_id="path-removed", severity=BREAKING, category="路径",
                direction="none", change="removed", endpoint=p,
                location=f"paths{p}",
                old_json_path=f"/paths{encode_path(p)}", new_json_path=None,
                old_ref=_ref_of(old_paths[p]), new_ref=None,
                message=f"删除路径 {p}（{', '.join(methods) or '无操作'}），所有调用方将收到 404。",
            )

        for p in sorted(old_set & new_set):
            self._compare_path_item(p, old_paths[p], new_paths[p])

    def _compare_servers(self) -> None:
        old_urls = [s.get("url") for s in (self.old.get("servers") or []) if isinstance(s, dict)]
        new_urls = [s.get("url") for s in (self.new.get("servers") or []) if isinstance(s, dict)]
        for u in old_urls:
            if u not in new_urls:
                self.add(
                    rule_id="server-removed", severity=BREAKING, category="服务器",
                    direction="none", change="removed", endpoint=None,
                    location="servers",
                    old_json_path="/servers", new_json_path="/servers",
                    old_ref=None, new_ref=None,
                    message=f"移除服务器地址 {u}，仍在使用该地址的客户端将无法访问。",
                    old_value=u, new_value=None,
                )
        for u in new_urls:
            if u not in old_urls:
                self.add(
                    rule_id="server-added", severity=NON_BREAKING, category="服务器",
                    direction="none", change="added", endpoint=None,
                    location="servers",
                    old_json_path="/servers", new_json_path="/servers",
                    old_ref=None, new_ref=None,
                    message=f"新增服务器地址 {u}。",
                    old_value=None, new_value=u,
                )

    def _compare_path_item(self, path: str, raw_old_item: Any, raw_new_item: Any) -> None:
        old_item, _ = self._target(self.old, raw_old_item)
        new_item, _ = self._target(self.new, raw_new_item)
        if not isinstance(old_item, dict):
            old_item = {}
        if not isinstance(new_item, dict):
            new_item = {}

        old_methods = {m for m in HTTP_METHODS if isinstance(old_item.get(m), dict)}
        new_methods = {m for m in HTTP_METHODS if isinstance(new_item.get(m), dict)}

        for m in sorted(new_methods - old_methods):
            self.add(
                rule_id="operation-added", severity=NON_BREAKING, category="操作",
                direction="none", change="added", endpoint=f"{m.upper()} {path}",
                location=f"paths{path}.{m}",
                old_json_path=None,
                new_json_path=f"/paths{encode_path(path)}/{m}",
                old_ref=None, new_ref=None,
                message=f"新增操作 {m.upper()} {path}。",
            )
        for m in sorted(old_methods - new_methods):
            self.add(
                rule_id="operation-removed", severity=BREAKING, category="操作",
                direction="none", change="removed", endpoint=f"{m.upper()} {path}",
                location=f"paths{path}.{m}",
                old_json_path=f"/paths{encode_path(path)}/{m}", new_json_path=None,
                old_ref=None, new_ref=None,
                message=f"删除操作 {m.upper()} {path}，调用方将收到 404/405。",
            )

        for m in sorted(old_methods & new_methods):
            self._compare_operation(
                path, m,
                raw_old_item, raw_new_item,
                old_item[m], new_item[m],
            )

    def _compare_operation(
        self,
        path: str,
        method: str,
        old_path_item: Any,
        new_path_item: Any,
        raw_old_op: Any,
        raw_new_op: Any,
    ) -> None:
        old_op, _ = self._target(self.old, raw_old_op)
        new_op, _ = self._target(self.new, raw_new_op)
        if not isinstance(old_op, dict) or not isinstance(new_op, dict):
            return

        endpoint = f"{method.upper()} {path}"
        base = f"/paths{encode_path(path)}/{method}"

        old_dep = bool(old_op.get("deprecated"))
        new_dep = bool(new_op.get("deprecated"))
        if new_dep and not old_dep:
            self.add(
                rule_id="operation-deprecated", severity=DEPRECATION,
                category="操作", direction="none", change="changed",
                endpoint=endpoint, location=f"paths{path}.{method}.deprecated",
                old_json_path=base + "/deprecated",
                new_json_path=base + "/deprecated",
                old_ref=None, new_ref=None,
                message=f"操作 {endpoint} 已标记为 deprecated，未来版本可能移除。",
                old_value=False, new_value=True,
            )
        elif old_dep and not new_dep:
            self.add(
                rule_id="operation-un-deprecated", severity=NON_BREAKING,
                category="操作", direction="none", change="changed",
                endpoint=endpoint, location=f"paths{path}.{method}.deprecated",
                old_json_path=base + "/deprecated",
                new_json_path=base + "/deprecated",
                old_ref=None, new_ref=None,
                message=f"操作 {endpoint} 取消了 deprecated 标记。",
                old_value=True, new_value=False,
            )

        oid1, oid2 = old_op.get("operationId"), new_op.get("operationId")
        if oid1 != oid2 and oid1 is not None:
            self.add(
                rule_id="operation-id-changed", severity=INFO, category="操作",
                direction="none", change="changed", endpoint=endpoint,
                location=f"paths{path}.{method}.operationId",
                old_json_path=base + "/operationId",
                new_json_path=base + "/operationId",
                old_ref=None, new_ref=None,
                message=(
                    f"operationId 由 {oid1!r} 变为 {oid2!r}，"
                    "基于 codegen 生成的客户端可能受影响。"
                ),
                old_value=oid1, new_value=oid2,
            )

        self._compare_parameters(
            endpoint, base,
            old_path_item, new_path_item,
            old_op, new_op, path
        )
        self._compare_request_body(endpoint, base, old_op, new_op)
        self._compare_responses(endpoint, base, old_op, new_op)
        self._compare_security(
            endpoint, base, raw_old_op, old_op, raw_new_op, new_op
        )

    # ------------------------------------------------------------------ #
    # 认证要求（security）
    # ------------------------------------------------------------------ #
    def _effective_security(
        self,
        root_security: Any,
        raw_op: Any,
        op: dict[str, Any],
        base: str,
    ) -> tuple[Any, str, str]:
        """计算操作的有效 security：(原始 security, JSON Pointer, 来源)。

        - 操作级 security 存在（含 OpenAPI 3.1 中 $ref 兄弟字段）即以其为准，
          哪怕是空数组（显式允许匿名，不回落根级）；
        - 操作级缺省时继承根级 security；根级也缺省视为允许匿名。
        来源取值：operation / root / default。
        """
        if isinstance(raw_op, dict) and "security" in raw_op:
            return raw_op.get("security"), base + "/security", "operation"
        if isinstance(op, dict) and "security" in op:
            return op.get("security"), base + "/security", "operation"
        # 根级显式声明（含 security: [] 显式允许匿名）；非数组的非法值按匿名处理
        if isinstance(root_security, list):
            return root_security, "/security", "root"
        return None, base, "default"

    def _compare_security(
        self,
        endpoint: str,
        base: str,
        raw_old_op: Any,
        old_op: dict[str, Any],
        raw_new_op: Any,
        new_op: dict[str, Any],
    ) -> None:
        """比较两版同一操作的有效认证要求（仅两版都存在的操作会走到这里）。

        security 列表中的对象之间是"或"，对象内多个方案是"且"，
        OAuth scope 集合须全部满足。旧版任一可用认证组合在新版找不到
        "要求不更严"的组合即破坏；否则有效要求有变化记为兼容。
        """
        old_sec, old_ptr, old_src = self._effective_security(
            self.old.get("security"), raw_old_op, old_op, base
        )
        new_sec, new_ptr, new_src = self._effective_security(
            self.new.get("security"), raw_new_op, new_op, base
        )
        old_alts = _sec_alternatives(old_sec)
        new_alts = _sec_alternatives(new_sec)

        # 规范化后相同（[] 与 [{}]、声明位置差异等）即无要求变化
        if old_alts == new_alts:
            return

        old_value = _sec_value(old_sec, old_alts, old_src)
        new_value = _sec_value(new_sec, new_alts, new_src)

        unsupported: list[tuple] = []
        for alt in sorted(old_alts, key=_sec_combo_sort):
            if not any(_sec_combo_covers(n, alt) for n in new_alts):
                unsupported.append((alt, _sec_best_missing(alt, new_alts)))

        if unsupported:
            bits = [
                _sec_block_reason(alt, missing) for alt, missing in unsupported
            ]
            self.add(
                rule_id="security-requirements-tightened", severity=BREAKING,
                category="认证", direction="request", change="changed",
                endpoint=endpoint, location="security",
                old_json_path=old_ptr, new_json_path=new_ptr,
                old_ref=None, new_ref=None,
                message=(
                    "认证要求收紧："
                    + "；".join(bits)
                    + "。持有旧版凭据的调用方可能收到 401/403。"
                ),
                old_value=old_value, new_value=new_value,
            )
        else:
            self.add(
                rule_id="security-requirements-relaxed", severity=NON_BREAKING,
                category="认证", direction="request", change="changed",
                endpoint=endpoint, location="security",
                old_json_path=old_ptr, new_json_path=new_ptr,
                old_ref=None, new_ref=None,
                message=(
                    "有效认证要求放宽或等价改写：旧版任一可用认证组合仍可在新版"
                    "满足（含新增可选认证方式、scope 收窄、允许匿名），现有客户端不受影响。"
                ),
                old_value=old_value, new_value=new_value,
            )

    # ------------------------------------------------------------------ #
    # 参数
    # ------------------------------------------------------------------ #
    def _collect_params(
        self, doc: dict[str, Any], containers: Iterable[Any]
    ) -> dict[tuple[str, str], dict[str, Any]]:
        """合并 pathItem 级与 operation 级 parameters（同名同位置以后者为准）。"""
        merged: dict[tuple[str, str], dict[str, Any]] = {}
        for raw in containers:
            container, _ = self._target(doc, raw)
            if not isinstance(container, dict):
                continue
            for p in container.get("parameters", []) or []:
                p, _ = self._target(doc, p)
                if isinstance(p, dict) and "name" in p and "in" in p:
                    merged[(str(p["in"]), str(p["name"]))] = p
        return merged

    def _compare_parameters(
        self,
        endpoint: str,
        base: str,
        old_item: dict,
        new_item: dict,
        old_op: dict,
        new_op: dict,
        path: str,
    ) -> None:
        old_params = self._collect_params(self.old, (old_item, old_op))
        new_params = self._collect_params(self.new, (new_item, new_op))

        old_keys, new_keys = set(old_params), set(new_params)

        for key in sorted(new_keys - old_keys):
            p = new_params[key]
            required = bool(p.get("required"))
            if key[0] == "path" or required:
                self.add(
                    rule_id="parameter-required-added", severity=BREAKING,
                    category="参数", direction="request", change="added",
                    endpoint=endpoint,
                    location=f"parameters[{key[1]} in {key[0]}]",
                    old_json_path=None, new_json_path=base + "/parameters",
                    old_ref=None, new_ref=_ref_of(p),
                    message=(
                        f"新增必填{_loc_cn(key[0])}参数 '{key[1]}'，"
                        "未提供该参数的现有请求会被拒绝。"
                    ),
                    old_value=None, new_value=_param_brief(p),
                )
            else:
                self.add(
                    rule_id="parameter-optional-added", severity=NON_BREAKING,
                    category="参数", direction="request", change="added",
                    endpoint=endpoint,
                    location=f"parameters[{key[1]} in {key[0]}]",
                    old_json_path=None, new_json_path=base + "/parameters",
                    old_ref=None, new_ref=_ref_of(p),
                    message=f"新增可选{_loc_cn(key[0])}参数 '{key[1]}'。",
                    old_value=None, new_value=_param_brief(p),
                )

        for key in sorted(old_keys - new_keys):
            p = old_params[key]
            severity = BREAKING if key[0] != "header" else NON_BREAKING
            self.add(
                rule_id="parameter-removed", severity=severity,
                category="参数", direction="request", change="removed",
                endpoint=endpoint,
                location=f"parameters[{key[1]} in {key[0]}]",
                old_json_path=base + "/parameters", new_json_path=None,
                old_ref=_ref_of(p), new_ref=None,
                message=(
                    f"移除{_loc_cn(key[0])}参数 '{key[1]}'"
                    + ("（非头部参数，客户端继续发送一般无害，但契约已删除）"
                       if key[0] == "header" else "，依赖该参数的请求可能失败。")
                ),
                old_value=_param_brief(p), new_value=None,
            )

        for key in sorted(old_keys & new_keys):
            self._compare_parameter(
                endpoint, base, key,
                old_params[key], new_params[key],
                old_op.get("parameters"), new_op.get("parameters"),
            )

    def _find_raw_param_index(
        self, doc: dict[str, Any],
        raw_list: Any, key: tuple[str, str]
    ) -> int | None:
        """在原始 parameters 数组中按 (in, name) 找下标（$ref 解引用后比较）。"""
        if not isinstance(raw_list, list):
            return None
        for idx, raw in enumerate(raw_list):
            node, _ = self._target(doc, raw)
            if (
                isinstance(node, dict)
                and str(node.get("in")) == key[0]
                and str(node.get("name")) == key[1]
            ):
                return idx
        return None

    def _compare_parameter(
        self, endpoint: str, base: str, key: tuple[str, str],
        old_p: Any, new_p: Any,
        old_raw_params: Any = None, new_raw_params: Any = None,
    ) -> None:
        old_p, old_ref = self._target(self.old, old_p)
        new_p, new_ref = self._target(self.new, new_p)
        if not isinstance(old_p, dict) or not isinstance(new_p, dict):
            return
        loc = f"parameters[{key[1]} in {key[0]}]"
        common = dict(
            category="参数", direction="request", endpoint=endpoint,
            location=loc,
            old_json_path=base + "/parameters",
            new_json_path=base + "/parameters",
            old_ref=old_ref, new_ref=new_ref,
        )

        old_req, new_req = bool(old_p.get("required")), bool(new_p.get("required"))
        if new_req and not old_req:
            self.add(
                rule_id="parameter-became-required", severity=BREAKING,
                change="changed",
                message=f"{_loc_cn(key[0])}参数 '{key[1]}' 由可选变为必填，现有省略它的请求会被拒绝。",
                old_value=False, new_value=True, **common,
            )
        elif old_req and not new_req:
            self.add(
                rule_id="parameter-became-optional", severity=NON_BREAKING,
                change="changed",
                message=f"{_loc_cn(key[0])}参数 '{key[1]}' 由必填变为可选。",
                old_value=True, new_value=False, **common,
            )

        if old_p.get("style") != new_p.get("style") or (
            old_p.get("explode", False) != new_p.get("explode", False)
            and (old_p.get("explode") is not None or new_p.get("explode") is not None)
        ):
            self.add(
                rule_id="parameter-style-changed", severity=BREAKING,
                change="changed",
                message=(
                    f"参数 '{key[1]}' 的序列化方式变化（style: "
                    f"{old_p.get('style')} → {new_p.get('style')}，"
                    f"explode: {old_p.get('explode')} → {new_p.get('explode')}），"
                    "线上传输格式不再兼容。"
                ),
                old_value={"style": old_p.get("style"), "explode": old_p.get("explode")},
                new_value={"style": new_p.get("style"), "explode": new_p.get("explode")},
                **common,
            )

        if not new_p.get("deprecated", False) and old_p.get("deprecated", False):
            self.add(
                rule_id="parameter-un-deprecated", severity=NON_BREAKING,
                change="changed",
                message=f"参数 '{key[1]}' 取消了 deprecated 标记。",
                old_value=True, new_value=False, **common,
            )
        elif new_p.get("deprecated", False) and not old_p.get("deprecated", False):
            self.add(
                rule_id="parameter-deprecated", severity=DEPRECATION,
                change="changed",
                message=f"参数 '{key[1]}' 已标记为 deprecated。",
                old_value=False, new_value=True, **common,
            )

        old_schema = old_p.get("schema")
        new_schema = new_p.get("schema")
        if isinstance(old_schema, dict) and isinstance(new_schema, dict):
            ctx = Ctx(
                direction="request", endpoint=endpoint,
                base=base,
                loc=loc,
                old_ref=old_ref, new_ref=new_ref,
                category="参数",
            )
            # 内联参数：找到其在 operation.parameters 数组中的真实下标；
            # 引用参数（$ref 到 components/parameters）：锚点指向组件。
            if not old_ref:
                idx = self._find_raw_param_index(self.old, old_raw_params, key)
                suffix = ([f"parameters/{idx}/schema"] if idx is not None
                          else ["parameters/schema"])
                ctx.old_suffix = suffix
                ctx.new_suffix = suffix
            self._compare_schema(old_schema, new_schema, ctx)

    # ------------------------------------------------------------------ #
    # 请求体
    # ------------------------------------------------------------------ #
    def _compare_request_body(
        self, endpoint: str, base: str, old_op: dict, new_op: dict
    ) -> None:
        old_rb = old_op.get("requestBody")
        new_rb = new_op.get("requestBody")

        if old_rb is None and new_rb is not None:
            new_rb, new_ref = self._target(self.new, new_rb)
            required = isinstance(new_rb, dict) and bool(new_rb.get("required"))
            self.add(
                rule_id="request-body-added" if not required
                else "request-body-required-added",
                severity=NON_BREAKING if not required else BREAKING,
                category="请求体", direction="request",
                change="added", endpoint=endpoint,
                location="requestBody",
                old_json_path=None, new_json_path=base + "/requestBody",
                old_ref=None, new_ref=_ref_of(new_op.get("requestBody")) or new_ref,
                message=(
                    "新增请求体且为必填，未携带请求体的现有请求会被拒绝。"
                    if required else "新增可选请求体。"
                ),
                old_value=None,
                new_value={"required": required,
                           "mediaTypes": _media_types(new_rb)},
            )
            if isinstance(new_rb, dict):
                self._compare_content(
                    None, new_rb.get("content"), "request", endpoint,
                    base + "/requestBody", "requestBody", None,
                    _ref_of(new_op.get("requestBody")),
                )
            return

        if old_rb is not None and new_rb is None:
            self.add(
                rule_id="request-body-removed", severity=NON_BREAKING,
                category="请求体", direction="request",
                change="removed", endpoint=endpoint,
                location="requestBody",
                old_json_path=base + "/requestBody", new_json_path=None,
                old_ref=_ref_of(old_rb), new_ref=None,
                message="移除请求体定义；原有客户端继续发送通常被忽略（请确认服务端是否拒绝）。",
                old_value={"mediaTypes": _media_types(self._safe_deref(self.old, old_rb))},
                new_value=None,
            )
            return

        if old_rb is None and new_rb is None:
            return

        old_rb_d, old_ref = self._target(self.old, old_rb)
        new_rb_d, new_ref = self._target(self.new, new_rb)
        if not isinstance(old_rb_d, dict) or not isinstance(new_rb_d, dict):
            return

        old_req, new_req = bool(old_rb_d.get("required")), bool(new_rb_d.get("required"))
        if new_req and not old_req:
            self.add(
                rule_id="request-body-became-required", severity=BREAKING,
                category="请求体", direction="request", change="changed",
                endpoint=endpoint, location="requestBody.required",
                old_json_path=base + "/requestBody/required",
                new_json_path=base + "/requestBody/required",
                old_ref=old_ref, new_ref=new_ref,
                message="请求体由可选变为必填，不带请求体的现有调用会被拒绝。",
                old_value=False, new_value=True,
            )
        elif old_req and not new_req:
            self.add(
                rule_id="request-body-became-optional", severity=NON_BREAKING,
                category="请求体", direction="request", change="changed",
                endpoint=endpoint, location="requestBody.required",
                old_json_path=base + "/requestBody/required",
                new_json_path=base + "/requestBody/required",
                old_ref=old_ref, new_ref=new_ref,
                message="请求体由必填变为可选。",
                old_value=True, new_value=False,
            )

        self._compare_content(
            old_rb_d.get("content"), new_rb_d.get("content"),
            "request", endpoint, base + "/requestBody",
            "requestBody", old_ref, new_ref,
        )

    # ------------------------------------------------------------------ #
    # 响应
    # ------------------------------------------------------------------ #
    def _compare_responses(
        self, endpoint: str, base: str, old_op: dict, new_op: dict
    ) -> None:
        old_resp = old_op.get("responses")
        new_resp = new_op.get("responses")
        if not isinstance(old_resp, dict) or not isinstance(new_resp, dict):
            return

        old_codes, new_codes = set(old_resp), set(new_resp)

        for code in sorted(new_codes - old_codes):
            severity = NON_BREAKING if _is_success_code(code) or code == "default" else INFO
            self.add(
                rule_id="response-added", severity=severity,
                category="响应", direction="response", change="added",
                endpoint=endpoint, location=f"responses[{code}]",
                old_json_path=None,
                new_json_path=f"{base}/responses/{encode_token(str(code))}",
                old_ref=None, new_ref=_ref_of(new_resp[code]),
                message=f"新增响应 {code}（{'成功响应，属于兼容扩展' if severity == NON_BREAKING else '非成功/默认响应，提示性变更'}）。",
                old_value=None, new_value=str(code),
            )

        for code in sorted(old_codes - new_codes):
            self.add(
                rule_id="response-removed", severity=BREAKING,
                category="响应", direction="response", change="removed",
                endpoint=endpoint, location=f"responses[{code}]",
                old_json_path=f"{base}/responses/{encode_token(str(code))}",
                new_json_path=None,
                old_ref=_ref_of(old_resp[code]), new_ref=None,
                message=(
                    f"移除响应 {code}，依赖该状态码分支处理的客户端可能失效"
                    + ("（通常为错误码删除，风险相对较低）。"
                       if not _is_success_code(code) else "。")
                ),
                old_value=str(code), new_value=None,
            )

        for code in sorted(old_codes & new_codes):
            old_r, old_ref = self._target(self.old, old_resp[code])
            new_r, new_ref = self._target(self.new, new_resp[code])
            if not isinstance(old_r, dict) or not isinstance(new_r, dict):
                continue
            common = dict(
                category="响应", direction="response", change="changed",
                endpoint=endpoint, location=f"responses[{code}]",
                old_json_path=f"{base}/responses/{encode_token(str(code))}",
                new_json_path=f"{base}/responses/{encode_token(str(code))}",
                old_ref=old_ref, new_ref=new_ref,
            )
            if not new_r.get("deprecated", False) and old_r.get("deprecated", False):
                self.add(
                    rule_id="response-un-deprecated", severity=NON_BREAKING,
                    message=f"响应 {code} 取消 deprecated 标记。",
                    old_value=True, new_value=False, **common,
                )
            elif new_r.get("deprecated", False) and not old_r.get("deprecated", False):
                self.add(
                    rule_id="response-deprecated", severity=DEPRECATION,
                    message=f"响应 {code} 已标记 deprecated。",
                    old_value=False, new_value=True, **common,
                )

            self._compare_content(
                old_r.get("content"), new_r.get("content"),
                "response", endpoint,
                f"{base}/responses/{encode_token(str(code))}",
                f"responses[{code}]", old_ref, new_ref,
            )

    # ------------------------------------------------------------------ #
    # MediaType 容器（request / response 共用）
    # ------------------------------------------------------------------ #
    def _compare_content(
        self,
        old_content: Any,
        new_content: Any,
        direction: str,
        endpoint: str,
        base: str,
        loc: str,
        old_ref: str | None,
        new_ref: str | None,
    ) -> None:
        old_content = old_content if isinstance(old_content, dict) else {}
        new_content = new_content if isinstance(new_content, dict) else {}
        old_mts, new_mts = set(old_content), set(new_content)

        if direction == "request":
            for mt in sorted(new_mts - old_mts):
                self.add(
                    rule_id="media-type-added", severity=NON_BREAKING,
                    category="请求体" if direction == "request" else "响应",
                    direction="request", change="added", endpoint=endpoint,
                    location=f"{loc}[{mt}]",
                    old_json_path=base + f"/content/{encode_token(mt)}",
                    new_json_path=base + f"/content/{encode_token(mt)}",
                    old_ref=old_ref, new_ref=new_ref,
                    message=f"服务端新增接受媒体类型 {mt}，旧客户端不受影响。",
                    old_value=None, new_value=mt,
                )
            for mt in sorted(old_mts - new_mts):
                self.add(
                    rule_id="media-type-removed", severity=BREAKING,
                    category="请求体", direction="request", change="removed",
                    endpoint=endpoint, location=f"{loc}[{mt}]",
                    old_json_path=base + f"/content/{encode_token(mt)}",
                    new_json_path=None,
                    old_ref=old_ref, new_ref=None,
                    message=f"服务端不再接受媒体类型 {mt}，使用该类型的请求将收到 415。",
                    old_value=mt, new_value=None,
                )
        else:
            for mt in sorted(new_mts - old_mts):
                self.add(
                    rule_id="response-media-type-added", severity=BREAKING,
                    category="响应", direction="response", change="added",
                    endpoint=endpoint, location=f"{loc}[{mt}]",
                    old_json_path=None,
                    new_json_path=base + f"/content/{encode_token(mt)}",
                    old_ref=None, new_ref=new_ref,
                    message=f"响应新增媒体类型 {mt}；严格校验 Content-Type 的客户端可能无法处理。",
                    old_value=None, new_value=mt,
                )
            for mt in sorted(old_mts - new_mts):
                self.add(
                    rule_id="response-media-type-removed", severity=NON_BREAKING,
                    category="响应", direction="response", change="removed",
                    endpoint=endpoint, location=f"{loc}[{mt}]",
                    old_json_path=base + f"/content/{encode_token(mt)}",
                    new_json_path=None,
                    old_ref=old_ref, new_ref=None,
                    message=f"响应不再返回媒体类型 {mt}（旧客户端一般仍可继续协商）。",
                    old_value=mt, new_value=None,
                )

        for mt in sorted(old_mts & new_mts):
            old_schema = old_content[mt].get("schema") if isinstance(old_content[mt], dict) else None
            new_schema = new_content[mt].get("schema") if isinstance(new_content[mt], dict) else None
            if isinstance(old_schema, dict) and isinstance(new_schema, dict):
                ctx = Ctx(
                    direction=direction, endpoint=endpoint,
                    base=f"{base}/content/{encode_token(mt)}/schema",
                    loc=f"{loc} > {mt}",
                    old_ref=old_ref, new_ref=new_ref,
                    category="请求 Schema" if direction == "request" else "响应 Schema",
                )
                self._compare_schema(old_schema, new_schema, ctx)

    def _safe_deref(self, doc: dict[str, Any], node: Any) -> Any:
        node, _ = self._target(doc, node)
        return node if isinstance(node, dict) else {}

    # ------------------------------------------------------------------ #
    # Schema 递归比较（核心语义规则）
    # ------------------------------------------------------------------ #
    def _identity(self, doc_id: int, node: Any, virtual: int | None) -> int:
        if virtual is not None:
            return virtual
        return id(node)

    def _compare_schema(self, old: Any, new: Any, ctx: "Ctx") -> None:
        key = (ctx.direction, id(old), id(new))
        if key in self._active or key in self._visited:
            return  # 环回或该节点对在此方向下已报告过差异：停止递归
        self._active.add(key)
        try:
            self._compare_schema_inner(old, new, ctx)
        finally:
            self._active.discard(key)
            self._visited.add(key)

    def _compare_schema_inner(self, old: Any, new: Any, ctx: "Ctx") -> None:
        # 布尔 schema
        if old is False or new is False or old is True or new is True:
            self._compare_boolean_schema(old, new, ctx)
            return

        old_target, o_ref = self._target(self.old, old)
        new_target, n_ref = self._target(self.new, new)
        nctx = ctx.entered_ref(o_ref, n_ref)

        oi = self._flatten(self.old, old_target)
        ni = self._flatten(self.new, new_target)

        # 布尔结果（allOf: [false]、true 之类）：另一侧传 None 表示普通 schema
        if oi.boolean is not None or ni.boolean is not None:
            self._compare_boolean_schema(
                oi.boolean if oi.boolean is not None else None,
                ni.boolean if ni.boolean is not None else None,
                nctx,
            )
            if oi.boolean is False or ni.boolean is False:
                return

        self._compare_types(oi, ni, nctx)
        self._compare_format(oi, ni, nctx)
        self._compare_enum(oi, ni, nctx)
        self._compare_nullable(oi, ni, nctx)
        self._compare_numeric(oi, ni, nctx)
        self._compare_lengths(oi, ni, nctx)
        self._compare_item_counts(oi, ni, nctx)
        self._compare_compositions(oi, ni, nctx)
        self._compare_properties(oi, ni, nctx)
        self._compare_additional_properties(oi, ni, nctx)

    def _compare_boolean_schema(
        self, old: Any, new: Any, ctx: "Ctx"
    ) -> None:
        """比较布尔 schema。

        入参为 True / False / None（None 表示"普通约束 schema"，
        即介于 true=任意值 与 false=不允许任何值 之间）。
        """
        ov = True if old is True else False if old is False else None
        nv = True if new is True else False if new is False else None
        if ov == nv:
            return
        req = ctx.direction == "request"

        # 以"允许值集合"大小排序：false(0) < 普通schema(中) < true(任意)
        rank = {False: 0, None: 1, True: 2}
        loosened = rank[nv] > rank[ov]  # 新版允许的值更多

        if nv is False:
            self._emit(
                "schema-became-false", BREAKING, ctx, "changed",
                "schema 变为 false（不允许任何值）"
                + ("，请求将被拒绝。" if req else "，响应契约不再允许任何数据。"),
                old_value=ov, new_value=nv,
            )
        elif ov is False:
            self._emit(
                "schema-false-relaxed",
                NON_BREAKING if req else BREAKING, ctx, "changed",
                "schema 由 false（不允许任何值）放宽。"
                if req else "schema 由 false 放宽，响应可能出现此前不可能存在的数据。",
                old_value=ov, new_value=nv,
            )
        elif loosened:
            self._emit(
                "schema-constraints-relaxed",
                NON_BREAKING if req else BREAKING, ctx, "changed",
                "schema 约束被放宽为接受任意值（true）。"
                if req else "schema 被放宽为 true，响应可能返回任意未声明的数据。",
                old_value=ov, new_value=nv,
            )
        else:
            self._emit(
                "schema-constraints-tightened",
                BREAKING if req else NON_BREAKING, ctx, "changed",
                "schema 由接受任意值（true）收紧为具体约束。"
                if req else "响应 schema 由 true 收紧为具体约束，承诺更严格。",
                old_value=ov, new_value=nv,
            )

    def _compare_types(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        ot, nt = oi.types, ni.types
        if not ot or not nt or ot == nt:
            return
        added = nt - ot
        removed = ot - nt
        req = ctx.direction == "request"
        if added and not removed:
            # 请求：接受更多类型 = 放宽（兼容）；响应：可能返回新类型 = 破坏
            self._emit(
                "type-added",
                NON_BREAKING if req else BREAKING,
                ctx, "changed",
                f"类型联合新增 {sorted(added)}"
                + ("，服务端接受范围扩大。" if req else "，响应可能返回客户端无法解析的新类型。"),
                old_value=sorted(ot), new_value=sorted(nt),
            )
        elif removed and not added:
            self._emit(
                "type-removed",
                BREAKING if req else NON_BREAKING,
                ctx, "changed",
                f"类型联合收窄 {sorted(removed)}"
                + ("，旧客户端可能发送已不再接受的类型。" if req
                   else "，服务端承诺返回的类型变少。"),
                old_value=sorted(ot), new_value=sorted(nt),
            )
        else:
            self._emit(
                "type-changed", BREAKING, ctx, "changed",
                f"类型由 {sorted(ot)} 变为 {sorted(nt)}，序列化结构不兼容。",
                old_value=sorted(ot), new_value=sorted(nt),
            )

    def _compare_format(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        if oi.fmt == ni.fmt:
            return
        if oi.fmt is None and ni.fmt:
            # 请求：加格式约束（破坏）；响应：承诺更具体（兼容）
            self._emit(
                "format-added",
                BREAKING if ctx.direction == "request" else NON_BREAKING,
                ctx, "changed",
                f"新增 format '{ni.fmt}'"
                + ("，不符合该格式的值将被拒绝。" if ctx.direction == "request"
                   else "，响应格式承诺更严格。"),
                old_value=oi.fmt, new_value=ni.fmt,
            )
        elif oi.fmt and ni.fmt is None:
            self._emit(
                "format-removed",
                NON_BREAKING if ctx.direction == "request" else BREAKING,
                ctx, "changed",
                f"移除 format '{oi.fmt}'"
                + ("，请求校验放宽。" if ctx.direction == "request"
                   else "，响应可能不再符合该格式。"),
                old_value=oi.fmt, new_value=ni.fmt,
            )
        else:
            self._emit(
                "format-changed", BREAKING, ctx, "changed",
                f"format 由 '{oi.fmt}' 变为 '{ni.fmt}'。",
                old_value=oi.fmt, new_value=ni.fmt,
            )

    def _compare_enum(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        if oi.enum is None and ni.enum is None:
            return
        req = ctx.direction == "request"
        ov = oi.enum if oi.enum is not None else None
        nv = ni.enum if ni.enum is not None else None

        if ov is None and nv is not None:
            self._emit(
                "enum-added", BREAKING if req else NON_BREAKING, ctx, "changed",
                f"新增枚举约束，仅允许 {nv}"
                + ("，枚举外的旧请求会被拒绝。" if req
                   else "，响应值域被收紧。"),
                old_value=None, new_value=nv,
            )
            return
        if ov is not None and nv is None:
            self._emit(
                "enum-removed", NON_BREAKING if req else BREAKING, ctx, "changed",
                f"移除枚举约束（旧值域 {ov}）"
                + ("，请求校验放宽。" if req
                   else "，响应可能返回枚举外的新值，导致客户端解析失败。"),
                old_value=ov, new_value=None,
            )
            return

        old_set, new_set = set(_hashable(v) for v in ov), set(_hashable(v) for v in nv)
        added = [v for v in nv if _hashable(v) not in old_set]
        removed = [v for v in ov if _hashable(v) not in new_set]
        if not added and not removed:
            return
        if added:
            self._emit(
                "enum-value-added",
                NON_BREAKING if req else BREAKING,
                ctx, "changed",
                f"枚举新增取值 {added}"
                + ("，请求可发送的值变多。" if req
                   else "，客户端可能遇到未知枚举值。"),
                old_value=ov, new_value=nv,
            )
        if removed:
            self._emit(
                "enum-value-removed",
                BREAKING if req else NON_BREAKING,
                ctx, "changed",
                f"枚举移除取值 {removed}"
                + ("，继续发送这些值会被拒绝。" if req
                   else "，服务端承诺的值变少。"),
                old_value=ov, new_value=nv,
            )

    def _compare_nullable(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        if oi.nullable == ni.nullable:
            return
        if not oi.nullable and ni.nullable:
            # 请求：允许 null = 放宽；响应：可能返回 null = 破坏
            self._emit(
                "nullable-added",
                NON_BREAKING if ctx.direction == "request" else BREAKING,
                ctx, "changed",
                ("新增 nullable"
                 + ("，请求允许传 null。" if ctx.direction == "request"
                    else "，响应可能返回 null，未判空的客户端会出错。")),
                old_value=False, new_value=True,
            )
        else:
            self._emit(
                "nullable-removed",
                BREAKING if ctx.direction == "request" else NON_BREAKING,
                ctx, "changed",
                ("移除 nullable"
                 + ("，传 null 的旧请求会被拒绝。" if ctx.direction == "request"
                    else "，响应不再返回 null。")),
                old_value=True, new_value=False,
            )

    def _compare_numeric(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        # minimum 升高 = 收紧；maximum 降低 = 收紧
        self._bound(oi.minimum, ni.minimum, "minimum", ctx,
                    tightened_when_increased=True)
        self._bound(oi.maximum, ni.maximum, "maximum", ctx,
                    tightened_when_increased=False)

    def _bound(
        self, ov: float | None, nv: float | None, name: str,
        ctx: "Ctx", *, tightened_when_increased: bool
    ) -> None:
        """min*/max* 数值边界，语义同 _length_pair。"""
        if ov == nv:
            return

        if ov is not None and nv is not None:
            increased = nv > ov
            tightened = increased if tightened_when_increased else not increased
            word = "收窄，可能拒绝既有数据" if tightened else "放宽"
            msg = f"{name} 由 {ov} 变为 {nv}，取值范围{word}。"
            rule = f"{name}-changed"
        elif nv is not None:
            tightened = True
            msg = f"新增 {name}={nv} 约束。"
            rule = f"{name}-added"
        else:
            tightened = False
            msg = f"移除 {name} 约束（原值 {ov}）。"
            rule = f"{name}-removed"

        breaking = tightened if ctx.direction == "request" else not tightened
        self._emit(
            rule, BREAKING if breaking else NON_BREAKING, ctx, "changed", msg,
            old_value=ov, new_value=nv,
        )

    def _compare_lengths(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        req = ctx.direction == "request"
        # min*：值变大 = 收紧（请求破坏）；max*：值变小 = 收紧（响应破坏）
        self._length_pair(oi.min_length, ni.min_length, "minLength", ctx,
                          tightened_when_increased=True)
        self._length_pair(oi.max_length, ni.max_length, "maxLength", ctx,
                          tightened_when_increased=False)

    def _length_pair(
        self, ov: int | None, nv: int | None, name: str, ctx: "Ctx",
        *, tightened_when_increased: bool
    ) -> None:
        """min*/max* 约束比较。

        tightened_when_increased=True 表示值变大时约束收紧（min* 类），
        False 表示值变小时收紧（max* 类）。
        请求方向收紧即破坏；响应方向放宽（新增/移除以"漂移"看）按规则判定。
        """
        if ov == nv:
            return

        if ov is not None and nv is not None:
            increased = nv > ov
            tightened = increased if tightened_when_increased else not increased
            word = "收窄，可能拒绝既有数据" if tightened else "放宽"
            msg = f"{name} 由 {ov} 变为 {nv}，取值范围{word}。"
            rule = f"{name}-changed"
        elif nv is not None:
            # 新增约束恒为收紧
            tightened = True
            msg = f"新增 {name}={nv} 约束。"
            rule = f"{name}-added"
        else:
            # 移除约束恒为放宽
            tightened = False
            msg = f"移除 {name} 约束（原值 {ov}）。"
            rule = f"{name}-removed"

        # 请求：收紧破坏；响应：放宽破坏（承诺可能漂移）
        breaking = tightened if ctx.direction == "request" else not tightened
        self._emit(
            rule, BREAKING if breaking else NON_BREAKING, ctx, "changed", msg,
            old_value=ov, new_value=nv,
        )

    def _compare_item_counts(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        self._length_pair(oi.min_items, ni.min_items, "minItems", ctx,
                          tightened_when_increased=True)
        self._length_pair(oi.max_items, ni.max_items, "maxItems", ctx,
                          tightened_when_increased=False)
        self._length_pair(oi.min_props, ni.min_props, "minProperties", ctx,
                          tightened_when_increased=True)
        self._length_pair(oi.max_props, ni.max_props, "maxProperties", ctx,
                          tightened_when_increased=False)

        if oi.unique_items != ni.unique_items and ni.unique_items is True:
            self._emit(
                "unique-items-added",
                BREAKING if req else NON_BREAKING, ctx, "changed",
                "新增 uniqueItems: true，数组元素必须唯一。",
                old_value=oi.unique_items, new_value=ni.unique_items,
            )
        elif oi.unique_items is True and not ni.unique_items:
            self._emit(
                "unique-items-removed",
                NON_BREAKING if req else BREAKING, ctx, "changed",
                "移除 uniqueItems 约束。",
                old_value=oi.unique_items, new_value=ni.unique_items,
            )

        # items schema 递归
        if oi.items and ni.items and oi.is_array_like() and ni.is_array_like():
            _, old_items_node = oi.items
            _, new_items_node = ni.items
            self._compare_schema(
                old_items_node, new_items_node,
                ctx.child("[*]", "items"),
            )

    def _compare_additional_properties(
        self, oi: SInfo, ni: SInfo, ctx: "Ctx"
    ) -> None:
        if oi.additional is None and ni.additional is None:
            return
        req = ctx.direction == "request"
        if oi.additional is not False and ni.additional is False:
            self._emit(
                "additional-properties-set-false",
                BREAKING if req else NON_BREAKING, ctx, "changed",
                "additionalProperties 变为 false"
                + ("，携带额外字段的旧请求会被拒绝。" if req
                   else "，响应不再允许扩展字段（客户端侧通常兼容）。"),
                old_value=_ap_value(oi.additional),
                new_value=False,
            )
        elif oi.additional is False and ni.additional is not False:
            self._emit(
                "additional-properties-relaxed",
                NON_BREAKING if req else BREAKING, ctx, "changed",
                "additionalProperties 不再为 false"
                + ("，请求允许扩展字段。" if req
                   else "，响应可能出现未知的额外字段。"),
                old_value=False,
                new_value=_ap_value(ni.additional),
            )

    def _compare_compositions(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        """anyOf / oneOf：按"扁平化后的签名集合"做双向包含检查。"""
        req = ctx.direction == "request"

        def branches(info: SInfo) -> list[SInfo]:
            out: list[SInfo] = []
            for _, schemas in info.compositions:
                for s in schemas:
                    out.append(self._flatten(
                        self.old if info is oi else self.new, s
                    ))
            return out

        old_branches = branches(oi)
        new_branches = branches(ni)
        if not old_branches and not new_branches:
            return

        def sig(b: SInfo) -> tuple:
            return (
                tuple(sorted(b.types)), b.fmt,
                tuple(sorted(b.required)),
                tuple(sorted(b.properties)),
            )

        old_sigs = {sig(b) for b in old_branches}
        new_sigs = {sig(b) for b in new_branches}

        if not old_branches and new_branches:
            self._emit(
                "composition-added", BREAKING if req else NON_BREAKING, ctx,
                "changed",
                f"新增 oneOf/anyOf 约束（{len(new_branches)} 个分支）。",
                old_value=None, new_value=len(new_branches),
            )
            return
        if old_branches and not new_branches:
            self._emit(
                "composition-removed", NON_BREAKING if req else BREAKING, ctx,
                "changed",
                f"移除 oneOf/anyOf 约束（原 {len(old_branches)} 个分支）。",
                old_value=len(old_branches), new_value=None,
            )
            return

        added = new_sigs - old_sigs
        removed = old_sigs - new_sigs
        if added:
            self._emit(
                "composition-branch-added",
                NON_BREAKING if req else BREAKING, ctx, "changed",
                f"oneOf/anyOf 新增 {len(added)} 个分支"
                + ("，可接受的请求形态变多。" if req
                   else "，响应可能出现客户端未处理的形态。"),
                old_value=len(old_branches), new_value=len(new_branches),
            )
        if removed:
            self._emit(
                "composition-branch-removed",
                BREAKING if req else NON_BREAKING, ctx, "changed",
                f"oneOf/anyOf 移除 {len(removed)} 个分支"
                + ("，旧请求可能不再匹配任何分支。" if req
                   else "，服务端承诺的形态变少。"),
                old_value=len(old_branches), new_value=len(new_branches),
            )

        # 分支内部 schema 递归：取第一组组合（anyOf 或 oneOf），按下标配对尽力比较
        old_group = oi.compositions[0] if oi.compositions else None
        new_group = ni.compositions[0] if ni.compositions else None
        if old_group and new_group and old_group[0] == new_group[0]:
            for i in range(min(len(old_group[1]), len(new_group[1]))):
                self._compare_schema(
                    old_group[1][i], new_group[1][i],
                    ctx.child(f"{old_group[0]}[{i}]", f"{old_group[0]}/{i}"),
                )

    # ------------------------------------------------------------------ #
    # 对象属性
    # ------------------------------------------------------------------ #
    def _compare_properties(self, oi: SInfo, ni: SInfo, ctx: "Ctx") -> None:
        req = ctx.direction == "request"
        old_names, new_names = set(oi.properties), set(ni.properties)

        for name in sorted(old_names - new_names):
            child_info = self._flatten(self.old, oi.properties[name])
            if req:
                # 客户端原本可发的字段被删：非破坏（服务端忽略多余字段是常态）
                sev = NON_BREAKING
                rule = "property-removed-request"
                msg = f"请求字段 '{name}' 从契约中移除，服务端可能开始忽略或拒绝该字段。"
            else:
                if child_info.write_only:
                    # 响应本来就不该出现 writeOnly 字段
                    sev = NON_BREAKING
                    rule = "property-removed-writeonly"
                    msg = f"响应中的只写字段 '{name}' 被移除（writeOnly 字段本不应出现在响应中）。"
                else:
                    sev = BREAKING
                    rule = "property-removed"
                    msg = f"响应字段 '{name}' 被删除，读取该字段的客户端将得到 undefined。"
            self._emit(
                rule, sev, ctx.child(name, oi.prop_locs[name][0]),
                "removed", msg,
                old_value=_schema_brief(child_info), new_value=None,
            )

        for name in sorted(new_names - old_names):
            child_info = self._flatten(self.new, ni.properties[name])
            is_required = name in ni.required
            if req:
                if child_info.read_only:
                    sev, rule = NON_BREAKING, "property-added-readonly"
                    msg = f"请求新增只读字段 '{name}'（readOnly 字段不应出现在请求中，实际影响小）。"
                elif is_required:
                    sev, rule = BREAKING, "required-property-added"
                    msg = f"请求新增必填字段 '{name}'，不含该字段的旧请求会被拒绝。"
                else:
                    sev, rule = NON_BREAKING, "optional-property-added"
                    msg = f"请求新增可选字段 '{name}'。"
            else:
                if child_info.write_only:
                    sev, rule = NON_BREAKING, "property-added-writeonly"
                    msg = f"响应新增只写字段 '{name}'（writeOnly 字段本不应出现在响应中）。"
                elif is_required:
                    # 响应保证新增必填字段：好事（非破坏）
                    sev, rule = NON_BREAKING, "required-response-property-added"
                    msg = f"响应新增必有字段 '{name}'，客户端可使用更多数据。"
                else:
                    # 响应新增可选字段：大多数客户端容忍，但严格模型会报错
                    sev, rule = NON_BREAKING, "optional-response-property-added"
                    msg = f"响应新增可选字段 '{name}'（严格反序列化的客户端需确认是否容忍未知字段）。"
            self._emit(
                rule, sev, ctx.child(name, ni.prop_locs[name][0]),
                "added", msg,
                old_value=None, new_value=_schema_brief(child_info),
            )

        for name in sorted(old_names & new_names):
            old_node = oi.properties[name]
            new_node = ni.properties[name]
            old_ci = self._flatten(self.old, old_node)
            new_ci = self._flatten(self.new, new_node)
            child_ctx = ctx.child(
                name,
                ni.prop_locs.get(name, oi.prop_locs.get(name, ("properties/" + encode_token(name), None)))[0],
            )

            # 必填性变化
            was_req = name in oi.required
            now_req = name in ni.required
            if now_req and not was_req:
                self._emit(
                    "property-became-required",
                    BREAKING if req else NON_BREAKING,
                    child_ctx, "changed",
                    f"字段 '{name}' 变为必填"
                    + ("，旧请求若缺失会被拒绝。" if req
                       else "，响应保证该字段存在，属于增强。"),
                    old_value=False, new_value=True,
                )
            elif was_req and not now_req:
                self._emit(
                    "property-became-optional",
                    NON_BREAKING if req else BREAKING,
                    child_ctx, "changed",
                    f"字段 '{name}' 由必填变为可选"
                    + ("，请求约束放宽。" if req
                       else "，响应中该字段可能缺失，客户端需要处理 undefined。"),
                    old_value=True, new_value=False,
                )

            # readOnly / writeOnly 翻转
            self._visibility_flip(name, old_ci, new_ci, child_ctx)

            # deprecated
            if new_ci.deprecated and not old_ci.deprecated:
                self._emit(
                    "property-deprecated", DEPRECATION, child_ctx, "changed",
                    f"字段 '{name}' 已标记 deprecated。",
                    old_value=False, new_value=True,
                )
            elif old_ci.deprecated and not new_ci.deprecated:
                self._emit(
                    "property-un-deprecated", NON_BREAKING, child_ctx, "changed",
                    f"字段 '{name}' 取消 deprecated 标记。",
                    old_value=True, new_value=False,
                )

            self._compare_schema(old_node, new_node, child_ctx)

    def _visibility_flip(
        self, name: str, old_ci: SInfo, new_ci: SInfo, ctx: "Ctx"
    ) -> None:
        req = ctx.direction == "request"
        if old_ci.read_only != new_ci.read_only and new_ci.read_only:
            self._emit(
                "property-became-readonly",
                NON_BREAKING if not req else BREAKING, ctx, "changed",
                f"字段 '{name}' 变为 readOnly"
                + ("，请求中发送该字段将被忽略/拒绝。" if req
                   else "（响应方向）无负面影响。"),
                old_value=old_ci.read_only, new_value=True,
            )
        elif old_ci.read_only != new_ci.read_only and old_ci.read_only:
            self._emit(
                "property-became-writable",
                NON_BREAKING, ctx, "changed",
                f"字段 '{name}' 不再是 readOnly，请求中可以写入。",
                old_value=True, new_value=new_ci.read_only,
            )
        if old_ci.write_only != new_ci.write_only and new_ci.write_only:
            self._emit(
                "property-became-writeonly",
                BREAKING if not req else NON_BREAKING, ctx, "changed",
                f"字段 '{name}' 变为 writeOnly"
                + ("，响应中将不再返回该字段。" if not req
                   else "（请求方向）无负面影响。"),
                old_value=old_ci.write_only, new_value=True,
            )
        elif old_ci.write_only != new_ci.write_only and old_ci.write_only:
            self._emit(
                "property-became-readable",
                NON_BREAKING, ctx, "changed",
                f"字段 '{name}' 不再是 writeOnly，响应中可能返回。",
                old_value=True, new_value=new_ci.write_only,
            )

    # ------------------------------------------------------------------ #
    # Finding 构造
    # ------------------------------------------------------------------ #
    def _emit(
        self,
        rule_id: str,
        severity: str,
        ctx: "Ctx",
        change: str,
        message: str,
        *,
        old_value: Any = None,
        new_value: Any = None,
    ) -> None:
        loc_path = ctx.path_display()
        self.add(
            rule_id=rule_id, severity=severity, category=ctx.category,
            direction=ctx.direction, change=change,
            endpoint=ctx.endpoint, location=loc_path,
            old_json_path=ctx.old_json_path(),
            new_json_path=ctx.new_json_path(),
            old_ref=ctx.old_ref, new_ref=ctx.new_ref,
            message=message, old_value=old_value, new_value=new_value,
        )


@dataclass
class Ctx:
    """schema 比较上下文：维护载荷内路径、$ref 锚点、方向。"""
    direction: str
    endpoint: str | None
    base: str                       # 文档中的基础 JSON path
    loc: str                        # 人类可读定位（如 requestBody > application/json）
    old_ref: str | None = None
    new_ref: str | None = None
    category: str = "Schema"
    segments: list[str] = field(default_factory=list)   # 载荷内路径段（显示用）
    old_suffix: list[str] = field(default_factory=list)
    new_suffix: list[str] = field(default_factory=list)

    def child(self, display: str, suffix: str | None) -> "Ctx":
        suf = suffix or display
        return Ctx(
            direction=self.direction, endpoint=self.endpoint,
            base=self.base, loc=self.loc,
            old_ref=self.old_ref, new_ref=self.new_ref,
            category=self.category,
            segments=self.segments + [display],
            old_suffix=self.old_suffix + [suf],
            new_suffix=self.new_suffix + [suf],
        )

    def entered_ref(self, old_ref: str | None, new_ref: str | None) -> "Ctx":
        """进入 $ref 目标。

        锚点（old_ref/new_ref）更新为目标指针；由于 JSON path 从目标位置
        重新计数，对"新进入 ref"的一侧把后缀清空，避免把调用方的路径段
        拼到被引用组件的内部路径上。
        """
        old_changed = old_ref is not None and old_ref != self.old_ref
        new_changed = new_ref is not None and new_ref != self.new_ref
        return Ctx(
            direction=self.direction, endpoint=self.endpoint,
            base=self.base, loc=self.loc,
            old_ref=old_ref or self.old_ref,
            new_ref=new_ref or self.new_ref,
            category=self.category,
            segments=list(self.segments),
            old_suffix=[] if old_changed else list(self.old_suffix),
            new_suffix=[] if new_changed else list(self.new_suffix),
        )

    def path_display(self) -> str:
        root = "请求参数值" if self.direction == "request" and self.category == "参数" else (
            "请求体" if self.direction == "request" else "响应"
        )
        if self.segments:
            return f"{self.loc} → {root}.{'.'.join(self.segments)}"
        return f"{self.loc} → {root}"

    def _suffix(self, parts: list[str]) -> str:
        out = ""
        for p in parts:
            if p.startswith("["):
                out += p
            else:
                out += "/" + p.replace("/", "~1")
        return out

    def old_json_path(self) -> str:
        anchor = self.old_ref if self.old_ref else self.base
        return anchor + self._suffix(self.old_suffix)

    def new_json_path(self) -> str:
        anchor = self.new_ref if self.new_ref else self.base
        return anchor + self._suffix(self.new_suffix)


# ---------------------------------------------------------------------- #
# 小工具
# ---------------------------------------------------------------------- #
def encode_path(path: str) -> str:
    return "/" + "/".join(encode_token(seg) for seg in path.strip("/").split("/"))


def _ref_of(node: Any) -> str | None:
    if isinstance(node, dict) and isinstance(node.get("$ref"), str):
        return node["$ref"]
    return None


def _loc_cn(loc: str) -> str:
    return {"query": "查询", "header": "请求头", "path": "路径", "cookie": "Cookie"}.get(
        loc, loc
    )


def _is_success_code(code: str) -> bool:
    try:
        return 200 <= int(code) < 300
    except (TypeError, ValueError):
        return code == "default"


def _media_types(rb: Any) -> list[str]:
    if isinstance(rb, dict) and isinstance(rb.get("content"), dict):
        return list(rb["content"])
    return []


# ---------------------------------------------------------------------- #
# 认证要求（security）
# ---------------------------------------------------------------------- #
def _sec_combo(req: Any) -> frozenset:
    """单个 Security Requirement 对象 → {(scheme, frozenset(scopes))}。

    对象内多个方案是"且"；scope 列表按集合处理（全部满足即可）。
    空对象 {} 表示无需认证，对应空集合。
    """
    if not isinstance(req, dict):
        return frozenset()
    items: set[tuple[str, frozenset[str]]] = set()
    for name, scopes in req.items():
        scope_set = (
            {str(s) for s in scopes} if isinstance(scopes, list) else set()
        )
        items.add((str(name), frozenset(scope_set)))
    return frozenset(items)


def _sec_alternatives(security: Any) -> frozenset[frozenset]:
    """有效 security → 可用认证组合集合（或关系去重后的 frozenset）。

    - security 缺省（None）、[]、[{}] 均允许匿名：统一表示为 {空组合}；
    - 列表中每个对象是一个"或"分支；分支内方案为"且"。
    """
    if not isinstance(security, list):
        return frozenset({frozenset()})
    alts = {_sec_combo(req) for req in security if isinstance(req, dict)}
    return alts if alts else frozenset({frozenset()})


def _sec_combo_covers(candidate: frozenset, required: frozenset) -> bool:
    """新版组合 candidate 是否接受满足旧版组合 required 的客户端（要求不更严）。

    满足旧版组合的客户端持有 required 中的全部方案与 scope；它能通过新版
    组合当且仅当新版要求的每个方案（及其全部 scope）都在其持有范围内：
    - candidate 为空（允许匿名）时接受任意客户端；
    - 否则 candidate 中每个（同名）方案都要在 required 中，
      且 candidate 要求的 OAuth scope 被 required 授予的 scope 覆盖；
      旧客户端持有 candidate 之外的多余方案不影响通过（新版要求更少即放宽）。
    """
    if not candidate:
        return True
    held_by_name = {name: scopes for name, scopes in required}
    for name, need_scopes in candidate:
        held = held_by_name.get(name)
        if held is None:
            return False
        if not need_scopes <= held:
            return False
    return True


def _sec_missing(candidate: frozenset, required: frozenset) -> list[tuple[str, frozenset[str]]]:
    """满足旧版 required 的客户端通过新版 candidate 仍缺少的认证要素。

    返回 (scheme, 缺少 scope 集合) 列表，空列表表示可被满足；
    缺少 scope 集合为空表示整个方案缺失，非空表示方案具备但 scope 不足。
    """
    held_by_name = {name: scopes for name, scopes in required}
    missing: list[tuple[str, frozenset[str]]] = []
    for name, need_scopes in sorted(candidate, key=lambda x: x[0]):
        held = held_by_name.get(name)
        if held is None:
            missing.append((name, frozenset()))
        elif not need_scopes <= held:
            missing.append((name, need_scopes - held))
    return missing


def _sec_best_missing(required: frozenset, alts: frozenset) -> list[tuple[str, frozenset[str]]]:
    """在新版组合中挑旧客户端最接近满足的一个，给出其缺口（用于可读原因）。

    优先选包含最多同名方案的组合，避免方案整缺时却报告另一个方案的 scope。
    """
    best: tuple | None = None  # (同名方案数, 缺口)
    for cand in alts:
        if not cand:
            return []  # 新版允许匿名：必然接受任意旧客户端
        missing = _sec_missing(cand, required)
        same_names = len({n for n, _ in cand} & {n for n, _ in required})
        rank = (same_names, -len(missing), -sum(len(s) for _, s in missing))
        if best is None or rank > best[0]:
            best = (rank, missing)
    return best[1] if best is not None else []


def _sec_combo_sort(combo: frozenset) -> tuple:
    return tuple(sorted(
        (name, tuple(sorted(scopes))) for name, scopes in combo
    ))


def _sec_combo_text(combo: frozenset) -> str:
    if not combo:
        return "匿名（无需认证）"
    parts = []
    for name, scopes in sorted(combo, key=lambda x: x[0]):
        parts.append(name + (f"[{', '.join(sorted(scopes))}]" if scopes else ""))
    return " + ".join(parts)


def _sec_block_reason(
    combo: frozenset, missing: list[tuple[str, frozenset[str]]]
) -> str:
    if not missing:
        return f"旧版可用组合「{_sec_combo_text(combo)}」不再被满足"
    need = []
    for name, scopes in missing:
        if scopes:
            need.append(f"{name} 的 scope {sorted(scopes)}")
        else:
            need.append(f"{name} 方案")
    return f"旧版组合「{_sec_combo_text(combo)}」缺少 {'、'.join(need)}"


def _sec_combo_json(combo: frozenset) -> dict[str, list[str]]:
    return {
        name: sorted(scopes)
        for name, scopes in sorted(combo, key=lambda x: x[0])
    }


def _sec_value(security: Any, alts: frozenset[frozenset], source: str) -> dict[str, Any]:
    """发现项的旧值 / 新值：规范化后的有效要求 + 生效来源。"""
    return {
        "source": source,  # operation=操作级覆盖 | root=继承根级 | default=缺省匿名
        "raw": security,
        "alternatives": [_sec_combo_json(c) for c in sorted(alts, key=_sec_combo_sort)],
    }


def _param_brief(p: dict) -> dict:
    return {
        "name": p.get("name"),
        "in": p.get("in"),
        "required": bool(p.get("required")),
        "deprecated": bool(p.get("deprecated")),
    }


def _schema_brief(info: SInfo) -> dict:
    brief: dict[str, Any] = {}
    if info.types:
        brief["type"] = sorted(info.types)
    if info.fmt:
        brief["format"] = info.fmt
    if info.enum is not None:
        brief["enum"] = info.enum
    if info.properties:
        brief["properties"] = list(info.properties)
    return brief


def _ap_value(v: Any) -> Any:
    if isinstance(v, dict):
        return "<schema>"
    return v


def _hashable(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return ("__list__", tuple(_hashable(x) for x in v))
    if isinstance(v, dict):
        return ("__dict__", tuple(sorted((k, _hashable(val)) for k, val in v.items())))
    return (type(v).__name__, v)


# ---------------------------------------------------------------------- #
# 入口
# ---------------------------------------------------------------------- #
def run_diff(old_doc: dict[str, Any], new_doc: dict[str, Any]) -> list[dict[str, Any]]:
    engine = Engine(old_doc, new_doc)
    return engine.compare()


def summarize(findings: list[dict[str, Any]]) -> dict[str, Any]:
    counts = {BREAKING: 0, NON_BREAKING: 0, DEPRECATION: 0, INFO: 0}
    by_category: dict[str, int] = {}
    by_direction: dict[str, int] = {}
    for f in findings:
        counts[f["severity"]] = counts.get(f["severity"], 0) + 1
        by_category[f["category"]] = by_category.get(f["category"], 0) + 1
        by_direction[f["direction"]] = by_direction.get(f["direction"], 0) + 1
    return {
        "total": len(findings),
        "counts": counts,
        "by_category": dict(sorted(by_category.items())),
        "by_direction": by_direction,
        "compatible": counts[BREAKING] == 0,
    }

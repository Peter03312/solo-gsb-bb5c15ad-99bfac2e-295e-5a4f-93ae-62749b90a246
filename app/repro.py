"""最小复现包导出：按发现项裁剪新旧文档、复核可复现性并打包 ZIP。

流程：
1. 解析发现项端点，确定需要保留的 path / operation；
2. 从旧、新文档分别裁剪：文档头（openapi / info，服务器类发现含 servers）
   + 目标 path item（操作级发现只保留目标 operation 与 path 级 parameters）；
3. 以裁剪结果为根做本地 $ref 闭包：把传递引用到的 components 复制进最小文档，
   visited 集合保证去重、循环引用安全终止；未被引用的路径与组件自然剔除；
4. 用现有差异引擎 run_diff 复核目标发现的 rule_id / 方向 / 端点 / 语义定位
   仍能复现，否则抛 ReproError，拒绝提供失真的包；
5. 打包 old.yaml / new.yaml / manifest.json（含原审查 ID 与发现指纹）。
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from typing import Any

import yaml

from .diff import run_diff
from .refs import PointerError, decode_token, normalize_pointer, resolve_pointer

_HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")

# manifest 中声明的复现包格式版本
REPRO_FORMAT = "openapi-repro/1"


class ReproError(Exception):
    """无法生成可复现的最小复现包（面向用户的可读原因）。"""


# ---------------------------------------------------------------------- #
# 发现项定位与指纹
# ---------------------------------------------------------------------- #
def finding_fingerprint(finding: dict[str, Any]) -> str:
    """发现项的稳定指纹：语义身份字段的 SHA-256 前 16 位十六进制。"""
    basis = {
        "rule_id": finding.get("rule_id"),
        "severity": finding.get("severity"),
        "category": finding.get("category"),
        "direction": finding.get("direction"),
        "change": finding.get("change"),
        "endpoint": finding.get("endpoint"),
        "location": finding.get("location"),
    }
    raw = json.dumps(basis, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def find_finding(
    findings: list[dict[str, Any]], finding_id: int
) -> dict[str, Any] | None:
    """按引擎序号 id 查找发现项；历史数据若无 id 则按 1 起始位置回退。"""
    has_ids = any(isinstance(f.get("id"), int) for f in findings)
    for f in findings:
        if f.get("id") == finding_id:
            return f
    if not has_ids and 1 <= finding_id <= len(findings):
        return findings[finding_id - 1]
    return None


# ---------------------------------------------------------------------- #
# 文档裁剪
# ---------------------------------------------------------------------- #
def _parse_endpoint(endpoint: str | None) -> tuple[str | None, str | None]:
    """'GET /pets/{id}' → ('get', '/pets/{id}')；'/pets' → (None, '/pets')。"""
    if not endpoint:
        return None, None
    head, _, rest = endpoint.partition(" ")
    if rest and head.lower() in _HTTP_METHODS:
        return head.lower(), rest.strip()
    if endpoint.startswith("/"):
        return None, endpoint
    return None, None


def _needs_servers(finding: dict[str, Any]) -> bool:
    """服务器类发现需要保留 servers 数组才能复现。"""
    rule = str(finding.get("rule_id") or "")
    if rule.startswith("server"):
        return True
    return any(
        str(finding.get(k) or "").startswith("/servers")
        for k in ("old_json_path", "new_json_path")
    )


def _needs_security(finding: dict[str, Any]) -> bool:
    """认证类发现可能继承根级 security，最小文档须保留该字段才能复现。"""
    rule = str(finding.get("rule_id") or "")
    if rule.startswith("security"):
        return True
    return any(
        str(finding.get(k) or "") == "/security"
        for k in ("old_json_path", "new_json_path")
    )


def _trim_path_item(item: Any, method: str | None) -> Any:
    """裁剪单个 path item。

    - 路径级发现（method=None）：整个 path item 都属于发现现场，原样保留；
    - 引用型 path item：保留 $ref，由闭包把目标组件复制进来；
    - 操作级发现：只保留目标 operation 与 path 级 parameters
      （引擎会把 path 级参数合并进操作比较，必须保留才能原样复现）。
    """
    if not isinstance(item, dict):
        return copy.deepcopy(item)
    if method is None or isinstance(item.get("$ref"), str):
        return copy.deepcopy(item)
    out: dict[str, Any] = {}
    if isinstance(item.get("parameters"), list):
        out["parameters"] = copy.deepcopy(item["parameters"])
    if isinstance(item.get(method), dict):
        out[method] = copy.deepcopy(item[method])
    return out


def _minimal_doc(
    doc: dict[str, Any],
    path: str | None,
    method: str | None,
    keep_servers: bool,
    keep_security: bool = False,
) -> dict[str, Any]:
    """从完整文档裁剪出最小骨架：文档头 + 目标 path（可能为空 paths）。"""
    out: dict[str, Any] = {}
    for key in ("openapi", "info", "jsonSchemaDialect"):
        if key in doc:
            out[key] = copy.deepcopy(doc[key])
    if keep_servers and isinstance(doc.get("servers"), list):
        out["servers"] = copy.deepcopy(doc["servers"])
    if keep_security and isinstance(doc.get("security"), list):
        out["security"] = copy.deepcopy(doc["security"])
    paths = doc.get("paths") if isinstance(doc.get("paths"), dict) else {}
    kept: dict[str, Any] = {}
    if path is not None and path in paths:
        # 该侧没有此路径时不补空壳：路径缺失本身就是差异现场（如 path-removed）
        kept[path] = _trim_path_item(paths[path], method)
    out["paths"] = kept
    return out


# ---------------------------------------------------------------------- #
# 本地 $ref 闭包
# ---------------------------------------------------------------------- #
def _collect_refs(node: Any, out: list[str]) -> None:
    """收集任意结构里的所有 $ref 字符串。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str):
            out.append(ref)
        for v in node.values():
            _collect_refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _collect_refs(v, out)


def _set_pointer(doc: dict[str, Any], pointer: str, value: Any) -> None:
    """把 value 写入 doc 的 pointer 位置，按需创建中间容器（dict / list）。"""
    tokens = [decode_token(t) for t in pointer.lstrip("/").split("/")]
    cur: Any = doc
    for i, tok in enumerate(tokens[:-1]):
        want_list = tokens[i + 1].isdigit()
        if isinstance(cur, dict):
            nxt = cur.get(tok)
            if not isinstance(nxt, (dict, list)):
                nxt = [] if want_list else {}
                cur[tok] = nxt
            cur = nxt
        elif isinstance(cur, list):
            if not tok.isdigit():
                return  # 异常指针：放弃写入，引用保持悬空（与原文档一致）
            idx = int(tok)
            while len(cur) <= idx:
                cur.append(None)
            if not isinstance(cur[idx], (dict, list)):
                cur[idx] = [] if want_list else {}
            cur = cur[idx]
        else:
            return
    last = tokens[-1]
    if isinstance(cur, dict):
        cur[last] = value
    elif isinstance(cur, list) and last.isdigit():
        idx = int(last)
        while len(cur) <= idx:
            cur.append(None)
        cur[idx] = value


def _apply_ref_closure(src: dict[str, Any], dst: dict[str, Any]) -> None:
    """把 dst 中（传递）引用到的本地节点从 src 复制进 dst 的原始指针位置。

    seen 集合既去重也打断循环：A→B→A 的互相引用会在第二轮被跳过；
    未被引用的 components 永远不会被复制，即"未引用即剔除"。
    """
    queue: list[str] = []
    _collect_refs(dst, queue)
    seen: set[str] = set()
    while queue:
        ref = queue.pop()
        try:
            pointer = normalize_pointer(ref)
        except PointerError:
            continue  # 外部引用：保持原样（与原文档中的可解析性一致）
        if not pointer or pointer in seen:
            continue
        seen.add(pointer)
        try:
            target = resolve_pointer(src, pointer)
        except PointerError:
            continue  # 原文档中即为悬空引用，保持原样
        copied = copy.deepcopy(target)
        _set_pointer(dst, pointer, copied)
        _collect_refs(copied, queue)


# ---------------------------------------------------------------------- #
# 复核
# ---------------------------------------------------------------------- #
def _verify(
    finding: dict[str, Any],
    old_min: dict[str, Any],
    new_min: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """用现有差异引擎复核目标发现在最小文档对上仍能复现。"""
    rerun = run_diff(old_min, new_min)
    for cand in rerun:
        if (
            cand.get("rule_id") == finding.get("rule_id")
            and cand.get("direction") == finding.get("direction")
            and cand.get("endpoint") == finding.get("endpoint")
            and cand.get("location") == finding.get("location")
        ):
            return cand, rerun
    raise ReproError(
        "裁剪后的最小文档未能复现目标发现"
        f"（rule_id={finding.get('rule_id')}，"
        f"端点={finding.get('endpoint') or '—'}，"
        f"定位={finding.get('location') or '—'}）。"
        "为避免提供失真的复现包，已中止导出。"
    )


# ---------------------------------------------------------------------- #
# 打包
# ---------------------------------------------------------------------- #
def build_repro_package(
    review: dict[str, Any], finding: dict[str, Any]
) -> tuple[bytes, dict[str, Any]]:
    """生成最小复现包 ZIP，返回 (zip_bytes, manifest)。

    复核失败时抛 ReproError —— 宁可报错也不提供失真的包。
    """
    old_doc = review.get("old_spec")
    new_doc = review.get("new_spec")
    if not isinstance(old_doc, dict) or not isinstance(new_doc, dict):
        raise ReproError("该审查记录缺少原文档快照，无法生成复现包。")

    method, path = _parse_endpoint(finding.get("endpoint"))
    keep_servers = _needs_servers(finding)
    keep_security = _needs_security(finding)

    old_min = _minimal_doc(old_doc, path, method, keep_servers, keep_security)
    new_min = _minimal_doc(new_doc, path, method, keep_servers, keep_security)
    _apply_ref_closure(old_doc, old_min)
    _apply_ref_closure(new_doc, new_min)

    matched, rerun = _verify(finding, old_min, new_min)

    manifest = {
        "format": REPRO_FORMAT,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tool": {"name": "openapi-compat-review", "version": "1.0.0"},
        "review": {
            "id": review.get("id"),
            "title": review.get("title"),
            "created_at": review.get("created_at"),
            "old_version": review.get("old_version"),
            "new_version": review.get("new_version"),
            "old_filename": review.get("old_filename"),
            "new_filename": review.get("new_filename"),
        },
        "finding": {
            "id": finding.get("id"),
            "fingerprint": finding_fingerprint(finding),
            "rule_id": finding.get("rule_id"),
            "severity": finding.get("severity"),
            "category": finding.get("category"),
            "direction": finding.get("direction"),
            "change": finding.get("change"),
            "endpoint": finding.get("endpoint"),
            "location": finding.get("location"),
            "message": finding.get("message"),
            "old_json_path": finding.get("old_json_path"),
            "new_json_path": finding.get("new_json_path"),
            "old_ref": finding.get("old_ref"),
            "new_ref": finding.get("new_ref"),
        },
        "verification": {
            "status": "passed",
            "engine": "run_diff",
            "checked_fields": ["rule_id", "direction", "endpoint", "location"],
            "matched_finding_id": matched.get("id"),
            "reproduced_findings_total": len(rerun),
        },
        "files": ["old.yaml", "new.yaml", "manifest.json"],
    }

    old_yaml = yaml.safe_dump(old_min, allow_unicode=True, sort_keys=False)
    new_yaml = yaml.safe_dump(new_min, allow_unicode=True, sort_keys=False)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("old.yaml", old_yaml)
        zf.writestr("new.yaml", new_yaml)
        zf.writestr(
            "manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2)
        )
    return buf.getvalue(), manifest

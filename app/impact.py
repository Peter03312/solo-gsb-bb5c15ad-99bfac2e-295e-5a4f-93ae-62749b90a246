"""调用清单影响定位：把 CSV 调用清单（method, path, count）匹配到旧版 paths 操作。

匹配规则（只使用**旧版** paths，查询串不参与匹配）：

- 路径模板按 `/` 切分为段，`{name}` 形式的段是参数段，其余为静态段；
  具体路径与模板段数相同、且每段要么与静态段全等、要么对应参数段，才算结构匹配；
- 同一 (方法, 路径) 的结构匹配候选按**静态段数量**排序，静态段越多越优先
  （如 `/orgs/{id}` 优先于 `/{root}`）；
- 最优候选唯一 → 调用量计入该操作；最优候选不唯一 → 标为**歧义**，不分配调用量；
- 结构上有匹配模板但该模板未定义对应 HTTP 方法，与"没有任何模板匹配"分别给出原因。

解析规则：CSV 首行须为含 method / path / count 的表头；count 须为正整数；
同一 (method, path)（忽略查询串）的重复行调用量累加。结果为纯函数实时计算、
不入库，仅供本次查看，不改变审查结论。
"""
from __future__ import annotations

import csv
import io
from typing import Any

from .diff import BREAKING, HTTP_METHODS
from .refs import resolve_pointer

REQUIRED_COLUMNS = ("method", "path", "count")
_METHOD_SET = {m.upper() for m in HTTP_METHODS}


class ImpactCsvError(ValueError):
    """CSV 文件级格式错误（缺少必需列 / 无数据行等），映射为 HTTP 400。"""


# ---------------------------------------------------------------------- #
# 路径模板匹配
# ---------------------------------------------------------------------- #
def _split_segments(path: str) -> list[str]:
    """/pets/{id} → ['pets', '{id}']；根路径 / → []（忽略空段）。"""
    return [seg for seg in path.strip("/").split("/") if seg != ""]


def _is_param_segment(segment: str) -> bool:
    """OpenAPI 路径模板参数段：{name}（允许花括号内空白，如 { id }）。"""
    return len(segment) >= 3 and segment.startswith("{") and segment.endswith("}")


def _segment_matches(template_seg: str, concrete_seg: str) -> bool:
    return _is_param_segment(template_seg) or template_seg == concrete_seg


def _build_old_operations(old_doc: dict[str, Any]) -> list[dict[str, Any]]:
    """枚举旧版 paths 下全部操作：{method, template, segments}。

    path item 若为本地 $ref 引用先解引用；无法解析的引用跳过。
    """
    operations: list[dict[str, Any]] = []
    paths = old_doc.get("paths")
    if not isinstance(paths, dict):
        return operations
    for template, raw_item in paths.items():
        if not isinstance(template, str):
            continue
        item = raw_item
        if isinstance(raw_item, dict) and isinstance(raw_item.get("$ref"), str):
            try:
                item = resolve_pointer(old_doc, raw_item["$ref"])
            except KeyError:
                continue
        if not isinstance(item, dict):
            continue
        segments = _split_segments(template)
        for method in HTTP_METHODS:
            if isinstance(item.get(method), dict):
                operations.append({
                    "method": method.upper(),
                    "template": template,
                    "segments": segments,
                    "static_count": sum(
                        1 for s in segments if not _is_param_segment(s)
                    ),
                })
    return operations


def _match_operation(
    method: str, concrete_segments: list[str], operations: list[dict[str, Any]]
) -> tuple[str, dict[str, Any] | None, list[dict[str, Any]]]:
    """把一行调用匹配到旧版操作。

    返回 (状态, 唯一候选或 None, 参与裁决的候选列表)；
    状态：matched | ambiguous | no_method | no_template。
    """
    # 先不区分方法做结构匹配，用于区分"模板存在但没有该方法"与"根本没有模板"
    structural = [
        op for op in operations
        if len(op["segments"]) == len(concrete_segments)
        and all(
            _segment_matches(t, c)
            for t, c in zip(op["segments"], concrete_segments)
        )
    ]
    if not structural:
        return "no_template", None, []
    same_method = [op for op in structural if op["method"] == method]
    if not same_method:
        templates = sorted({op["template"] for op in structural})
        return "no_method", None, [
            op for op in structural if op["template"] in templates
        ]
    best_score = max(op["static_count"] for op in same_method)
    best = [op for op in same_method if op["static_count"] == best_score]
    if len(best) == 1:
        return "matched", best[0], best
    return "ambiguous", None, best


# ---------------------------------------------------------------------- #
# CSV 解析
# ---------------------------------------------------------------------- #
def _strip_query(path: str) -> str:
    """去掉查询串（? 及其后内容），查询串不参与匹配；保留片段本身。"""
    return path.split("?", 1)[0]


def parse_inventory_csv(text: str) -> list[dict[str, Any]]:
    """解析调用清单 CSV，返回逐行记录（物理行号 + 原始字段 + 规范化结果）。

    文件级问题（空文件 / 缺列 / 无数据行）抛 ImpactCsvError；
    行级问题（空字段、count 非正整数等）记录在每行的 error 中，不中断解析。
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    reader = csv.reader(io.StringIO(text))
    rows = list(reader)
    if not rows or not any(cell.strip() for cell in rows[0]):
        raise ImpactCsvError("CSV 内容为空：首行须为 method,path,count 表头。")

    header = [cell.strip() for cell in rows[0]]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise ImpactCsvError(
            "CSV 缺少必需列：" + "、".join(missing)
            + "。首行须为包含 method、path、count 的表头。"
        )
    idx = {name: header.index(name) for name in REQUIRED_COLUMNS}

    records: list[dict[str, Any]] = []
    for line_no, row in enumerate(rows[1:], start=2):
        # 整行空白跳过（不计入任何统计）
        if not any(cell.strip() for cell in row):
            continue

        def cell(name: str) -> str:
            i = idx[name]
            return row[i].strip() if i < len(row) else ""

        raw_method = cell("method")
        raw_path = cell("path")
        raw_count = cell("count")

        rec: dict[str, Any] = {
            "line": line_no,
            "method": raw_method,
            "path": raw_path,
            "count_raw": raw_count,
            "error": None,
        }

        if not raw_method:
            rec["error"] = "method 为空。"
            records.append(rec)
            continue
        method = raw_method.upper()
        if method not in _METHOD_SET:
            rec["error"] = f"不支持的 HTTP 方法：{raw_method}。"
            records.append(rec)
            continue
        rec["method"] = method

        if not raw_path:
            rec["error"] = "path 为空。"
            records.append(rec)
            continue
        path = _strip_query(raw_path)
        rec["query_ignored"] = "?" in raw_path
        if not path.startswith("/"):
            rec["error"] = f"路径必须以 / 开头：{raw_path}。"
            records.append(rec)
            continue
        rec["path"] = path

        if not raw_count:
            rec["error"] = "count 为空，须为正整数。"
            records.append(rec)
            continue
        if not raw_count.isdigit() or int(raw_count) <= 0:
            rec["error"] = f"count 须为正整数，实际为：{raw_count}。"
            records.append(rec)
            continue
        rec["count"] = int(raw_count)
        records.append(rec)

    if not records:
        raise ImpactCsvError("CSV 没有任何数据行（首行之后至少需要一行调用记录）。")
    return records


# ---------------------------------------------------------------------- #
# 汇总 + 匹配 + 关联破坏性发现
# ---------------------------------------------------------------------- #
def _finding_brief(f: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": f.get("id"),
        "rule_id": f.get("rule_id"),
        "severity": f.get("severity"),
        "direction": f.get("direction"),
        "category": f.get("category"),
        "location": f.get("location"),
        "message": f.get("message"),
    }


def build_impact(
    old_doc: dict[str, Any],
    findings: list[dict[str, Any]],
    csv_text: str,
) -> dict[str, Any]:
    """解析调用清单并定位影响，返回影响表 / 未匹配 / 歧义 / 非法行。

    纯函数、无存储：调用结果只供本次查看，不改变审查结论。
    """
    records = parse_inventory_csv(csv_text)
    operations_pool = _build_old_operations(old_doc)

    # 规范化 findings 的序号（历史数据无引擎序号时按 1 起始补齐）
    norm_findings = []
    for i, f in enumerate(findings or []):
        g = dict(f)
        if not isinstance(g.get("id"), int):
            g["id"] = i + 1
        norm_findings.append(g)
    breaking = [f for f in norm_findings if f.get("severity") == BREAKING]

    # 操作级破坏：endpoint 形如 "GET /pets/{id}"；路径级破坏：endpoint 为模板本身
    op_findings: dict[str, list[dict[str, Any]]] = {}
    path_findings: dict[str, list[dict[str, Any]]] = {}
    for f in breaking:
        ep = f.get("endpoint")
        if not isinstance(ep, str):
            continue
        if " " in ep:
            op_findings.setdefault(ep, []).append(f)
        else:
            path_findings.setdefault(ep, []).append(f)

    # ---- 合法行按 (method, 去查询串路径) 聚合，重复行累加 ----
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    invalid_rows: list[dict[str, Any]] = []
    for rec in records:
        if rec["error"]:
            invalid_rows.append({
                "line": rec["line"],
                "method": rec["method"],
                "path": rec["path"],
                "count": rec["count_raw"],
                "reason": rec["error"],
            })
            continue
        key = (rec["method"], rec["path"])
        g = groups.get(key)
        if g is None:
            g = groups[key] = {
                "method": rec["method"],
                "path": rec["path"],
                "count": 0,
                "lines": [],
                "query_ignored": False,
            }
        g["count"] += rec["count"]
        g["lines"].append(rec["line"])
        g["query_ignored"] = g["query_ignored"] or rec.get("query_ignored", False)

    matched: dict[tuple[str, str], dict[str, Any]] = {}
    unmatched_rows: list[dict[str, Any]] = []
    ambiguous_rows: list[dict[str, Any]] = []

    for g in groups.values():
        segments = _split_segments(g["path"])
        status, unique, candidates = _match_operation(
            g["method"], segments, operations_pool
        )
        base = {
            "method": g["method"],
            "path": g["path"],
            "count": g["count"],
            "lines": g["lines"],
            "query_ignored": g["query_ignored"],
        }
        if status == "matched" and unique is not None:
            op_key = (unique["method"], unique["template"])
            entry = matched.get(op_key)
            if entry is None:
                entry = matched[op_key] = {
                    "method": unique["method"],
                    "template": unique["template"],
                    "endpoint": f"{unique['method']} {unique['template']}",
                    "matched_paths": [],
                    "count": 0,
                }
            entry["count"] += g["count"]
            entry["matched_paths"].append({
                "path": g["path"],
                "count": g["count"],
                "lines": g["lines"],
                "query_ignored": g["query_ignored"],
            })
        elif status == "ambiguous":
            templates = sorted({f"{op['method']} {op['template']}" for op in candidates})
            base["reason"] = (
                "静态段优先级相同的最优候选不唯一："
                + "、".join(templates)
                + "；记为歧义，不分配调用量。"
            )
            base["candidates"] = templates
            ambiguous_rows.append(base)
        elif status == "no_method":
            templates = sorted({op["template"] for op in candidates})
            base["reason"] = (
                f"旧版路径模板 {'、'.join(templates)} 结构匹配，但未定义 "
                f"{g['method']} 操作。"
            )
            unmatched_rows.append(base)
        else:
            base["reason"] = (
                f"旧版 paths 中没有与 {g['method']} {g['path']} 段数和静态段匹配的路径模板。"
            )
            unmatched_rows.append(base)

    # ---- 影响表：每个匹配到的操作 + 关联破坏性发现 ----
    operations: list[dict[str, Any]] = []
    for (method, template), entry in matched.items():
        endpoint = f"{method} {template}"
        op_bad = [
            _finding_brief(f) for f in op_findings.get(endpoint, [])
        ]
        path_bad = [
            _finding_brief(f) for f in path_findings.get(template, [])
        ]
        op_bad.sort(key=lambda x: x["id"])
        path_bad.sort(key=lambda x: x["id"])
        operations.append({
            **entry,
            "breaking_count": len(op_bad),
            "path_breaking_count": len(path_bad),
            "breaking_findings": op_bad,
            "path_breaking_findings": path_bad,
        })
    operations.sort(key=lambda o: (-o["count"], -o["breaking_count"], o["endpoint"]))

    def _lines_text(g: dict[str, Any]) -> str:
        lines = sorted(g["lines"])
        if len(lines) == 1:
            return f"第 {lines[0]} 行"
        contiguous = lines == list(range(lines[0], lines[0] + len(lines)))
        if contiguous:
            return f"第 {lines[0]}–{lines[-1]} 行（{len(lines)} 行累加）"
        shown = "、".join(str(n) for n in lines[:6])
        if len(lines) > 6:
            shown += f" 等 {len(lines)} 行"
        return f"第 {shown} 行（{len(lines)} 行累加）"

    for row in unmatched_rows + ambiguous_rows:
        row["source"] = _lines_text(row)

    total_calls = sum(g["count"] for g in groups.values())
    matched_calls = sum(o["count"] for o in operations)
    ambiguous_calls = sum(r["count"] for r in ambiguous_rows)
    unmatched_calls = sum(r["count"] for r in unmatched_rows)

    return {
        "summary": {
            "valid_rows": len(groups),
            "invalid_rows": len(invalid_rows),
            "total_calls": total_calls,
            "matched_operations": len(operations),
            "matched_calls": matched_calls,
            "ambiguous_rows": len(ambiguous_rows),
            "ambiguous_calls": ambiguous_calls,
            "unmatched_rows": len(unmatched_rows),
            "unmatched_calls": unmatched_calls,
            "affected_operations": sum(
                1 for o in operations
                if o["breaking_count"] or o["path_breaking_count"]
            ),
        },
        "operations": operations,
        "unmatched": sorted(unmatched_rows, key=lambda r: -r["count"]),
        "ambiguous": sorted(ambiguous_rows, key=lambda r: -r["count"]),
        "invalid": invalid_rows,
        "note": "导入结果只供本次查看，不改变审查结论。",
    }

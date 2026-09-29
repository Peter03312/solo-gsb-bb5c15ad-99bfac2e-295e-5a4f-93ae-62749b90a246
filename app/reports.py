"""审查报告生成：Markdown（人读）与 JSON（机器读）。"""
from __future__ import annotations

import json
from typing import Any

from .diff import BREAKING, DEPRECATION, INFO, NON_BREAKING
from .exemptions import STATUS_CN as EXEMPTION_STATUS_CN

SEV_CN = {
    BREAKING: "🔴 破坏性",
    NON_BREAKING: "🟢 兼容",
    DEPRECATION: "🟡 废弃提示",
    INFO: "⚪ 信息",
}
CHANGE_CN = {"added": "新增", "removed": "删除", "changed": "修改"}
DIRECTION_CN = {
    "request": "请求",
    "response": "响应",
    "shared": "请求/响应",
    "none": "—",
}


def build_markdown(review: dict[str, Any]) -> str:
    summary = review["summary"]
    counts = summary["counts"]
    ex_summary = review.get("exemption_summary")
    lines: list[str] = []

    lines.append(f"# OpenAPI 兼容性审查报告")
    lines.append("")
    title = review.get("title") or "未命名审查"
    lines.append(f"- **审查标题**：{title}")
    lines.append(f"- **审查时间**：{review.get('created_at', '')}")
    lines.append(
        f"- **版本对比**：{review.get('old_version') or '未知'}  →  "
        f"{review.get('new_version') or '未知'}"
    )
    if review.get("api_id"):
        lines.append(f"- **API 标识**：{review['api_id']}")
    if review.get("old_filename") or review.get("new_filename"):
        lines.append(
            f"- **文档来源**：{review.get('old_filename') or '(粘贴)'}  →  "
            f"{review.get('new_filename') or '(粘贴)'}"
        )
    verdict = "❌ 存在破坏性变更，不建议直接发布" if not summary["compatible"] else "✅ 未发现破坏性变更"
    lines.append(f"- **结论**：{verdict}")
    if ex_summary is not None:
        lines.append(f"- **放行结论**：{ex_summary['conclusion']}")
    lines.append("")

    lines.append("## 统计概览")
    lines.append("")
    lines.append("| 级别 | 数量 |")
    lines.append("| --- | ---: |")
    lines.append(f"| 🔴 破坏性变更 | {counts.get(BREAKING, 0)} |")
    if ex_summary is not None:
        lines.append(f"| 🛡️ 已豁免破坏项 | {ex_summary['exempted']} |")
        lines.append(f"| ⚠️ 未豁免破坏项 | {ex_summary['unexempted']} |")
    lines.append(f"| 🟢 兼容性变更 | {counts.get(NON_BREAKING, 0)} |")
    lines.append(f"| 🟡 废弃提示 | {counts.get(DEPRECATION, 0)} |")
    lines.append(f"| ⚪ 信息性变更 | {counts.get(INFO, 0)} |")
    lines.append(f"| **合计** | **{summary['total']}** |")
    lines.append("")

    if summary.get("by_category"):
        lines.append("### 按分类")
        lines.append("")
        for cat, n in summary["by_category"].items():
            lines.append(f"- {cat}：{n}")
        lines.append("")

    findings: list[dict[str, Any]] = review["findings"]
    breaking = [f for f in findings if f["severity"] == BREAKING]
    if breaking:
        lines.append("## 🔴 破坏性变更明细")
        lines.append("")
        _render_finding_table(lines, breaking, with_exemption=True)

    other = [f for f in findings if f["severity"] != BREAKING]
    if other:
        lines.append("## 其他变更")
        lines.append("")
        _render_finding_table(lines, other)

    exemptions = review.get("exemptions") or []
    if exemptions:
        lines.append("## 🛡️ 限时豁免记录")
        lines.append("")
        lines.append("| # | 状态 | 规则 | 端点 | 定位 | 理由 | 到期（UTC） | 当前命中 |")
        lines.append("| ---: | --- | --- | --- | --- | --- | --- | --- |")
        for row in exemptions:
            lines.append(
                "| {id} | {status} | {rule} | {endpoint} | `{loc}` | {reason} | {exp} | {hit} |".format(
                    id=row["id"],
                    status=EXEMPTION_STATUS_CN.get(row.get("status"), row.get("status") or ""),
                    rule=_md_escape(row.get("rule_id") or ""),
                    endpoint=_md_escape(row.get("endpoint") or "—"),
                    loc=_md_escape(row.get("location") or ""),
                    reason=_md_escape(row.get("reason") or "—"),
                    exp=_md_escape(row.get("expires_at") or "—"),
                    hit="是" if row.get("matched") else "否",
                )
            )
        lines.append("")

    lines.append("")
    lines.append("---")
    lines.append("*由 OpenAPI 兼容性审查工具生成。规则按请求/响应两个方向分别判定。*")
    return "\n".join(lines)


def _exemption_cell(f: dict[str, Any]) -> str:
    """破坏项表格的豁免列：匹配状态 + 理由 + 到期时间。"""
    ex = f.get("exemption")
    if not ex:
        return "—"
    status = EXEMPTION_STATUS_CN.get(ex.get("status"), ex.get("status") or "")
    reason = _md_escape(ex.get("reason") or "—")
    expires = _md_escape(ex.get("expires_at") or "—")
    return f"{status} · 至 {expires} · {reason}"


def _render_finding_table(
    lines: list[str], findings: list[dict[str, Any]], *, with_exemption: bool = False
) -> None:
    head = "| # | 级别 | 分类 | 方向 | 变更 | 端点 | 定位 | 原因 |"
    sep = "| ---: | --- | --- | --- | --- | --- | --- | --- |"
    if with_exemption:
        head += " 豁免 |"
        sep += " --- |"
    lines.append(head)
    lines.append(sep)
    for i, f in enumerate(findings, 1):
        row = "| {idx} | {sev} | {cat} | {direction} | {change} | {endpoint} | `{loc}` | {msg} |".format(
            idx=i,
            sev=SEV_CN.get(f["severity"], f["severity"]),
            cat=_md_escape(f.get("category", "")),
            direction=DIRECTION_CN.get(f.get("direction"), f.get("direction", "")),
            change=CHANGE_CN.get(f.get("change"), f.get("change", "")),
            endpoint=_md_escape(f.get("endpoint") or "—"),
            loc=_md_escape(f.get("location", "")),
            msg=_md_escape(f.get("message", "")),
        )
        if with_exemption:
            row += f" {_exemption_cell(f)} |"
        lines.append(row)
    lines.append("")

    # 附录：旧值/新值与 JSON 路径，便于精确定位
    lines.append("<details><summary>JSON 路径与取值明细</summary>")
    lines.append("")
    for i, f in enumerate(findings, 1):
        lines.append(f"**{i}. {f.get('rule_id')}**")
        if f.get("old_json_path"):
            lines.append(f"- 旧版路径：`{f['old_json_path']}`")
        if f.get("new_json_path"):
            lines.append(f"- 新版路径：`{f['new_json_path']}`")
        if f.get("old_ref"):
            lines.append(f"- 旧版引用：`{f['old_ref']}`")
        if f.get("new_ref"):
            lines.append(f"- 新版引用：`{f['new_ref']}`")
        if f.get("old_value") is not None or f.get("new_value") is not None:
            lines.append(
                f"- 旧值：`{json.dumps(f.get('old_value'), ensure_ascii=False)}`"
            )
            lines.append(
                f"- 新值：`{json.dumps(f.get('new_value'), ensure_ascii=False)}`"
            )
        ex = f.get("exemption")
        if ex:
            lines.append(
                f"- 豁免：{EXEMPTION_STATUS_CN.get(ex.get('status'), ex.get('status') or '')}"
            )
            lines.append(f"- 豁免理由：{_md_escape(ex.get('reason') or '—')}")
            lines.append(f"- 豁免到期（UTC）：`{ex.get('expires_at') or '—'}`")
            if ex.get("revoked_at"):
                lines.append(f"- 撤销时间（UTC）：`{ex['revoked_at']}`")
        lines.append("")
    lines.append("</details>")
    lines.append("")


def build_json(review: dict[str, Any]) -> str:
    payload = {
        "id": review.get("id"),
        "title": review.get("title"),
        "created_at": review.get("created_at"),
        "old_filename": review.get("old_filename"),
        "new_filename": review.get("new_filename"),
        "old_version": review.get("old_version"),
        "new_version": review.get("new_version"),
        "api_id": review.get("api_id"),
        "summary": review["summary"],
        "exemption_summary": review.get("exemption_summary"),
        "exemptions": review.get("exemptions") or [],
        "findings": review["findings"],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _md_escape(text: Any) -> str:
    if text is None:
        return "—"
    return str(text).replace("|", "\\|").replace("\n", " ")

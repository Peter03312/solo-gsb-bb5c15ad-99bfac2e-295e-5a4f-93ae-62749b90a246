"""限时豁免：按 API 标识 + 语义指纹匹配破坏性发现，支持到期与撤销。

匹配键 = 规则 + 方向 + 端点 + 语义位置 + 旧值 + 新值（值以规范 JSON 比较：
对象键序忽略、数组顺序与值类型保留），刻意不含发现序号——重新审查、
序号变化不影响匹配；同位置的变更值一旦变化，旧豁免自然失配，须重新处理。

豁免状态在读取审查时按当前时间重新计算：已撤销，或当前时间达到（>=）
UTC 到期时间，即失效，不再计入"已豁免"。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .diff import BREAKING

ACTIVE = "active"
EXPIRED = "expired"
REVOKED = "revoked"

STATUS_CN = {ACTIVE: "生效中", EXPIRED: "已过期", REVOKED: "已撤销"}


def canonical_value(value: Any) -> str:
    """值的规范 JSON 形式：对象键排序（键序无关），数组顺序与值类型保留。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), default=str)


def finding_match_key(finding: dict[str, Any]) -> tuple[str, ...]:
    """发现的豁免匹配键：规则 / 方向 / 端点 / 语义位置 / 新旧值。

    刻意不含发现序号（id）与严重度：重新审查、发现序号变化不影响匹配。
    """
    return (
        str(finding.get("rule_id") or ""),
        str(finding.get("direction") or ""),
        str(finding.get("endpoint") or ""),
        str(finding.get("location") or ""),
        canonical_value(finding.get("old_value")),
        canonical_value(finding.get("new_value")),
    )


def parse_utc(text: str) -> datetime:
    """解析 ISO 8601 时间为 UTC aware datetime；无时区信息一律按 UTC 处理。"""
    s = str(text).strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_utc(dt: datetime) -> str:
    """UTC 时间的规范输出：秒级 ISO 8601，带 Z 后缀。"""
    return (
        dt.astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def exemption_status(row: dict[str, Any], now: datetime) -> str:
    """豁免当前状态：已撤销 > 已到期（当前时间达到到期时间即失效）> 生效中。"""
    if row.get("revoked_at"):
        return REVOKED
    try:
        expires = parse_utc(str(row.get("expires_at") or ""))
    except (ValueError, TypeError):
        return EXPIRED  # 无法解析的到期时间按失效处理，不予放行
    return EXPIRED if now >= expires else ACTIVE


def _row_match_key(row: dict[str, Any]) -> tuple[str, ...]:
    """豁免记录的匹配键（与 finding_match_key 同构）。"""
    return (
        str(row.get("rule_id") or ""),
        str(row.get("direction") or ""),
        str(row.get("endpoint") or ""),
        str(row.get("location") or ""),
        _canon_stored(row.get("old_value")),
        _canon_stored(row.get("new_value")),
    )


def _canon_stored(value: Any) -> str:
    """库中存放的规范 JSON 字符串原样使用；NULL（异常历史数据）按该值的规范形处理。"""
    return value if isinstance(value, str) else canonical_value(value)


def apply_to_review(
    review: dict[str, Any],
    exemption_rows: list[dict[str, Any]] | None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """计算审查的豁免视图（纯函数，不改库；原始发现与统计保持不变）。

    返回审查副本：
    - 每条破坏发现附带 exemption（匹配状态 / 理由 / 到期时间），未匹配为 None；
    - exemption_summary：已豁免 / 未豁免破坏项数量与"未豁免为零才可放行"结论；
    - exemptions：该 API 标识下全部豁免记录及当前命中情况。
    无 API 标识的审查不应用任何豁免（exemption_summary.applied = False）。
    """
    now = now or datetime.now(timezone.utc)
    findings = review.get("findings") or []
    api_id = (review.get("api_id") or "").strip()
    applied = bool(api_id)

    rows: list[dict[str, Any]] = []
    for r in exemption_rows or []:
        row = dict(r)
        row["status"] = exemption_status(row, now)
        rows.append(row)

    # 匹配键 → 候选豁免（仅同一 API 标识下的记录参与）
    by_key: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    if applied:
        for row in rows:
            by_key.setdefault(_row_match_key(row), []).append(row)

    rank = {ACTIVE: 0, EXPIRED: 1, REVOKED: 2}
    enriched: list[dict[str, Any]] = []
    breaking_total = 0
    exempted = 0
    for f in findings:
        g = dict(f)
        if g.get("severity") == BREAKING:
            breaking_total += 1
            match = None
            if applied:
                cands = by_key.get(finding_match_key(g))
                if cands:
                    # 生效中优先；同状态取最新创建的一条
                    match = sorted(
                        cands, key=lambda r: (rank[r["status"]], -int(r["id"]))
                    )[0]
            g["exemption"] = (
                {
                    "id": match["id"],
                    "status": match["status"],
                    "reason": match.get("reason"),
                    "expires_at": match.get("expires_at"),
                    "created_at": match.get("created_at"),
                    "revoked_at": match.get("revoked_at"),
                }
                if match is not None
                else None
            )
            if match is not None and match["status"] == ACTIVE:
                exempted += 1
        enriched.append(g)

    unexempted = breaking_total - exempted
    releasable = unexempted == 0
    summary = {
        "applied": applied,
        "api_id": api_id or None,
        "breaking_total": breaking_total,
        "exempted": exempted,
        "unexempted": unexempted,
        "releasable": releasable,
        "conclusion": (
            "✅ 未豁免破坏性变更为零，可放行"
            if releasable
            else f"❌ 仍有 {unexempted} 项未豁免破坏性变更，不可放行"
        ),
    }

    matched_ids = {f["exemption"]["id"] for f in enriched if f.get("exemption")}
    for row in rows:
        row["matched"] = row["id"] in matched_ids

    out = dict(review)
    out["findings"] = enriched
    out["exemption_summary"] = summary
    out["exemptions"] = rows
    return out

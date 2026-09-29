"""FastAPI 入口：审查、历史、引用展开树、根因聚合、破坏项对账、组件可达性审计、调用清单影响定位、请求样本回放、Link 后续调用参数预览、限时豁免、报告下载、最小复现包导出。"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database, exemptions, groups, impact, linkpreview, reachability, reconcile, reports, repro, samplecheck
from .diff import BREAKING, run_diff, summarize
from .parser import SpecParseError, load_spec
from .refs import build_ref_tree, expand_path_to_pointer, list_root_pointers

app = FastAPI(
    title="OpenAPI 版本兼容性审查",
    description="解析新旧两份 OpenAPI 3.x 文档，按请求/响应语义规则定位破坏性变更。",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    database.init_db()


@app.get("/api/health")
def health() -> dict[str, str]:
    """轻量健康检查（Docker HEALTHCHECK 与容器编排探活使用）。"""
    return {"status": "ok"}


# ---------------------------------------------------------------------- #
# 请求模型
# ---------------------------------------------------------------------- #
class ReviewRequest(BaseModel):
    old_spec: str = Field(..., description="旧版 OpenAPI 文档原文（JSON 或 YAML）")
    new_spec: str = Field(..., description="新版 OpenAPI 文档原文（JSON 或 YAML）")
    old_filename: str | None = None
    new_filename: str | None = None
    title: str | None = None
    api_id: str | None = Field(None, description="API 标识（用于限时豁免匹配；留空则不应用豁免）")


class ApiIdUpdate(BaseModel):
    api_id: str | None = Field(None, description="API 标识；空串或 null 表示清除")


class ExemptionCreate(BaseModel):
    finding_id: int = Field(..., description="破坏项发现序号 id")
    reason: str = Field(..., description="豁免理由（必填）")
    expires_at: str = Field(..., description="UTC 到期时间（ISO 8601，须为未来时间）")


class SampleCheckRequest(BaseModel):
    # 元素不约束为 dict：非对象样本由逐条校验给出错误，而不是整请求 422
    samples: list[Any] = Field(
        ...,
        description=(
            "请求样本数组；每条含 method（旧版操作方法）、path（旧版路径模板）、"
            "content_type（如 application/json）与 body（JSON 值或 JSON 文本）。"
            "非对象或字段缺失的元素在结果中逐条报错。"
        ),
    )


class LinkPreviewRequest(BaseModel):
    side: str = Field(..., description="在哪一侧文档预览：old（旧版）或 new（新版）")
    method: str = Field(..., description="源操作 HTTP 方法，如 GET（大小写不敏感）")
    path: str = Field(..., description="源操作路径模板，如 /pets/{id}")
    status_code: int = Field(..., description="提交的响应状态码（100–599 整数）")
    response_body: Any = Field(
        default=None,
        description=(
            "JSON 响应体（JSON 值；也接受 JSON 文本字符串，后端会再解析一次），"
            "供 $response.body#JSONPointer 取值。"
        ),
    )
    headers: dict[str, Any] | None = Field(
        default=None,
        description="提交的响应头（头名称 → 值）；$response.header.名称 取值时忽略大小写。",
    )


def _version_of(doc: dict[str, Any]) -> str | None:
    info = doc.get("info")
    if isinstance(info, dict) and info.get("version"):
        return str(info["version"])
    return None


def _norm_api_id(value: str | None) -> str | None:
    """API 标识规范化：去空白，空串视为未设置。"""
    return (value or "").strip() or None


def _enriched_review(review_id: int) -> dict[str, Any] | None:
    """读取审查并叠加豁免视图（每次读取按当前时间重新计算，不改库存储）。"""
    review = database.get_review(review_id)
    if review is None:
        return None
    api_id = _norm_api_id(review.get("api_id"))
    rows = database.list_exemptions(api_id) if api_id else []
    return exemptions.apply_to_review(review, rows)


# ---------------------------------------------------------------------- #
# 审查
# ---------------------------------------------------------------------- #
@app.post("/api/reviews", status_code=201)
def create_review(req: ReviewRequest) -> dict[str, Any]:
    try:
        old_doc = load_spec(req.old_spec, req.old_filename)
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail={"side": "old", "message": str(exc)})
    try:
        new_doc = load_spec(req.new_spec, req.new_filename)
    except SpecParseError as exc:
        raise HTTPException(status_code=422, detail={"side": "new", "message": str(exc)})

    findings = run_diff(old_doc, new_doc)
    summary_data = summarize(findings)

    title = req.title or (
        f"{new_doc.get('info', {}).get('title', 'API')} "
        f"{_version_of(old_doc) or '旧版'} → {_version_of(new_doc) or '新版'}"
    )

    review_id = database.create_review(
        title=title,
        old_filename=req.old_filename,
        new_filename=req.new_filename,
        old_version=_version_of(old_doc),
        new_version=_version_of(new_doc),
        api_id=_norm_api_id(req.api_id),
        summary=summary_data,
        findings=findings,
        old_doc=old_doc,
        new_doc=new_doc,
    )
    return _enriched_review(review_id)


@app.get("/api/reviews")
def get_reviews() -> dict[str, Any]:
    return {"items": database.list_reviews()}


@app.get("/api/reviews/{review_id}")
def get_review(review_id: int) -> dict[str, Any]:
    review = _enriched_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return review


@app.patch("/api/reviews/{review_id}")
def update_review_api_id(review_id: int, req: ApiIdUpdate) -> dict[str, Any]:
    """设置 / 清除审查的 API 标识；保存后按该标识重新匹配豁免。"""
    if database.get_review(review_id) is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    database.set_api_id(review_id, _norm_api_id(req.api_id))
    return _enriched_review(review_id)


@app.delete("/api/reviews/{review_id}", status_code=204)
def remove_review(review_id: int) -> Response:
    if not database.delete_review(review_id):
        raise HTTPException(status_code=404, detail="审查记录不存在")
    return Response(status_code=204)


# ---------------------------------------------------------------------- #
# 限时豁免（按 API 标识 + 语义指纹匹配破坏项，到期 / 撤销即失效）
# ---------------------------------------------------------------------- #
@app.post("/api/reviews/{review_id}/exemptions", status_code=201)
def create_exemption(review_id: int, req: ExemptionCreate) -> dict[str, Any]:
    """为审查中的某条破坏性发现创建限时豁免。

    匹配键（规则 / 方向 / 端点 / 语义位置 / 新旧值）由服务端从发现项计算，
    与发现序号无关；到期时间须为未来的 UTC 时间。返回叠加豁免视图后的审查。
    """
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    api_id = _norm_api_id(review.get("api_id"))
    if not api_id:
        raise HTTPException(
            status_code=400,
            detail="该审查未设置 API 标识，不能创建豁免；请先为审查填写 API 标识。",
        )

    finding = repro.find_finding(review["findings"], req.finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="发现项不存在")
    if finding.get("severity") != BREAKING:
        raise HTTPException(status_code=400, detail="仅破坏性变更可以创建豁免。")

    reason = (req.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="请填写豁免理由。")
    try:
        expires = exemptions.parse_utc(req.expires_at)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="到期时间格式无效，请使用 ISO 8601 UTC 时间（如 2026-10-01T00:00:00Z）。",
        )
    if expires <= datetime.now(timezone.utc):
        raise HTTPException(status_code=400, detail="到期时间必须是未来的 UTC 时间。")

    key = exemptions.finding_match_key(finding)
    database.create_exemption(
        api_id=api_id,
        rule_id=key[0],
        direction=key[1],
        endpoint=key[2],
        location=key[3],
        old_value=key[4],
        new_value=key[5],
        reason=reason,
        expires_at=exemptions.format_utc(expires),
    )
    return _enriched_review(review_id)


@app.post("/api/reviews/{review_id}/exemptions/{exemption_id}/revoke")
def revoke_exemption(review_id: int, exemption_id: int) -> dict[str, Any]:
    """撤销豁免（保留记录，撤销后立即失效）。返回叠加豁免视图后的审查。"""
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    row = database.get_exemption(exemption_id)
    if row is None:
        raise HTTPException(status_code=404, detail="豁免记录不存在")
    if row["api_id"] != (_norm_api_id(review.get("api_id")) or ""):
        raise HTTPException(status_code=400, detail="该豁免不属于此审查的 API 标识。")
    database.revoke_exemption(exemption_id)
    return _enriched_review(review_id)


# ---------------------------------------------------------------------- #
# 根因聚合（有向影响组）
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/groups")
def get_review_groups(review_id: int) -> dict[str, Any]:
    review = database.get_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    result = groups.build_impact_groups(review["findings"])
    result["review_id"] = review_id
    return result


# ---------------------------------------------------------------------- #
# 破坏项对账（基线审查 vs 当前审查）
# ---------------------------------------------------------------------- #
def _review_brief(review: dict[str, Any]) -> dict[str, Any]:
    """对账结果中附带的对账双方概要信息。"""
    return {
        "id": review.get("id"),
        "title": review.get("title"),
        "created_at": review.get("created_at"),
        "old_version": review.get("old_version"),
        "new_version": review.get("new_version"),
    }


@app.get("/api/reviews/{review_id}/reconcile")
def reconcile_review(review_id: int, baseline_id: int) -> dict[str, Any]:
    """以 baseline_id 为基线，与当前审查 review_id 对账破坏性发现。

    输出新增 / 延续 / 已消失三类破坏项及数量；基线不存在或与当前
    审查相同时返回可读错误。
    """
    current = database.get_review(review_id)
    if current is None:
        raise HTTPException(status_code=404, detail="当前审查记录不存在")
    if baseline_id == review_id:
        raise HTTPException(
            status_code=400,
            detail="基线审查与当前审查相同，请选择另一条历史审查作为基线",
        )
    baseline = database.get_review(baseline_id)
    if baseline is None:
        raise HTTPException(
            status_code=404,
            detail=f"基线审查 #{baseline_id} 不存在或已被删除",
        )
    result = reconcile.build_reconciliation(baseline["findings"], current["findings"])
    result["current"] = _review_brief(current)
    result["baseline"] = _review_brief(baseline)
    return result


# ---------------------------------------------------------------------- #
# 组件可达性审计（paths / webhooks 入口 → 本地 $ref 传递追踪）
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/reachability")
def get_reachability(review_id: int) -> dict[str, Any]:
    """组件可达性审计：分别对旧版、新版文档从 paths / webhooks 入口出发，
    沿本地 $ref 传递追踪 components（循环引用只访问一次），全局与操作级
    security 按名称引用的 securitySchemes 也算可达。

    返回各侧未被入口触达的组件（类型 / 名称 / 数量）、新版独有的未使用
    组件标记，以及无效本地引用明细（位置与原因）。实时计算，无额外存储。
    """
    specs = database.get_specs(review_id)
    if specs is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    old_doc, new_doc = specs
    result = reachability.build_reachability(old_doc, new_doc)
    result["review_id"] = review_id
    return result


# ---------------------------------------------------------------------- #
# 调用清单影响定位（CSV：method,path,count → 旧版 paths 操作；仅本次查看）
# ---------------------------------------------------------------------- #
@app.post("/api/reviews/{review_id}/impact")
async def import_inventory(review_id: int, request: Request) -> dict[str, Any]:
    """导入调用清单 CSV（method,path,count），按旧版 paths 的 HTTP 方法与
    路径模板匹配每行调用：静态段优先于参数段；最优候选不唯一则标为歧义、
    不分配调用量。唯一匹配的调用量按操作汇总并关联该操作的破坏性发现。

    请求体直接为 UTF-8 CSV 文本（Content-Type: text/csv）；查询串不参与
    匹配，count 须为正整数，重复行累加。结果实时计算、不入库，仅供本次
    查看，不改变审查结论。
    """
    review = database.get_review(review_id, with_specs=True)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    old_doc = review["old_spec"]
    raw = await request.body()
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="CSV 文件须为 UTF-8 编码文本。")
    try:
        result = impact.build_impact(old_doc, review["findings"], csv_text)
    except impact.ImpactCsvError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result["review_id"] = review_id
    return result


# ---------------------------------------------------------------------- #
# 请求样本回放（旧版接受而新版拒绝的请求体风险；仅本次查看）
# ---------------------------------------------------------------------- #
@app.post("/api/reviews/{review_id}/sample-check")
def sample_check(review_id: int, req: SampleCheckRequest) -> dict[str, Any]:
    """逐条回放请求样本，对比旧版 / 新版请求体校验结果。

    每条样本给出旧版与新版的请求体校验状态（接受 / 拒绝 / 无法判定）、
    失败字段路径，并汇总「旧版接受而新版拒绝」的风险样本。新版按相同
    方法与路径模板查找操作；操作或媒体类型消失视为新版拒绝。校验覆盖
    type、required、enum、properties、items、allOf 与本地 $ref（循环
    引用安全终止）；无法解析的引用或覆盖范围外约束标为无法判定，不计
    入风险。旧版操作不存在、样本格式错误逐条报错；旧版拒绝的样本不计
    风险。实时计算、不入库，仅供本次查看，不改变审查结论。
    """
    review = database.get_review(review_id, with_specs=True)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    try:
        result = samplecheck.build_sample_check(
            review["old_spec"], review["new_spec"], req.samples
        )
    except samplecheck.SampleCheckError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result["review_id"] = review_id
    return result


# ---------------------------------------------------------------------- #
# 响应实例 → Link 后续调用参数预览（仅本次查看）
# ---------------------------------------------------------------------- #
@app.post("/api/reviews/{review_id}/link-preview")
def link_preview(review_id: int, req: LinkPreviewRequest) -> dict[str, Any]:
    """用一次响应实例预览 OpenAPI Link 的后续调用参数。

    选择旧 / 新版文档与源操作（方法 + 路径模板），提交状态码、JSON 响应体与
    响应头；响应按精确码 → 范围码（1XX…5XX）→ default 匹配。逐条返回 Link 指针、
    目标方法 / 路径（唯一 operationId 或本地 operationRef）与每个目标参数的取值
    （合并路径级 + 操作级参数，操作级覆盖；支持 JSON 常量、
    $response.body#JSONPointer、$response.header.名称；指针解码 ~0/~1，头名忽略
    大小写，null 算已提供）。未知参数 / 歧义裸名 / 缺少必填值 / 不支持的表达式 /
    外部或失效 $ref 逐条报错。实时计算、不入库、不发请求、不改变审查结论。
    """
    review = database.get_review(review_id, with_specs=True)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    if req.side not in ("old", "new"):
        raise HTTPException(status_code=400, detail="side 必须是 old（旧版）或 new（新版）")
    doc = review["old_spec"] if req.side == "old" else review["new_spec"]

    # 允许把响应体写成 JSON 文本（与请求样本回放一致）
    response_body = req.response_body
    if isinstance(response_body, str):
        try:
            response_body = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail=f"响应体 JSON 文本解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）",
            )

    try:
        result = linkpreview.build_link_preview(
            doc,
            side=req.side,
            method=req.method,
            path=req.path,
            status_code=req.status_code,
            response_body=response_body,
            headers=req.headers,
        )
    except linkpreview.LinkPreviewError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    result["review_id"] = review_id
    return result


# ---------------------------------------------------------------------- #
# 引用展开树
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/pointers")
def get_pointers(review_id: int) -> dict[str, Any]:
    specs = database.get_specs(review_id)
    if specs is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    old_doc, new_doc = specs
    return {
        "old": list_root_pointers(old_doc),
        "new": list_root_pointers(new_doc),
    }


@app.get("/api/reviews/{review_id}/tree")
def get_tree(
    review_id: int,
    side: str = "new",
    pointer: str = "",
    target: str = "",
) -> dict[str, Any]:
    if side not in ("old", "new"):
        raise HTTPException(status_code=400, detail="side 必须是 old 或 new")
    specs = database.get_specs(review_id)
    if specs is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    doc = specs[0] if side == "old" else specs[1]
    tree = build_ref_tree(doc, pointer)
    target_path: list[str] = []
    if target:
        target_path = expand_path_to_pointer(tree, target)
    tree["target_path"] = target_path
    tree["target_found"] = bool(target_path)
    return tree


# ---------------------------------------------------------------------- #
# 报告下载
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/report")
def download_report(review_id: int, format: str = "markdown") -> Response:
    review = _enriched_review(review_id)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")

    safe_title = (review.get("title") or f"review-{review_id}")
    safe_title = "".join(c for c in safe_title if c.isalnum() or c in ("-", "_", " ", "."))
    safe_title = safe_title.strip().replace(" ", "_") or f"review-{review_id}"
    # HTTP 头仅支持 latin-1：ASCII 回退名 + RFC 5987 UTF-8 文件名（中文标题兼容）
    ascii_title = "".join(c for c in safe_title if c.isascii()) or f"review-{review_id}"

    def _disposition(ext: str) -> str:
        utf8_name = quote(f"openapi-review-{review_id}-{safe_title}.{ext}")
        return (
            f'attachment; filename="openapi-review-{review_id}-{ascii_title}.{ext}"; '
            f"filename*=UTF-8''{utf8_name}"
        )

    if format == "json":
        body = reports.build_json(review)
        return Response(
            content=body,
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": _disposition("json")},
        )

    body = reports.build_markdown(review)
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": _disposition("md")},
    )


# ---------------------------------------------------------------------- #
# 最小复现包导出
# ---------------------------------------------------------------------- #
@app.get("/api/reviews/{review_id}/findings/{finding_id}/repro")
def export_repro(review_id: int, finding_id: int) -> Response:
    review = database.get_review(review_id, with_specs=True)
    if review is None:
        raise HTTPException(status_code=404, detail="审查记录不存在")
    finding = repro.find_finding(review["findings"], finding_id)
    if finding is None:
        raise HTTPException(status_code=404, detail="发现项不存在")
    try:
        zip_bytes, _manifest = repro.build_repro_package(review, finding)
    except repro.ReproError as exc:
        # 复核未通过：返回可读错误，不提供失真的包
        raise HTTPException(status_code=409, detail=str(exc))
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="openapi-repro-r{review_id}-f{finding_id}.zip"'
            )
        },
    )


@app.exception_handler(SpecParseError)
async def _parse_error_handler(_request, exc: SpecParseError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"message": str(exc)})


# ---------------------------------------------------------------------- #
# 静态前端（挂载在最后，避免覆盖 /api）
# ---------------------------------------------------------------------- #
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

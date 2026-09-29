"""OpenAPI 文档载入：支持 JSON / YAML，校验为 OpenAPI 3.x。"""
from __future__ import annotations

import json
from typing import Any

import yaml


class SpecParseError(Exception):
    """文档无法解析或不是受支持的 OpenAPI 文档。"""


def load_spec(text: str, filename: str | None = None) -> dict[str, Any]:
    if text is None or not text.strip():
        raise SpecParseError("文档内容为空，请粘贴或上传 OpenAPI 文档。")

    stripped = text.strip()
    doc: Any
    if stripped[0] in "{[" :
        try:
            doc = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise SpecParseError(f"JSON 解析失败：{exc.msg}（第 {exc.lineno} 行第 {exc.colno} 列）") from exc
    else:
        try:
            doc = yaml.safe_load(stripped)
        except yaml.YAMLError as exc:
            raise SpecParseError(f"YAML 解析失败：{exc}") from exc
        if not isinstance(doc, dict):
            # 也可能是看起来不像 JSON 的纯 JSON，补一次尝试
            try:
                doc = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise SpecParseError(f"无法解析为 JSON 或 YAML：{exc}") from exc

    if not isinstance(doc, dict):
        raise SpecParseError("文档顶层必须是一个对象（OpenAPI 根对象）。")

    _validate_openapi(doc, filename)
    return doc


def _validate_openapi(doc: dict[str, Any], filename: str | None) -> None:
    source = f"文件 {filename} " if filename else ""
    if "openapi" not in doc:
        swagger = doc.get("swagger")
        if swagger:
            raise SpecParseError(
                f"{source}检测到 Swagger/OpenAPI {swagger}，本工具仅支持 OpenAPI 3.x 文档。"
            )
        raise SpecParseError(f"{source}缺少 openapi 版本字段，不是合法的 OpenAPI 3 文档。")

    version = str(doc["openapi"])
    if not version.startswith("3."):
        raise SpecParseError(f"{source}仅支持 OpenAPI 3.x，当前声明版本为 {version}。")

    paths = doc.get("paths")
    if not isinstance(paths, dict):
        raise SpecParseError(f"{source}缺少 paths 对象或 paths 不是映射。")

    if not isinstance(doc.get("info"), dict):
        raise SpecParseError(f"{source}缺少 info 对象。")

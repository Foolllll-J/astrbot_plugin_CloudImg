import os
import re
from urllib.parse import urlparse

import aiohttp

from astrbot import logger
from astrbot.core.message.components import Image, Video


class UtilsMixin:
    """通用工具方法：URL 构建、文件名推断、序号解析、媒体引用收集等。"""

    def _clamp_config_int(
        self,
        value: object,
        min_value: int,
        max_value: int | None,
        default: int,
    ) -> int:
        """Parse int config with hard bounds."""
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default

        if parsed < min_value:
            return min_value
        if max_value is not None and parsed > max_value:
            return max_value
        return parsed

    def _normalize_base_url(self, value: object) -> str:
        return str(value or "").strip().rstrip("/")

    def _build_url_from_base(self, base_url: str, path: str) -> str:
        normalized_base = self._normalize_base_url(base_url)
        normalized_path = (path or "").strip()
        if not normalized_path:
            return ""
        if not normalized_path.startswith("/"):
            normalized_path = f"/{normalized_path}"
        return (
            f"{normalized_base}{normalized_path}"
            if normalized_base
            else normalized_path
        )

    def _build_upload_display_url(self, src_path: str) -> str:
        src_value = (src_path or "").strip()
        if not src_value:
            return ""

        parsed = urlparse(src_value)
        if parsed.scheme and parsed.netloc:
            if self.public_base_url:
                path_and_suffix = parsed.path or "/"
                if parsed.query:
                    path_and_suffix = f"{path_and_suffix}?{parsed.query}"
                if parsed.fragment:
                    path_and_suffix = f"{path_and_suffix}#{parsed.fragment}"
                return self._build_url_from_base(self.public_base_url, path_and_suffix)
            return src_value

        display_base_url = self.public_base_url or self.base_url
        return self._build_url_from_base(display_base_url, src_value)

    def _extract_media_id(self, relative_file_path: str) -> str:
        """Use normalized path as media ID for dedupe tracking."""
        media_id = (relative_file_path or "").strip()
        if not media_id:
            return ""

        media_id = media_id.split("?", 1)[0]
        media_id = media_id.split("#", 1)[0]
        return media_id.lstrip("/")

    def _history_distance(self, history: list[str], media_id: str) -> int:
        """Larger value means farther from most recent records."""
        try:
            index = history.index(media_id)
        except ValueError:
            return len(history) + 1

        return len(history) - index

    def _handle_response_error(self, status: int, response_text: str) -> str:
        """处理 API 响应错误，记录日志并返回友好提示"""
        error_map = {
            400: "请求参数错误",
            401: "身份验证失败，请检查 auth_code 或 api_token",
            403: "权限不足，请检查 auth_code 或 api_token 是否正确",
            404: "资源未找到，请检查文件夹名是否正确",
            413: "文件体积超过限制",
            500: "图床服务器内部错误",
            502: "图床服务网关错误",
            503: "图床服务不可用",
            504: "图床服务网关超时",
        }

        friendly_msg = error_map.get(status, f"未知错误 (HTTP {status})")
        logger.error(f"API 请求失败: status={status}, response={response_text}")
        return f"操作失败: {friendly_msg}"

    async def download_image(self, url: str) -> bytes | None:
        """下载图片并返回字节数据"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.read()
        except Exception as e:
            logger.error(
                f"图片下载失败: url={self._redact_url_for_log(url)}, err={type(e).__name__}"
            )
            return None

    def _guess_filename_from_url(self, url: str, fallback_ext: str) -> str:
        try:
            parsed = urlparse(url)
            base = os.path.basename(parsed.path or "")
            if base and "." in base:
                return base
        except Exception:
            pass
        return f"upload{fallback_ext}"

    def _redact_url_for_log(self, url: str) -> str:
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return "<invalid-url>"
            base = os.path.basename(parsed.path or "")
            if base:
                return f"{parsed.scheme}://{parsed.netloc}/.../{base}"
            return f"{parsed.scheme}://{parsed.netloc}{parsed.path or ''}"
        except Exception:
            return "<invalid-url>"

    def _build_upload_reply(self, title: str, results: list[dict]) -> str:
        total = len(results)
        ok_results = [r for r in results if r.get("ok")]
        fail_results = [r for r in results if not r.get("ok")]

        # 如果只有一个任务且成功，返回精简格式
        if total == 1 and len(ok_results) == 1:
            res = ok_results[0]
            kind_name = "视频" if res.get("kind") == "video" else "图片"
            if self.show_upload_link and res.get("url"):
                return f"{kind_name}上传成功！\n链接: {res.get('url')}"
            return f"{kind_name}上传成功！"

        img_total = sum(1 for r in results if r.get("kind") == "image")
        vid_total = sum(1 for r in results if r.get("kind") == "video")
        img_ok = sum(1 for r in ok_results if r.get("kind") == "image")
        vid_ok = sum(1 for r in ok_results if r.get("kind") == "video")

        type_parts: list[str] = []
        if img_total:
            type_parts.append(f"图片 {img_ok}/{img_total}")
        if vid_total:
            type_parts.append(f"视频 {vid_ok}/{vid_total}")
        type_suffix = f"（{'，'.join(type_parts)}）" if type_parts else ""

        msg_lines = [f"{title}：成功 {len(ok_results)}/{total}{type_suffix}"]
        for r in ok_results:
            kind = "视频" if r.get("kind") == "video" else "图片"
            if self.show_upload_link:
                msg_lines.append(f"- 序号 {r['index']}: {kind}\n  链接: {r.get('url')}")
            else:
                msg_lines.append(f"- 序号 {r['index']}: {kind} 上传成功")
        for r in fail_results:
            kind = "视频" if r.get("kind") == "video" else "图片"
            msg_lines.append(f"- 序号 {r['index']}: {kind} 失败: {r.get('error')}")

        return "\n".join(msg_lines)

    def _parse_index_spec(
        self,
        spec: str | None,
        total: int,
        label: str = "媒体文件",
        empty_msg: str | None = None,
    ) -> tuple[list[int] | None, str | None]:
        if total <= 0:
            return None, empty_msg or f"未找到可上传的{label}"

        if spec is None:
            return list(range(1, total + 1)), None

        spec = str(spec).strip()
        if not spec:
            return list(range(1, total + 1)), None

        # 替换中文逗号为英文逗号
        spec = spec.replace("，", ",")
        parts = spec.split(",")
        indices = set()

        for part in parts:
            part = part.strip()
            if not part:
                continue

            # 1. 处理单数字 (支持负数，如 -1, -2)
            if re.fullmatch(r"-?\d+", part):
                idx = int(part)
                if idx == 0:
                    return None, "序号不能为 0"

                # 转换负数为正数索引
                final_idx = idx if idx > 0 else total + idx + 1

                if final_idx < 1 or final_idx > total:
                    return (
                        None,
                        f"序号 {idx} (计算为 {final_idx}) 超出范围：当前共有 {total} 个{label}",
                    )
                indices.add(final_idx)

            # 2. 处理范围 (如 1-3 或 -3--1)
            elif m := re.fullmatch(r"(-?\d+)-(-?\d+)", part):
                try:
                    start_val = int(m.group(1))
                    end_val = int(m.group(2))

                    if start_val == 0 or end_val == 0:
                        return None, "序号范围中不能包含 0"

                    # 转换逻辑
                    start_idx = start_val if start_val > 0 else total + start_val + 1
                    end_idx = end_val if end_val > 0 else total + end_val + 1

                    if start_idx > end_idx:
                        return None, f"序号范围 {part} 无效：起始位置大于结束位置"

                    if start_idx < 1 or end_idx > total:
                        return None, f"序号范围 {part} 超出边界 (1-{total})"

                    for i in range(start_idx, end_idx + 1):
                        indices.add(i)
                except Exception:
                    return None, f"序号范围 {part} 解析错误"
            else:
                return None, f"无法解析序号参数: {part}"

        if not indices:
            return list(range(1, total + 1)), None

        return sorted(list(indices)), None

    def _collect_media_refs(self, chain: list) -> list[dict]:
        refs: list[dict] = []
        for inner in chain:
            if isinstance(inner, Image):
                url = getattr(inner, "url", None)
                file_or_id = getattr(inner, "file", None)
                path = getattr(inner, "path", None)
                filename = None
                if isinstance(file_or_id, str) and file_or_id:
                    base = os.path.basename(file_or_id)
                    if base and "." in base:
                        filename = base
                if not filename and isinstance(url, str) and url:
                    filename = self._guess_filename_from_url(url, ".jpg")
                refs.append(
                    {
                        "kind": "image",
                        "url": url,
                        "file": file_or_id,
                        "path": path,
                        "filename": filename or "upload.jpg",
                    }
                )
            elif isinstance(inner, Video):
                url = getattr(inner, "url", None)
                file_or_id = getattr(inner, "file", None)
                path = getattr(inner, "path", None)
                filename = None
                if isinstance(file_or_id, str) and file_or_id:
                    base = os.path.basename(file_or_id)
                    if base and "." in base:
                        filename = base
                if not filename and isinstance(url, str) and url:
                    filename = self._guess_filename_from_url(url, ".mp4")
                if not filename and isinstance(path, str) and path:
                    base = os.path.basename(path)
                    if base and "." in base:
                        filename = base
                refs.append(
                    {
                        "kind": "video",
                        "url": url,
                        "file": file_or_id,
                        "path": path,
                        "filename": filename or "upload.mp4",
                    }
                )
        return [r for r in refs if r.get("url") or r.get("file") or r.get("path")]

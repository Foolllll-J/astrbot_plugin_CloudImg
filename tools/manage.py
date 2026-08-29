from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from astrbot.api import FunctionTool
from astrbot.api.event import AstrMessageEvent


def _json_ok(data: Any = None, **extra) -> str:
    payload: dict[str, Any] = {"ok": True}
    if data is not None:
        payload["data"] = data
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _json_err(error: str, **extra) -> str:
    payload: dict[str, Any] = {"ok": False, "error": error}
    payload.update(extra)
    return json.dumps(payload, ensure_ascii=False)


def _require_admin(event: AstrMessageEvent) -> str | None:
    if event.is_admin():
        return None
    return _json_err("仅管理员可用")


def _plugin_or_err(plugin: Any) -> str | None:
    if plugin is None:
        return _json_err("插件实例未初始化")
    return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


@dataclass
class CloudImgListTool(FunctionTool):
    plugin: Any = None
    name: str = "cloudimg_list"
    description: str = (
        "List files and subdirectories on the configured CloudFlare ImgBed. "
        "Use for browsing folders; not for random images."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "dir": {
                    "type": "string",
                    "description": "Optional directory relative path. Empty means root.",
                },
                "start": {
                    "type": "number",
                    "description": "Optional pagination offset. Default 0.",
                },
                "count": {
                    "type": "number",
                    "description": "Optional page size. Defaults to plugin list_page_size. Max 50.",
                },
                "search": {
                    "type": "string",
                    "description": "Optional filename search keyword.",
                },
                "file_type": {
                    "type": "string",
                    "description": "Optional filter: image, video, or empty for all.",
                },
                "recursive": {
                    "type": "boolean",
                    "description": "Optional. Recurse into subdirectories. Default false.",
                },
            },
            "required": [],
        }
    )

    async def run(
        self,
        event: AstrMessageEvent,
        dir: str = "",
        start: float = 0,
        count: float = 0,
        search: str = "",
        file_type: str = "",
        recursive: bool = False,
    ) -> str:
        if err := _require_admin(event):
            return err
        if err := _plugin_or_err(self.plugin):
            return err

        page_size = self.plugin.list_page_size
        max_count = 50
        count_val = _as_int(count, 0)
        if count_val <= 0:
            count_val = page_size
        count_val = max(1, min(count_val, max_count))

        result = await self.plugin.list_files(
            dir_path=str(dir or "").strip(),
            start=max(0, _as_int(start, 0)),
            count=count_val,
            recursive=_as_bool(recursive, False),
            search=str(search or "").strip(),
            file_type=str(file_type or "").strip(),
        )
        if isinstance(result, str):
            return _json_err(result)
        return _json_ok(result)


@dataclass
class CloudImgStatTool(FunctionTool):
    plugin: Any = None
    name: str = "cloudimg_stat"
    description: str = (
        "Count files under a CloudFlare ImgBed directory (including subdirectories)."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "dir": {
                    "type": "string",
                    "description": "Optional directory relative path. Empty means root.",
                },
            },
            "required": [],
        }
    )

    async def run(self, event: AstrMessageEvent, dir: str = "") -> str:
        if err := _require_admin(event):
            return err
        if err := _plugin_or_err(self.plugin):
            return err

        dir_path = str(dir or "").strip()
        result = await self.plugin.list_files(
            dir_path=dir_path,
            sum_only=True,
            recursive=True,
        )
        if isinstance(result, str):
            return _json_err(result)

        total = result.get("sum")
        if total is None:
            total = result.get("totalCount")
        return _json_ok({"dir": dir_path or "/", "sum": total})


@dataclass
class CloudImgGetFileTool(FunctionTool):
    plugin: Any = None
    name: str = "cloudimg_get_file"
    description: str = (
        "Resolve a CloudFlare ImgBed file path to a full public URL for download or delivery. "
        "After success, use AstrBot built-in tool send_message_to_user to deliver media: "
        'messages=[{type:"image"| "video"| "file", url:"<data.url>"}]. '
        "Path must be known (e.g. from cloudimg_list). Not a random-image tool."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": (
                        "File path or id from cloudimg_list name, e.g. photos/a.jpg "
                        "or file/xxx.jpg."
                    ),
                },
            },
            "required": ["path"],
        }
    )

    async def run(self, event: AstrMessageEvent, path: str = "") -> str:
        if err := _require_admin(event):
            return err
        if err := _plugin_or_err(self.plugin):
            return err

        raw_path = str(path or "").strip()
        if not raw_path:
            return _json_err("path 不能为空")

        if not (self.plugin.base_url or self.plugin.public_base_url):
            parsed = urlparse(raw_path)
            if not (parsed.scheme in {"http", "https"} and parsed.netloc):
                return _json_err("请先配置 base_url 或 public_base_url")

        url = self.plugin.resolve_media_display_url(raw_path)
        if not url:
            return _json_err(
                "无法解析为可访问的绝对 URL，请检查 path 与 base_url/public_base_url"
            )

        media_type = self.plugin.guess_media_type(raw_path or url)
        hint = (
            "To send this media to the user, call the built-in tool send_message_to_user "
            f'with messages=[{{"type":"{media_type}","url":"{url}"}}]. '
            "Use type video for videos, image for images, file otherwise."
        )
        return _json_ok(
            {
                "path": raw_path,
                "url": url,
                "media_type": media_type,
            },
            hint=hint,
        )


@dataclass
class CloudImgDeleteTool(FunctionTool):
    plugin: Any = None
    name: str = "cloudimg_delete"
    description: str = "Delete a single file on CloudFlare ImgBed by path. Admin and API Token required."
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path to delete, e.g. example/image.jpg",
                },
            },
            "required": ["path"],
        }
    )

    async def run(self, event: AstrMessageEvent, path: str = "") -> str:
        if err := _require_admin(event):
            return err
        if err := _plugin_or_err(self.plugin):
            return err

        raw_path = str(path or "").strip()
        if not raw_path:
            return _json_err("path 不能为空")

        result = await self.plugin.delete_path(raw_path, is_folder=False)
        if isinstance(result, str):
            return _json_err(result)
        if result.get("success") is False:
            return _json_err(
                str(result.get("error") or result.get("message") or "删除失败")
            )
        return _json_ok({"path": result.get("fileId") or raw_path, "deleted": True})


@dataclass
class CloudImgDeleteFolderTool(FunctionTool):
    plugin: Any = None
    name: str = "cloudimg_delete_folder"
    description: str = (
        "Recursively delete a folder on CloudFlare ImgBed. "
        "Asks the user for session confirmation before deleting. Admin and API Token required."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Folder path to delete recursively, e.g. example/folder",
                },
            },
            "required": ["path"],
        }
    )

    async def run(self, event: AstrMessageEvent, path: str = "") -> str:
        if err := _require_admin(event):
            return err
        if err := _plugin_or_err(self.plugin):
            return err

        raw_path = str(path or "").strip()
        if not raw_path:
            return _json_err("path 不能为空")

        prompt = (
            f"危险操作：将递归删除目录「{raw_path}」及其全部内容。\n"
            f"请回复「确认」继续，或「取消」中止。（60 秒内有效）"
        )
        try:
            await event.send(event.plain_result(prompt))
        except Exception:
            pass

        confirmed, msg = await self.plugin.wait_user_confirm(event, timeout=60)
        if not confirmed:
            return _json_err(msg or "已取消操作。")

        result = await self.plugin.delete_path(raw_path, is_folder=True)
        if isinstance(result, str):
            return _json_err(result)
        if result.get("success") is False:
            return _json_err(
                str(result.get("error") or result.get("message") or "删除失败")
            )

        deleted = result.get("deleted") or []
        failed = result.get("failed") or []
        return _json_ok(
            {
                "path": raw_path,
                "deleted_count": len(deleted) if isinstance(deleted, list) else deleted,
                "failed_count": len(failed) if isinstance(failed, list) else 0,
                "failed": failed[:20] if isinstance(failed, list) else failed,
            }
        )


@dataclass
class CloudImgUploadUrlTool(FunctionTool):
    plugin: Any = None
    name: str = "cloudimg_upload_url"
    description: str = (
        "Download a media file from an http(s) URL and upload it to a CloudFlare ImgBed folder. "
        "Admin required. Returns the public display URL when possible."
    )
    parameters: dict = field(
        default_factory=lambda: {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "Source http(s) URL of the image or video to upload.",
                },
                "folder": {
                    "type": "string",
                    "description": "Target upload folder name on the image bed.",
                },
            },
            "required": ["url", "folder"],
        }
    )

    async def run(
        self, event: AstrMessageEvent, url: str = "", folder: str = ""
    ) -> str:
        if err := _require_admin(event):
            return err
        if err := _plugin_or_err(self.plugin):
            return err

        source_url = str(url or "").strip()
        folder_name = str(folder or "").strip()
        if not source_url or not folder_name:
            return _json_err("url 与 folder 均为必填")

        (
            data,
            content_type,
            filename,
            dl_err,
        ) = await self.plugin.download_url_for_upload(
            source_url,
            max_bytes=20 * 1024 * 1024,
            timeout_sec=30.0,
        )
        if dl_err:
            return _json_err(dl_err)
        if not data:
            return _json_err("下载源文件失败")

        upload_name = filename or "upload.bin"
        result = await self.plugin.upload_to_cloudflare_imgbed(
            data, folder_name, upload_name
        )
        if not isinstance(result, str):
            return _json_err("上传失败")
        if not result.startswith("http"):
            return _json_err(result)

        media_type = self.plugin.guess_media_type(upload_name or result)
        if content_type:
            major = content_type.split(";", 1)[0].strip().lower().split("/", 1)[0]
            if major in {"image", "video"}:
                media_type = major

        return _json_ok(
            {
                "url": result,
                "folder": folder_name,
                "filename": upload_name,
                "media_type": media_type,
                "content_type": content_type,
            }
        )

import asyncio
import json
import os
import random
import string
from urllib.parse import urlparse

import aiohttp

from astrbot import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Video, Reply as ApiReply
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.message.components import Image, Plain
from astrbot.core.platform.astr_message_event import (
    AstrMessageEvent as BaseAstrMessageEvent,
)
from astrbot.core.utils.media_utils import MediaResolver

from .helpers.aiocqhttp import AiocqhttpMixin
from .helpers.telegram import TelegramMixin
from .helpers.utils import UtilsMixin


class CloudImgPlugin(Star, UtilsMixin, TelegramMixin, AiocqhttpMixin):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        # ── 图床连接 ──
        server = config.get("server", {})
        self.base_url = server.get("base_url", "")
        self.upload_api_url = self.base_url
        self.public_base_url = self._normalize_base_url(
            server.get("public_base_url", "")
        )
        self.auth_code = server.get("auth_code", "")
        self.api_token = server.get("api_token", "")
        self.random_path_suffix = "/random?form=text"

        # ── 上传设置 ──
        upload = config.get("upload", {})
        self.upload_admin_only = upload.get("admin_only", True)
        self.show_upload_link = upload.get("show_link", True)

        # ── 随机获取设置 ──
        randomizer = config.get("randomizer", {})
        self.local_random_type = randomizer.get("local_random_type", False)
        self.keyword_recent_media_limit = self._clamp_config_int(
            randomizer.get("dedupe_window", 0),
            min_value=0,
            max_value=None,
            default=0,
        )
        self.keyword_dedupe_retry_limit = self._clamp_config_int(
            randomizer.get("dedupe_retry", 2),
            min_value=0,
            max_value=10,
            default=2,
        )
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_CloudImg")

        os.makedirs(self.plugin_data_dir, exist_ok=True)

        self.keyword_folder_map = {}
        self.config_keyword_map: dict[str, dict] = {}
        self.effective_keyword_map: dict[str, dict] = {}
        self.mappings_file = os.path.join(self.plugin_data_dir, "keyword_mappings.json")
        self.load_keyword_mappings()
        self.load_config_keyword_templates()
        self.refresh_effective_keyword_map()
        self.keyword_recent_media_ids: dict[str, list[str]] = {}
        self.keyword_recent_media_kv_key = "keyword_recent_media_ids"

    # ==================== 配置文件管理 ====================

    def load_keyword_mappings(self):
        """从文件加载关键词-文件夹映射"""
        try:
            if os.path.exists(self.mappings_file):
                with open(self.mappings_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self.keyword_folder_map = {}
                    for keyword, value in data.items():
                        if isinstance(value, str):
                            self.keyword_folder_map[keyword] = {
                                "folder": value,
                                "content_type": "image,video",
                            }
                        else:
                            self.keyword_folder_map[keyword] = value
            else:
                self.keyword_folder_map = {}
        except Exception as e:
            logger.error(f"加载关键词映射失败: {e}")
            self.keyword_folder_map = {}

    def save_keyword_mappings(self):
        """保存关键词-文件夹映射到文件"""
        try:
            with open(self.mappings_file, "w", encoding="utf-8") as f:
                json.dump(self.keyword_folder_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存关键词映射失败: {e}")

    def _normalize_folder_names(self, folder_value: object) -> list[str]:
        if isinstance(folder_value, list):
            candidates = folder_value
        else:
            candidates = str(folder_value or "").replace("，", ",").split(",")

        folders: list[str] = []
        for item in candidates:
            folder = str(item or "").strip()
            if folder and folder not in folders:
                folders.append(folder)
        return folders

    def _normalize_content_type(self, content_type: object) -> str:
        normalized = str(content_type or "").strip().lower()
        if normalized in {"image", "video", "image,video"}:
            return normalized
        return "image,video"

    def _build_mapping_entry(
        self,
        folders: list[str],
        content_type: object = "image,video",
        source: str | None = None,
    ) -> dict | None:
        normalized_folders = self._normalize_folder_names(folders)
        if not normalized_folders:
            return None

        mapping = {
            "folder": ",".join(normalized_folders),
            "content_type": self._normalize_content_type(content_type),
        }
        if source:
            mapping["source"] = source
        return mapping

    def load_config_keyword_templates(self):
        self.config_keyword_map = {}
        templates = self.config.get("keyword_templates", [])
        if not isinstance(templates, list):
            return

        for item in templates:
            if not isinstance(item, dict):
                continue

            keywords = item.get("keywords", [])
            folders = item.get("folders", [])
            if not isinstance(keywords, list):
                continue

            mapping_entry = self._build_mapping_entry(
                folders=folders,
                content_type=item.get("content_type", "image,video"),
                source="template",
            )
            if not mapping_entry:
                continue

            for keyword in keywords:
                normalized_keyword = str(keyword or "").strip()
                if not normalized_keyword:
                    continue
                self.config_keyword_map[normalized_keyword] = dict(mapping_entry)

    def refresh_effective_keyword_map(self):
        effective_map: dict[str, dict] = {
            key: dict(value)
            for key, value in self.config_keyword_map.items()
            if isinstance(value, dict)
        }

        for key, value in self.keyword_folder_map.items():
            if isinstance(value, str):
                mapping_entry = self._build_mapping_entry(
                    folders=value,
                    content_type="image,video",
                    source="runtime",
                )
            elif isinstance(value, dict):
                mapping_entry = self._build_mapping_entry(
                    folders=value.get("folder", ""),
                    content_type=value.get("content_type", "image,video"),
                    source="runtime",
                )
            else:
                mapping_entry = None

            if mapping_entry:
                effective_map[key] = mapping_entry

        self.effective_keyword_map = effective_map

    # ==================== 核心功能方法 ====================

    async def initialize(self):
        await self._load_keyword_recent_media_ids()

    async def _load_keyword_recent_media_ids(self):
        self.keyword_recent_media_ids = {}
        if self.keyword_recent_media_limit <= 0:
            return

        try:
            stored = await self.get_kv_data(self.keyword_recent_media_kv_key, {})
        except Exception as e:
            logger.error(f"加载近期媒体去重缓存失败: {e}")
            return

        if not isinstance(stored, dict):
            return

        valid_keywords = set(self.effective_keyword_map.keys())
        valid_keywords.add("__img__")
        needs_persist = False

        for keyword, history in stored.items():
            if not isinstance(keyword, str) or not isinstance(history, list):
                needs_persist = True
                continue

            if keyword not in valid_keywords:
                needs_persist = True
                continue

            normalized_history = [
                str(item).strip()
                for item in history
                if isinstance(item, str) and str(item).strip()
            ]
            if self.keyword_recent_media_limit > 0:
                trimmed_history = normalized_history[-self.keyword_recent_media_limit :]
                if trimmed_history != normalized_history:
                    needs_persist = True
                normalized_history = trimmed_history
            if normalized_history:
                self.keyword_recent_media_ids[keyword] = normalized_history
            elif history:
                needs_persist = True

        if needs_persist:
            await self._persist_keyword_recent_media_ids()

    async def _persist_keyword_recent_media_ids(self):
        try:
            if self.keyword_recent_media_limit <= 0:
                await self.delete_kv_data(self.keyword_recent_media_kv_key)
            else:
                await self.put_kv_data(
                    self.keyword_recent_media_kv_key,
                    self.keyword_recent_media_ids,
                )
        except Exception as e:
            logger.error(f"保存近期媒体去重缓存失败: {e}")

    async def _remember_keyword_media_id(self, keyword: str, media_id: str):
        if self.keyword_recent_media_limit <= 0:
            return

        media_id = (media_id or "").strip()
        if not media_id:
            return

        history = self.keyword_recent_media_ids.get(keyword, [])
        history.append(media_id)
        if len(history) > self.keyword_recent_media_limit:
            history = history[-self.keyword_recent_media_limit :]

        self.keyword_recent_media_ids[keyword] = history
        await self._persist_keyword_recent_media_ids()

    def _build_random_media_chain(self, file_url: str) -> list:
        parsed_path = urlparse(file_url).path.lower()
        video_exts = (".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm")
        if any(parsed_path.endswith(ext) for ext in video_exts):
            return [Video.fromURL(file_url)]
        return [Image.fromURL(file_url)]

    async def _fetch_random_media_entry(
        self, folder_name: str = "", content_type: str = "image,video"
    ):
        if not self.base_url:
            return "请先配置 base_url。"

        final_content_type = content_type
        if self.local_random_type and "," in final_content_type:
            types = [t.strip() for t in final_content_type.split(",") if t.strip()]
            if len(types) > 1:
                final_content_type = random.choice(types)
                logger.debug(f"本地随机媒体类型：{final_content_type}")

        api_request_url = (
            f"{self.base_url}/random?form=text&content={final_content_type}"
        )
        if folder_name:
            api_request_url += f"&dir={folder_name}"

        ssl_context = aiohttp.TCPConnector(verify_ssl=False)
        async with aiohttp.ClientSession(connector=ssl_context) as session:
            try:
                async with session.get(api_request_url) as response:
                    if response.status != 200:
                        response_text = await response.text()
                        return self._handle_response_error(
                            response.status, response_text
                        )

                    relative_file_path = (await response.text()).strip()
                    if not relative_file_path:
                        logger.error("Random API returned an empty path")
                        return "请求失败：图床未返回媒体路径。"

                    file_url = f"{self.base_url}{relative_file_path}"
                    return {
                        "chain": self._build_random_media_chain(file_url),
                        "media_id": self._extract_media_id(relative_file_path),
                        "file_url": file_url,
                        "relative_file_path": relative_file_path,
                    }

            except Exception as e:
                logger.error(f"请求随机媒体失败: {e}")
                return "请求失败，请检查网络、base_url 与文件夹名。"

    async def get_random_file_from_keyword(
        self, keyword: str, folder_name: str = "", content_type: str = "image,video"
    ):
        if self.keyword_recent_media_limit <= 0:
            result = await self._fetch_random_media_entry(folder_name, content_type)
            if isinstance(result, dict):
                return result["chain"]
            return result

        history = self.keyword_recent_media_ids.get(keyword, [])
        dedupe_enabled = len(history) > 0
        retry_limit = self.keyword_dedupe_retry_limit if dedupe_enabled else 0

        best_fallback = None
        best_distance = -1

        # 首次 fetch
        result = await self._fetch_random_media_entry(folder_name, content_type)
        if not isinstance(result, dict):
            return result

        media_id = result.get("media_id", "")
        if not dedupe_enabled or not media_id or media_id not in history:
            await self._remember_keyword_media_id(keyword, media_id)
            return result["chain"]

        distance = self._history_distance(history, media_id)
        if distance > best_distance:
            best_distance = distance
            best_fallback = result

        # 显式重试循环
        for retry_count in range(1, retry_limit + 1):
            logger.debug(
                f"/{keyword} 命中近期媒体 ID，重试 {retry_count}/{retry_limit}"
            )
            result = await self._fetch_random_media_entry(folder_name, content_type)
            if not isinstance(result, dict):
                return result

            media_id = result.get("media_id", "")
            if not media_id or media_id not in history:
                await self._remember_keyword_media_id(keyword, media_id)
                return result["chain"]

            distance = self._history_distance(history, media_id)
            if distance > best_distance:
                best_distance = distance
                best_fallback = result

        selected = best_fallback or result
        if isinstance(selected, dict):
            selected_id = selected.get("media_id", "")
            await self._remember_keyword_media_id(keyword, selected_id)
            logger.debug(
                f"/{keyword} 达到重试上限，回退媒体 ID：{selected_id or '<empty>'}"
            )
            return selected["chain"]

        return "请求失败：未获取到可用媒体。"

    async def get_random_file_from_folder(
        self, folder_name: str = "", content_type: str = "image,video"
    ):
        """Get one random image/video from the target folder."""
        result = await self._fetch_random_media_entry(folder_name, content_type)
        if isinstance(result, dict):
            return result["chain"]
        return result

    async def _resolve_media_bytes(
        self,
        event: BaseAstrMessageEvent,
        kind: str,
        file_or_id: str | None,
        url: str | None,
        path: str | None,
    ) -> tuple[bytes | None, str | None]:
        """跨平台解析媒体字节数据。

        优先级：
        1) http(s) 直链下载（保持原有行为，不校验 SSL）
        2) 框架 MediaResolver：本地路径 / file:// / base64 / data URI
        3) Telegram 文件路径：拼接待下载 URL（client.base_url + /file/）
        4) OneBot file_id 兜底：call_action("get_file")

        Returns:
            (字节数据, 错误信息)。
        """
        source = (
            (url or "").strip() or (path or "").strip() or (file_or_id or "").strip()
        )
        if not source:
            return None, "无法获取媒体文件数据"

        # 1) http(s) 直链
        if source.startswith(("http://", "https://")):
            data = await self.download_image(source)
            if data:
                return data, None

        # 2) 框架 MediaResolver
        try:
            media_type = "video" if kind == "video" else "image"
            data = await MediaResolver(source, media_type=media_type).to_bytes()
            if data:
                return data, None
        except Exception as e:
            logger.debug(f"媒体解析失败(MediaResolver): kind={kind}, err={e}")

        # 3) Telegram 文件路径下载
        data = await self._telegram_download_file(event, source)
        if data:
            return data, None

        # 4) OneBot file_id 兜底
        data = await self._onebot_get_file(event, file_or_id or "")
        if data:
            return data, None

        return None, "无法获取媒体文件数据"

    async def upload_to_cloudflare_imgbed(
        self, image_data: bytes, folder_name: str, original_filename: str = None
    ) -> str | None:
        """上传文件到CloudFlare ImgBed"""
        if not self.upload_api_url:
            return "上传API地址未配置"

        upload_url = f"{self.upload_api_url}/upload"

        file_ext = ".jpg"
        content_type = "image/jpeg"

        ext_map = {
            ".jpg": ("image", "jpeg"),
            ".jpeg": ("image", "jpeg"),
            ".png": ("image", "png"),
            ".gif": ("image", "gif"),
            ".bmp": ("image", "bmp"),
            ".webp": ("image", "webp"),
            ".mp4": ("video", "mp4"),
            ".webm": ("video", "webm"),
            ".mov": ("video", "quicktime"),
            ".mkv": ("video", "x-matroska"),
            ".avi": ("video", "x-msvideo"),
            ".wmv": ("video", "x-ms-wmv"),
            ".flv": ("video", "x-flv"),
            ".m4v": ("video", "x-m4v"),
        }

        if original_filename:
            ext = os.path.splitext(original_filename)[1].lower()
            if ext in ext_map:
                major, minor = ext_map[ext]
                file_ext = ext
                content_type = f"{major}/{minor}"

        # 准备表单数据
        filename = f"upload{file_ext}"
        data = aiohttp.FormData()
        data.add_field("file", image_data, filename=filename, content_type=content_type)

        headers = {}
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"

        params = {}
        if not self.api_token and self.auth_code:
            params["authCode"] = self.auth_code
        params["serverCompress"] = "false"  # 禁用压缩
        params["uploadFolder"] = folder_name
        params["returnFormat"] = "full"  # 使用完整格式

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    upload_url, data=data, params=params, headers=headers
                ) as response:
                    response_text = await response.text()

                    if response.status != 200:
                        return self._handle_response_error(
                            response.status, response_text
                        )

                    try:
                        response_json = json.loads(
                            response_text
                        )  # Use json.loads since we already have response_text

                        if isinstance(response_json, list) and len(response_json) > 0:
                            src_path = response_json[0].get("src", "")
                            if src_path:
                                return self._build_upload_display_url(src_path)
                            else:
                                logger.error(
                                    f"上传成功但未找到链接，响应: {response_text}"
                                )
                                return "上传成功但未找到链接"
                        elif (
                            "data" in response_json
                            and isinstance(response_json["data"], list)
                            and len(response_json["data"]) > 0
                        ):
                            src_path = response_json["data"][0].get("src", "")
                            if src_path:
                                return self._build_upload_display_url(src_path)
                            else:
                                logger.error(
                                    f"上传成功但未找到链接，响应: {response_text}"
                                )
                                return "上传成功但未找到链接"
                        else:
                            logger.error(f"上传响应格式错误，响应: {response_text}")
                            return "上传响应格式错误"
                    except json.JSONDecodeError:
                        logger.error(
                            f"上传响应不是有效的JSON格式，响应: {response_text}"
                        )
                        return "上传响应不是有效的JSON格式"
        except Exception as e:
            logger.error(f"文件上传失败: err={type(e).__name__}")
            return "文件上传失败"

    async def _list_image_refs_from_event(
        self, event: BaseAstrMessageEvent
    ) -> list[dict]:
        messages = event.get_messages()

        reply_refs: list[dict] = []
        for seg in messages:
            if (
                isinstance(seg, ApiReply)
                and hasattr(seg, "chain")
                and isinstance(seg.chain, list)
            ):
                reply_refs.extend(self._collect_media_refs(seg.chain))

        if reply_refs:
            return reply_refs

        current_refs = self._collect_media_refs(messages)
        if current_refs:
            return current_refs

        # Telegram 兜底：从原始 Update 的 reply_to_message 提取媒体
        # （引用带说明文字的图片时，回复链会被替换为纯文本，无法直接看到图片）
        telegram_refs = await self._list_telegram_raw_reply_refs(event)
        if telegram_refs:
            return telegram_refs

        return []

    async def _read_media_bytes(
        self, event: AstrMessageEvent, media_ref: dict
    ) -> tuple[bytes | None, str | None, str | None]:
        url = media_ref.get("url")
        file_or_id = media_ref.get("file")
        path = media_ref.get("path")
        filename = media_ref.get("filename")
        kind = media_ref.get("kind")

        data, read_err = await self._resolve_media_bytes(
            event, kind, file_or_id, url, path
        )
        if data:
            return data, filename, None
        return None, filename, read_err or "无法获取媒体文件数据"

    # ==================== 命令处理方法 ====================

    @filter.command("img")
    async def get_image(self, event: AstrMessageEvent):
        """获取随机图片或视频"""
        result = await self.get_random_file_from_keyword("__img__", "", "image,video")
        if isinstance(result, list):
            yield event.chain_result(result)
        else:
            yield event.plain_result(result)

    @filter.command("上传", alias={"upload"})
    async def upload_image(
        self, event: AstrMessageEvent, folder_name: str = None, index_spec: str = None
    ):
        """上传媒体到CloudFlare ImgBed"""
        logger.info(f"/上传: folder={folder_name}, index_spec={index_spec}")
        if self.upload_admin_only:
            if not event.is_admin():
                yield event.plain_result("上传功能仅限管理员使用")
                return

        if not folder_name:
            yield event.plain_result("请指定上传的文件夹名，格式：/上传 文件夹名")
            return

        # 检测文件夹名是否包含英文标点
        if any(char in string.punctuation for char in folder_name):
            yield event.plain_result(f"文件夹名 {folder_name} 不允许包含英文标点")
            return

        forward_id = None
        found_json_forward = False
        if event.get_platform_name() == "aiocqhttp":
            forward_id, found_json_forward = await self._try_get_forward_id(event)
        if forward_id:
            media_refs = await self._list_media_refs_from_forward(event, forward_id)
            if not media_refs:
                yield event.plain_result("合并聊天记录中未找到可上传的图片/视频")
                return

            indexes, err = self._parse_index_spec(
                index_spec,
                len(media_refs),
                label="媒体文件",
                empty_msg="合并聊天记录中未找到图片/视频",
            )
            if err:
                yield event.plain_result(err)
                return

            logger.info(
                f"合并聊天记录上传开始: folder={folder_name}, total={len(media_refs)}, selected={len(indexes)}"
            )

            semaphore = asyncio.Semaphore(3)

            async def upload_one(i: int):
                ref = media_refs[i - 1]
                async with semaphore:
                    data, filename, read_err = await self._read_media_bytes(event, ref)
                    if read_err:
                        logger.warning(
                            f"合并聊天记录媒体读取失败: index={i}, err={read_err}"
                        )
                        return {
                            "index": i,
                            "ok": False,
                            "error": read_err,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    result = await self.upload_to_cloudflare_imgbed(
                        data, folder_name, filename
                    )
                    if isinstance(result, str) and result.startswith("http"):
                        return {
                            "index": i,
                            "ok": True,
                            "url": result,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    err_msg = result or "上传失败"
                    logger.warning(
                        f"合并聊天记录媒体上传失败: index={i}, err={err_msg}"
                    )
                    return {
                        "index": i,
                        "ok": False,
                        "error": err_msg,
                        "filename": filename,
                        "kind": ref.get("kind"),
                    }

            results = await asyncio.gather(*(upload_one(i) for i in indexes))

            ok_results = [r for r in results if r.get("ok")]
            fail_results = [r for r in results if not r.get("ok")]

            logger.info(
                f"合并聊天记录上传结束: folder={folder_name}, success={len(ok_results)}, fail={len(fail_results)}"
            )

            yield event.plain_result(self._build_upload_reply("上传完成", results))
            return

        if found_json_forward:
            yield event.plain_result(
                "检测到合并聊天记录（JSON 格式），当前无法提取其中的图片/视频，请发送可解析的合并转发消息"
            )
            return

        media_refs = await self._list_image_refs_from_event(event)
        if media_refs:
            indexes, err = self._parse_index_spec(
                index_spec,
                len(media_refs),
                label="媒体文件",
                empty_msg="未找到可上传的图片/视频",
            )
            if err:
                yield event.plain_result(err)
                return

            logger.info(
                f"媒体上传开始: folder={folder_name}, total={len(media_refs)}, selected={len(indexes)}"
            )

            semaphore = asyncio.Semaphore(3)

            async def upload_one(i: int):
                ref = media_refs[i - 1]
                async with semaphore:
                    data, filename, read_err = await self._read_media_bytes(event, ref)
                    if read_err:
                        logger.warning(f"媒体读取失败: index={i}, err={read_err}")
                        return {
                            "index": i,
                            "ok": False,
                            "error": read_err,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    result = await self.upload_to_cloudflare_imgbed(
                        data, folder_name, filename
                    )
                    if isinstance(result, str) and result.startswith("http"):
                        return {
                            "index": i,
                            "ok": True,
                            "url": result,
                            "filename": filename,
                            "kind": ref.get("kind"),
                        }
                    err_msg = result or "上传失败"
                    logger.warning(f"媒体上传失败: index={i}, err={err_msg}")
                    return {
                        "index": i,
                        "ok": False,
                        "error": err_msg,
                        "filename": filename,
                        "kind": ref.get("kind"),
                    }

            results = await asyncio.gather(*(upload_one(i) for i in indexes))
            ok_results = [r for r in results if r.get("ok")]
            fail_results = [r for r in results if not r.get("ok")]

            logger.info(
                f"媒体上传结束: folder={folder_name}, success={len(ok_results)}, fail={len(fail_results)}"
            )

            yield event.plain_result(self._build_upload_reply("上传完成", results))
            return

        yield event.plain_result("未找到引用消息中的图片/视频")

    @filter.command("imglink")
    async def link_keyword_to_folder(
        self,
        event: AstrMessageEvent,
        keyword: str = None,
        folder_name: str = None,
        content_type: str = None,
    ):
        """关联关键词和文件夹"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        if not keyword:
            if not self.effective_keyword_map:
                yield event.plain_result("当前没有已设置的关键词映射。")
                return

            result = "当前关键词映射列表：\n"
            for key, mapping in self.effective_keyword_map.items():
                if isinstance(mapping, dict):
                    folder = mapping.get("folder", "")
                    ctype = mapping.get("content_type", "image,video")
                    source = mapping.get("source", "runtime")
                    source_text = "指令" if source == "runtime" else "模板"
                    result += f"  /{key} -> {folder} ({ctype}) [{source_text}]\n"
                else:
                    result += f"  /{key} -> {mapping}\n"
            result += "\n使用 /imglink 关键词 文件夹名 [内容类型] 来添加新映射。\n内容类型可选: img(图片), vid(视频), 未指定则为全部"
            yield event.plain_result(result.strip())
            return

        if not folder_name:
            yield event.plain_result(
                "参数错误！格式：/imglink 关键词 文件夹名 [内容类型]\n例如：/imglink test test 或 /imglink test test,test2 img\n内容类型可选: img(图片), vid(视频), 未指定则为全部\n\n不带参数使用 /imglink 可查看所有映射。"
            )
            return

        if content_type:
            if content_type.lower() in ["img", "image"]:
                final_content_type = "image"
            elif content_type.lower() in ["vid", "video"]:
                final_content_type = "video"
            else:
                yield event.plain_result(
                    "内容类型参数错误！可选值: img(图片), vid(视频)"
                )
                return
        else:
            final_content_type = "image,video"

        self.keyword_folder_map[keyword] = {
            "folder": folder_name,
            "content_type": final_content_type,
        }
        self.save_keyword_mappings()
        self.refresh_effective_keyword_map()

        content_type_desc = {
            "image": "图片",
            "video": "视频",
            "image,video": "图片或视频",
        }
        desc = content_type_desc.get(final_content_type, "图片或视频")

        yield event.plain_result(
            f"已将关键词 '{keyword}' 与文件夹 '{folder_name}' 关联（{desc}），现在发送 /{keyword} 即可获取其中随机一个文件夹的随机{desc}。"
        )

    @filter.command("imgunlink")
    async def unlink_keyword(
        self,
        event: AstrMessageEvent,
        keyword: str = None,
        folders_to_remove: str = None,
    ):
        """取消关键词关联或删除部分文件夹"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        if not keyword:
            yield event.plain_result(
                "参数错误！格式：/imgunlink 关键词 [文件夹名]\n例如：/imgunlink test 或 /imgunlink test 3cy,test1"
            )
            return

        if keyword not in self.keyword_folder_map:
            if keyword in self.config_keyword_map:
                yield event.plain_result(
                    f"关键词 '{keyword}' 来自模板配置，不能通过 /imgunlink 删除，请到插件配置中修改。"
                )
            else:
                yield event.plain_result(f"关键词 '{keyword}' 不存在映射。")
            return

        if not folders_to_remove:
            # 删除整个关键词映射
            del self.keyword_folder_map[keyword]
            self.save_keyword_mappings()
            self.refresh_effective_keyword_map()
            if keyword not in self.effective_keyword_map:
                self.keyword_recent_media_ids.pop(keyword, None)
            await self._persist_keyword_recent_media_ids()
            if keyword in self.config_keyword_map:
                yield event.plain_result(
                    f"已删除关键词 '{keyword}' 的指令映射，当前将回退为模板配置。"
                )
            else:
                yield event.plain_result(f"已完全删除关键词 '{keyword}' 的所有映射。")
            return

        # 删除指定的文件夹
        mapping = self.keyword_folder_map[keyword]
        if isinstance(mapping, dict):
            current_folders_str = mapping.get("folder", "")
        else:
            current_folders_str = mapping

        current_folders = [
            f.strip()
            for f in current_folders_str.replace("，", ",").split(",")
            if f.strip()
        ]
        remove_list = [
            f.strip()
            for f in folders_to_remove.replace("，", ",").split(",")
            if f.strip()
        ]

        new_folders = []
        removed_count = 0
        not_found = []

        for f in current_folders:
            if f in remove_list:
                removed_count += 1
            else:
                new_folders.append(f)

        for f in remove_list:
            if f not in current_folders:
                not_found.append(f)

        if removed_count == 0:
            yield event.plain_result(
                f"关键词 '{keyword}' 的映射中未找到指定的文件夹: {', '.join(not_found)}"
            )
            return

        if not new_folders:
            # 如果删完了，直接删除关键词
            del self.keyword_folder_map[keyword]
            self.refresh_effective_keyword_map()
            if keyword not in self.effective_keyword_map:
                self.keyword_recent_media_ids.pop(keyword, None)
                msg = f"已删除关键词 '{keyword}' 关联的所有文件夹，该关键词已失效。"
            else:
                msg = f"已删除关键词 '{keyword}' 的指令映射，当前将回退为模板配置。"
        else:
            new_folder_str = ",".join(new_folders)
            if isinstance(mapping, dict):
                mapping["folder"] = new_folder_str
            else:
                self.keyword_folder_map[keyword] = new_folder_str
            self.refresh_effective_keyword_map()
            msg = f"已从关键词 '{keyword}' 中删除 {removed_count} 个文件夹。当前关联：{new_folder_str}"
            if not_found:
                msg += f"\n注：未找到以下文件夹：{', '.join(not_found)}"

        self.save_keyword_mappings()
        if keyword not in self.effective_keyword_map:
            await self._persist_keyword_recent_media_ids()
        yield event.plain_result(msg)

    # ==================== 动态命令处理 ====================
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def handle_dynamic_commands(self, event: AstrMessageEvent):
        """处理群聊和私聊消息中的动态命令"""
        async for result in self._process_dynamic_command(event):
            yield result
            event.stop_event()

    async def _process_dynamic_command(self, event: AstrMessageEvent):
        """处理动态命令"""
        message_text = ""
        for seg in event.get_messages():
            if isinstance(seg, Plain):
                message_text = seg.text.strip()
                break

        if not message_text:
            return

        is_private = event.get_group_id() is None
        if not is_private and not getattr(event, "is_at_or_wake_command", False):
            return

        # 去除唤醒前缀（如有），提取命令内容
        try:
            cfg = self.context.get_config(event.unified_msg_origin)
        except Exception:
            cfg = self.context.get_config()
        wake_prefixes = cfg.get("wake_prefix", [])
        if isinstance(wake_prefixes, str):
            wake_prefixes = [wake_prefixes]
        for prefix in wake_prefixes:
            if isinstance(prefix, str) and prefix and message_text.startswith(prefix):
                message_text = message_text[len(prefix) :].strip()
                break

        parts = [p for p in message_text.split() if p]
        if not parts:
            return

        keyword = parts[0]
        force_content_type: str | None = None
        if len(parts) >= 2:
            type_arg = parts[1].lower()
            if type_arg in ["v", "vid", "video"]:
                force_content_type = "video"
            elif type_arg in ["i", "img", "image"]:
                force_content_type = "image"

        if keyword in self.effective_keyword_map:
            mapping = self.effective_keyword_map[keyword]
            if isinstance(mapping, dict):
                folder_name_raw = mapping.get("folder", "")
                content_type = mapping.get("content_type", "image,video")
            else:
                folder_name_raw = mapping
                content_type = "image,video"

            if force_content_type:
                content_type = force_content_type

            folders = [
                f.strip()
                for f in folder_name_raw.replace("，", ",").split(",")
                if f.strip()
            ]
            if not folders:
                return

            folder_name = random.choice(folders)
            logger.debug(
                f"动态命令 /{keyword} 触发，从 {folders} 中随机选择文件夹: {folder_name}, content_type={content_type}"
            )

            result = await self.get_random_file_from_keyword(
                keyword, folder_name, content_type
            )

            if isinstance(result, list):
                yield event.chain_result(result)
            else:
                yield event.plain_result(result)

    async def terminate(self):
        """插件销毁时的清理工作"""
        await self._persist_keyword_recent_media_ids()
        logger.info("CF图床助手已卸载")

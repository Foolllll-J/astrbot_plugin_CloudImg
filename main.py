from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, StarTools, register
import asyncio
import aiohttp
import os
import json
import re
import string
from urllib.parse import urlparse
from astrbot import logger
from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.platform.astr_message_event import AstrMessageEvent as BaseAstrMessageEvent


@register("astrbot_plugin_CloudImg", "Foolllll", "获取随机媒体及上传图片/视频到CloudFlare图床。使用指令可获取随机媒体，使用 /上传 文件夹名 回复图片或视频消息进行上传。", "1.1", "https://github.com/Foolllll-J/astrbot_plugin_CloudImg")
class CloudImgPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.base_url = config.get("base_url", "")
        self.upload_api_url = self.base_url
        self.auth_code = config.get("auth_code", "")
        self.random_path_suffix = "/random?form=text"
        self.upload_admin_only = config.get("upload_admin_only", True)
        self.show_upload_link = config.get("show_upload_link", True)
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_CloudImg")

        os.makedirs(self.plugin_data_dir, exist_ok=True)

        self.keyword_folder_map = {}
        self.mappings_file = os.path.join(self.plugin_data_dir, "keyword_mappings.json")
        self.load_keyword_mappings()

    # ==================== 配置文件管理 ====================

    def load_keyword_mappings(self):
        """从文件加载关键词-文件夹映射"""
        try:
            if os.path.exists(self.mappings_file):
                with open(self.mappings_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.keyword_folder_map = {}
                    for keyword, value in data.items():
                        if isinstance(value, str):
                            self.keyword_folder_map[keyword] = {"folder": value, "content_type": "image,video"}
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
            with open(self.mappings_file, 'w', encoding='utf-8') as f:
                json.dump(self.keyword_folder_map, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存关键词映射失败: {e}")

    # ==================== 核心功能方法 ====================

    async def get_random_file_from_folder(self, folder_name: str = "", content_type: str = "image,video"):
        """获取指定文件夹中的随机文件（图片或视频）

        Args:
            folder_name: 文件夹名称，空字符串表示根目录
            content_type: 内容类型，可选 "image", "video", "image,video"
        """
        if not self.base_url:
            return "\n请先在配置文件中设置图床的基础地址 (base_url)"

        api_request_url = f"{self.base_url}/random?form=text&content={content_type}"
        if folder_name:
            api_request_url += f"&dir={folder_name}"

        # 创建不验证SSL的连接上下文
        ssl_context = aiohttp.TCPConnector(verify_ssl=False)
        async with aiohttp.ClientSession(connector=ssl_context) as session:
            try:
                async with session.get(api_request_url) as response:
                    # 检查HTTP状态码
                    if response.status != 200:
                        return f"\nAPI请求失败，状态码: {response.status}"

                    relative_file_path = await response.text()
                    relative_file_path = relative_file_path.strip()

                    file_url = f"{self.base_url}{relative_file_path}"

                    # 根据文件扩展名判断是图片还是视频
                    if any(file_url.lower().endswith(ext) for ext in ['.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm']):
                        # 视频文件
                        from astrbot.api.message_components import Video
                        chain = [
                            Video.fromURL(file_url)
                        ]
                    else:
                        # 图片文件
                        chain = [
                            Image.fromURL(file_url)
                        ]

                    return chain

            except Exception as e:
                return f"\n请求图床失败: {str(e)}。请检查 base_url 和文件夹名是否正确。"

    async def download_image(self, url: str) -> bytes | None:
        """下载图片并返回字节数据"""
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    resp.raise_for_status()
                    return await resp.read()
        except Exception as e:
            logger.error(f"图片下载失败: url={self._redact_url_for_log(url)}, err={type(e).__name__}")
            return None

    async def get_first_image(self, event: BaseAstrMessageEvent) -> bytes | None:
        """获取消息里的第一张图并返回字节数据。
        顺序：
        1) 引用消息中的图片
        2) 当前消息中的图片
        找不到返回 None。
        """
        # 检查引用消息中的图片
        messages = event.get_messages()

        for seg in messages:
            if isinstance(seg, Reply):
                if hasattr(seg, 'chain') and isinstance(seg.chain, list):
                    for reply_seg in seg.chain:
                        if isinstance(reply_seg, Image):
                            if hasattr(reply_seg, 'url') and reply_seg.url:
                                return await self.download_image(reply_seg.url)
                            if hasattr(reply_seg, 'file') and reply_seg.file:
                                if os.path.exists(reply_seg.file):
                                    with open(reply_seg.file, 'rb') as f:
                                        return f.read()

        # 检查当前消息中的图片
        for seg in messages:
            if isinstance(seg, Image):
                if hasattr(seg, 'url') and seg.url:
                    return await self.download_image(seg.url)
                if hasattr(seg, 'file') and seg.file:
                    if os.path.exists(seg.file):
                        with open(seg.file, 'rb') as f:
                            return f.read()

        return None

    async def get_first_video_from_reply(self, event: BaseAstrMessageEvent) -> tuple[bytes | None, str | None]:
        """从引用消息中获取第一个视频并返回(字节数据, 原始文件名)。"""
        from astrbot.api.message_components import Reply, Video

        messages = event.get_messages()

        for seg in messages:
            if isinstance(seg, Reply):
                if hasattr(seg, 'chain') and isinstance(seg.chain, list):
                    for item in seg.chain:
                        if isinstance(item, Video):
                            original_filename = getattr(item, 'file', None)
                            if hasattr(item, 'url') and item.url:
                                return await self.download_image(item.url), original_filename
                            if hasattr(item, 'file') and item.file:
                                try:
                                    if hasattr(event, 'bot') and hasattr(event.bot, 'api'):
                                        result = await event.bot.api.call_action('get_file', file_id=item.file)
                                        if result and 'url' in result:
                                            video_url = result['url']
                                            video_data = await self.download_image(video_url)
                                            return video_data, original_filename
                                except Exception:
                                    pass
                                return None, None

        return None, None

    async def upload_to_cloudflare_imgbed(self, image_data: bytes, folder_name: str, original_filename: str = None) -> str | None:
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
        data.add_field('file', image_data, filename=filename, content_type=content_type)

        params = {}
        if self.auth_code:
            params['authCode'] = self.auth_code
        params['serverCompress'] = 'false'  # 禁用压缩
        params['uploadFolder'] = folder_name
        params['returnFormat'] = 'full'  # 使用完整格式

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(upload_url, data=data, params=params) as response:
                    response_text = await response.text()

                    if response.status != 200:
                        return f"上传失败，状态码: {response.status}, 响应: {response_text}"

                    import json
                    try:
                        response_json = await response.json()

                        if isinstance(response_json, list) and len(response_json) > 0:
                            src_path = response_json[0].get('src', '')
                            if src_path:
                                return src_path
                            else:
                                return f"上传成功但未找到链接，响应: {response_text}"
                        elif 'data' in response_json and isinstance(response_json['data'], list) and len(response_json['data']) > 0:
                            src_path = response_json['data'][0].get('src', '')
                            if src_path:
                                return src_path
                            else:
                                return f"上传成功但未找到链接，响应: {response_text}"
                        else:
                            return f"上传响应格式错误，响应: {response_text}"
                    except json.JSONDecodeError:
                        return f"上传响应不是有效的JSON格式，响应: {response_text}"
        except Exception as e:
            logger.error(f"文件上传失败: err={type(e).__name__}")
            return "文件上传失败"

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

        if isinstance(spec, int):
            spec = str(spec)
        elif not isinstance(spec, str):
            return None, "序号参数格式错误，应为 1, 1-3 或 1,3,5"

        spec = spec.strip()
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
            
            # 处理单数字
            if re.fullmatch(r"\d+", part):
                idx = int(part)
                if idx < 1 or idx > total:
                    return None, f"序号 {idx} 超出范围：当前共有 {total} 个{label}"
                indices.add(idx)
            # 处理范围
            elif m := re.fullmatch(r"(\d+)-(\d+)", part):
                start = int(m.group(1))
                end = int(m.group(2))
                if start < 1 or end < 1 or start > end:
                    return None, f"序号范围 {part} 格式错误，应为 1-3 这种格式"
                if end > total:
                    return None, f"序号 {end} 超出范围：当前共有 {total} 个{label}"
                for i in range(start, end + 1):
                    indices.add(i)
            else:
                return None, f"无法解析序号参数: {part}"

        if not indices:
            return list(range(1, total + 1)), None

        return sorted(list(indices)), None

    async def _list_image_refs_from_event(self, event: BaseAstrMessageEvent) -> list[dict]:
        messages = event.get_messages()

        reply_refs: list[dict] = []
        for seg in messages:
            if isinstance(seg, Reply) and hasattr(seg, "chain") and isinstance(seg.chain, list):
                for inner in seg.chain:
                    if isinstance(inner, Image):
                        url = getattr(inner, "url", None)
                        file_or_id = getattr(inner, "file", None)
                        filename = None
                        if isinstance(file_or_id, str) and file_or_id:
                            base = os.path.basename(file_or_id)
                            if base and "." in base:
                                filename = base
                        if not filename and isinstance(url, str) and url:
                            filename = self._guess_filename_from_url(url, ".jpg")
                        reply_refs.append(
                            {
                                "kind": "image",
                                "url": url,
                                "file": file_or_id,
                                "filename": filename or "upload.jpg",
                            }
                        )

        if reply_refs:
            logger.debug(f"检测到回复消息多图: count={len(reply_refs)}")
            return [r for r in reply_refs if r.get("url") or r.get("file")]

        current_refs: list[dict] = []
        for seg in messages:
            if isinstance(seg, Image):
                url = getattr(seg, "url", None)
                file_or_id = getattr(seg, "file", None)
                filename = None
                if isinstance(file_or_id, str) and file_or_id:
                    base = os.path.basename(file_or_id)
                    if base and "." in base:
                        filename = base
                if not filename and isinstance(url, str) and url:
                    filename = self._guess_filename_from_url(url, ".jpg")
                current_refs.append(
                    {
                        "kind": "image",
                        "url": url,
                        "file": file_or_id,
                        "filename": filename or "upload.jpg",
                    }
                )

        if current_refs:
            logger.debug(f"检测到当前消息多图: count={len(current_refs)}")
        return [r for r in current_refs if r.get("url") or r.get("file")]

    async def _try_get_forward_id(self, event: AstrMessageEvent) -> tuple[str | None, bool]:
        forward_id = None
        found_json_forward = False
        msg_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        logger.debug(f"/上传 合并检测开始: message_id={msg_id}")

        def extract_forward_id_from_multimsg_json(obj: object) -> str | None:
            if not isinstance(obj, dict):
                return None
            if obj.get("app") != "com.tencent.multimsg":
                return None
            if obj.get("config", {}).get("forward") != 1:
                return None

            meta = obj.get("meta", {})
            if not isinstance(meta, dict):
                return None

            def pick(v: object) -> str | None:
                if isinstance(v, str) and v.strip():
                    return v.strip()
                if isinstance(v, int):
                    return str(v)
                return None

            target_keys = {"resid", "id", "forward_id"}

            def deep_find(o: object, depth: int) -> str | None:
                if depth <= 0:
                    return None
                if isinstance(o, dict):
                    for k, v in o.items():
                        if isinstance(k, str) and k in target_keys:
                            picked = pick(v)
                            if picked:
                                return picked
                        found = deep_find(v, depth - 1)
                        if found:
                            return found
                elif isinstance(o, list):
                    for item in o:
                        found = deep_find(item, depth - 1)
                        if found:
                            return found
                return None

            found_id = deep_find(meta.get("detail"), 4)
            if found_id:
                return found_id

            return deep_find(meta, 4)

            return None

        reply_id = None
        message_list = getattr(getattr(event, "message_obj", None), "message", None)
        logger.debug(f"/上传 合并检测: message_list_type={type(message_list).__name__}")
        if isinstance(message_list, list):
            logger.debug(f"/上传 合并检测: message_list_count={len(message_list)}")
            for seg in message_list:
                if forward_id is None and seg.__class__.__name__ == "Forward" and hasattr(seg, "id"):
                    forward_id = seg.id
                    logger.debug(f"检测到合并转发(直接消息段): forward_id={forward_id}")
                    break
                if seg.__class__.__name__ == "Reply" and hasattr(seg, "id"):
                    reply_id = seg.id

        if forward_id is not None:
            logger.debug(f"/上传 合并检测结束(直接命中): forward_id={forward_id}")
            return forward_id, found_json_forward

        for seg in event.get_messages():
            if isinstance(seg, Reply) and hasattr(seg, "chain") and isinstance(seg.chain, list):
                for inner in seg.chain:
                    if inner.__class__.__name__ == "Forward" and hasattr(inner, "id"):
                        logger.debug(f"检测到合并转发(Reply.chain): forward_id={inner.id}")
                        return inner.id, found_json_forward

        if reply_id and hasattr(event, "bot") and hasattr(event.bot, "api"):
            try:
                logger.debug(f"尝试从被回复消息解析合并转发: reply_id={reply_id}")
                original_msg = await event.bot.api.call_action("get_msg", message_id=reply_id)
                original_chain = original_msg.get("message") if isinstance(original_msg, dict) else None
                if isinstance(original_chain, list):
                    logger.debug(f"get_msg 返回消息段: count={len(original_chain)}")
                else:
                    logger.debug("get_msg 返回消息段为空或结构异常")
                if isinstance(original_chain, list):
                    for segment in original_chain:
                        if not isinstance(segment, dict):
                            continue
                        seg_type = segment.get("type")
                        if seg_type == "forward":
                            forward_id = segment.get("data", {}).get("id")
                            if forward_id:
                                logger.debug(f"检测到合并转发(get_msg->forward): forward_id={forward_id}")
                                break
                        if seg_type == "json":
                            try:
                                inner_data_str = segment.get("data", {}).get("data")
                                if inner_data_str:
                                    inner_data_str = inner_data_str.replace("&#44;", ",")
                                    inner_json = json.loads(inner_data_str)
                                    json_forward_id = extract_forward_id_from_multimsg_json(inner_json)
                                    if json_forward_id:
                                        forward_id = json_forward_id
                                        found_json_forward = True
                                        logger.debug(f"从 JSON 合并聊天记录提取到 forward_id: {forward_id}")
                                        break
                                    if inner_json.get("app") == "com.tencent.multimsg" and inner_json.get("config", {}).get("forward") == 1:
                                        found_json_forward = True
                                        logger.debug("检测到 JSON 合并聊天记录，但未解析到 forward_id")
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"获取被回复消息详情失败: {e}")

        logger.debug(f"/上传 合并检测结束: forward_id={forward_id}, found_json_forward={found_json_forward}, reply_id={reply_id}")
        return forward_id, found_json_forward

    async def _list_media_refs_from_forward(self, event: AstrMessageEvent, forward_id: str) -> list[dict]:
        if not hasattr(event, "bot") or not hasattr(event.bot, "api"):
            return []

        try:
            logger.debug(f"开始拉取合并转发详情: forward_id={forward_id}")
            forward_data = await event.bot.api.call_action("get_forward_msg", id=forward_id)
        except Exception as e:
            logger.warning(f"调用 get_forward_msg API 失败 (ID: {forward_id}): {e}")
            return []

        messages = forward_data.get("messages") if isinstance(forward_data, dict) else None
        if not isinstance(messages, list):
            logger.debug("get_forward_msg 返回 messages 为空或结构异常")
            return []

        media_refs: list[dict] = []

        async def walk_nodes(nodes: list[dict]):
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                raw_content = node.get("message") or node.get("content", [])
                content_chain = []
                if isinstance(raw_content, str):
                    try:
                        parsed = json.loads(raw_content)
                        if isinstance(parsed, list):
                            content_chain = parsed
                    except Exception:
                        content_chain = []
                elif isinstance(raw_content, list):
                    content_chain = raw_content

                for segment in content_chain:
                    if not isinstance(segment, dict):
                        continue
                    seg_type = segment.get("type")
                    seg_data = segment.get("data", {}) or {}

                    if seg_type == "image":
                        url = seg_data.get("url")
                        file_or_id = seg_data.get("file")
                        filename = seg_data.get("filename") or seg_data.get("name")
                        if not filename and isinstance(url, str) and url:
                            filename = self._guess_filename_from_url(url, ".jpg")
                        media_refs.append(
                            {
                                "kind": "image",
                                "url": url,
                                "file": file_or_id,
                                "filename": filename,
                            }
                        )
                    elif seg_type == "video":
                        url = seg_data.get("url")
                        file_or_id = seg_data.get("file")
                        filename = seg_data.get("filename") or seg_data.get("name")
                        if not filename and isinstance(url, str) and url:
                            filename = self._guess_filename_from_url(url, ".mp4")
                        if not filename and isinstance(file_or_id, str) and file_or_id:
                            filename = file_or_id
                        media_refs.append(
                            {
                                "kind": "video",
                                "url": url,
                                "file": file_or_id,
                                "filename": filename or "upload.mp4",
                            }
                        )
                    elif seg_type == "forward":
                        nested = seg_data.get("content")
                        if isinstance(nested, list):
                            await walk_nodes(nested)

        await walk_nodes(messages)

        filtered = []
        for ref in media_refs:
            if ref.get("url") or ref.get("file"):
                filtered.append(ref)

        img_count = sum(1 for r in filtered if r.get("kind") == "image")
        vid_count = sum(1 for r in filtered if r.get("kind") == "video")
        logger.debug(f"合并转发媒体解析完成: total={len(filtered)}, images={img_count}, videos={vid_count}")
        return filtered

    async def _read_media_bytes(self, event: AstrMessageEvent, media_ref: dict) -> tuple[bytes | None, str | None, str | None]:
        url = media_ref.get("url")
        file_or_id = media_ref.get("file")
        filename = media_ref.get("filename")
        kind = media_ref.get("kind")

        if isinstance(url, str) and url.startswith(("http://", "https://")):
            logger.debug(
                f"读取媒体(直链): kind={kind}, filename={filename}, url={self._redact_url_for_log(url)}"
            )
            data = await self.download_image(url)
            if not data:
                return None, filename, "下载失败"
            return data, filename, None

        if isinstance(file_or_id, str) and file_or_id:
            if os.path.exists(file_or_id):
                try:
                    logger.debug(f"读取媒体(本地文件): kind={kind}, filename={filename}, path={file_or_id}")
                    with open(file_or_id, "rb") as f:
                        return f.read(), filename, None
                except Exception as e:
                    return None, filename, f"读取文件失败: {e}"

            if hasattr(event, "bot") and hasattr(event.bot, "api"):
                try:
                    logger.debug(f"读取媒体(get_file): kind={kind}, filename={filename}, file_id={file_or_id}")
                    result = await event.bot.api.call_action("get_file", file_id=file_or_id)
                    if isinstance(result, dict) and result.get("url"):
                        data = await self.download_image(result["url"])
                        if not data:
                            return None, filename, "下载失败"
                        if not filename:
                            if kind == "video":
                                filename = self._guess_filename_from_url(result["url"], ".mp4")
                            else:
                                filename = self._guess_filename_from_url(result["url"], ".jpg")
                        return data, filename, None
                except Exception as e:
                    return None, filename, f"获取文件失败: {e}"

        return None, filename, "无法获取媒体文件数据"

    # ==================== 命令处理方法 ====================

    @filter.command("img")
    async def get_image(self, event: AstrMessageEvent):
        """获取随机图片或视频"""
        result = await self.get_random_file_from_folder("", "image,video")
        if isinstance(result, list):
            yield event.chain_result(result)
        else:
            yield event.plain_result(result)

    @filter.command("上传", alias={"upload"})
    async def upload_image(self, event: AstrMessageEvent, folder_name: str = None, index_spec: str = None):
        """上传图片到CloudFlare ImgBed"""
        msg_id = getattr(getattr(event, "message_obj", None), "message_id", None)
        logger.info(f"/上传: folder={folder_name}, index_spec={index_spec}")
        logger.debug(f"/上传 message_id={msg_id}")
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
        
        forward_id, found_json_forward = await self._try_get_forward_id(event)
        logger.debug(f"/上传 检测结果: forward_id={forward_id}, found_json_forward={found_json_forward}")
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

            logger.info(f"合并聊天记录上传开始: folder={folder_name}, total={len(media_refs)}, selected={len(indexes)}")
            logger.debug(f"合并聊天记录上传 indexes={indexes}")

            semaphore = asyncio.Semaphore(3)

            async def upload_one(i: int):
                ref = media_refs[i - 1]
                async with semaphore:
                    logger.debug(
                        f"合并聊天记录上传任务开始: index={i}, kind={ref.get('kind')}, filename={ref.get('filename')}, has_url={bool(ref.get('url'))}, has_file={bool(ref.get('file'))}"
                    )
                    data, filename, read_err = await self._read_media_bytes(event, ref)
                    if read_err:
                        logger.warning(f"合并聊天记录媒体读取失败: index={i}, err={read_err}")
                        return {"index": i, "ok": False, "error": read_err, "filename": filename, "kind": ref.get("kind")}
                    result = await self.upload_to_cloudflare_imgbed(data, folder_name, filename)
                    if isinstance(result, str) and result.startswith("http"):
                        return {"index": i, "ok": True, "url": result, "filename": filename, "kind": ref.get("kind")}
                    err_msg = result or "上传失败"
                    logger.warning(f"合并聊天记录媒体上传失败: index={i}, err={err_msg}")
                    return {"index": i, "ok": False, "error": err_msg, "filename": filename, "kind": ref.get("kind")}

            results = await asyncio.gather(*(upload_one(i) for i in indexes))

            ok_results = [r for r in results if r.get("ok")]
            fail_results = [r for r in results if not r.get("ok")]

            logger.info(f"合并聊天记录上传结束: folder={folder_name}, success={len(ok_results)}, fail={len(fail_results)}")
            logger.debug(f"合并聊天记录上传 forward_id={forward_id}")

            yield event.plain_result(self._build_upload_reply("上传完成", results))
            return

        if found_json_forward:
            yield event.plain_result("检测到合并聊天记录（JSON 格式），当前无法提取其中的图片/视频，请发送可解析的合并转发消息")
            return

        image_refs = await self._list_image_refs_from_event(event)
        if image_refs:
            indexes, err = self._parse_index_spec(index_spec, len(image_refs), label="图片", empty_msg="未找到可上传的图片")
            if err:
                yield event.plain_result(err)
                return

            logger.info(f"图片上传开始: folder={folder_name}, total={len(image_refs)}, selected={len(indexes)}")
            logger.debug(f"图片上传 indexes={indexes}")

            semaphore = asyncio.Semaphore(3)

            async def upload_one(i: int):
                ref = image_refs[i - 1]
                async with semaphore:
                    logger.debug(
                        f"图片上传任务开始: index={i}, filename={ref.get('filename')}, has_url={bool(ref.get('url'))}, has_file={bool(ref.get('file'))}"
                    )
                    data, filename, read_err = await self._read_media_bytes(event, ref)
                    if read_err:
                        logger.warning(f"图片读取失败: index={i}, err={read_err}")
                        return {"index": i, "ok": False, "error": read_err, "filename": filename, "kind": "image"}
                    result = await self.upload_to_cloudflare_imgbed(data, folder_name, filename)
                    if isinstance(result, str) and result.startswith("http"):
                        return {"index": i, "ok": True, "url": result, "filename": filename, "kind": "image"}
                    err_msg = result or "上传失败"
                    logger.warning(f"图片上传失败: index={i}, err={err_msg}")
                    return {"index": i, "ok": False, "error": err_msg, "filename": filename, "kind": "image"}

            results = await asyncio.gather(*(upload_one(i) for i in indexes))
            ok_results = [r for r in results if r.get("ok")]
            fail_results = [r for r in results if not r.get("ok")]

            logger.info(f"图片上传结束: folder={folder_name}, success={len(ok_results)}, fail={len(fail_results)}")

            yield event.plain_result(self._build_upload_reply("上传完成", results))
            return

        image_data = await self.get_first_image(event)

        if not image_data:
            video_data, original_filename = await self.get_first_video_from_reply(event)
            if video_data:
                result = await self.upload_to_cloudflare_imgbed(video_data, folder_name, original_filename)
                kind = "video"
            else:
                yield event.plain_result("未找到引用消息中的图片/视频")
                return
        else:
            result = await self.upload_to_cloudflare_imgbed(image_data, folder_name, None)
            kind = "image"

        if isinstance(result, str) and result.startswith("http"):
            reply = self._build_upload_reply(
                "上传完成",
                [{"index": 1, "ok": True, "url": result, "kind": kind}],
            )
            yield event.plain_result(reply)
        else:
            yield event.plain_result(result)

    @filter.command("imglink")
    async def link_keyword_to_folder(self, event: AstrMessageEvent, keyword: str = None, folder_name: str = None, content_type: str = None):
        """关联关键词和文件夹"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        if not keyword:
            if not self.keyword_folder_map:
                yield event.plain_result("当前没有已设置的关键词映射。")
                return

            result = "当前关键词映射列表：\n"
            for key, mapping in self.keyword_folder_map.items():
                if isinstance(mapping, dict):
                    folder = mapping.get("folder", "")
                    ctype = mapping.get("content_type", "image,video")
                    result += f"  /{key} -> {folder} ({ctype})\n"
                else:
                    result += f"  /{key} -> {mapping}\n"
            result += "\n使用 /imglink 关键词 文件夹名 [内容类型] 来添加新映射。\n内容类型可选: img(图片), vid(视频), 未指定则为全部"
            yield event.plain_result(result.strip())
            return

        if not folder_name:
            yield event.plain_result("参数错误！格式：/imglink 关键词 文件夹名 [内容类型]\n例如：/imglink 3cy 3cy 或 /imglink 3cy 3cy img\n内容类型可选: img(图片), vid(视频), 未指定则为全部\n\n不带参数使用 /imglink 可查看所有映射。")
            return

        if content_type:
            if content_type.lower() in ['img', 'image']:
                final_content_type = "image"
            elif content_type.lower() in ['vid', 'video']:
                final_content_type = "video"
            else:
                yield event.plain_result(f"内容类型参数错误！可选值: img(图片), vid(视频)")
                return
        else:
            final_content_type = "image,video"

        self.keyword_folder_map[keyword] = {
            "folder": folder_name,
            "content_type": final_content_type
        }
        self.save_keyword_mappings()

        content_type_desc = {"image": "图片", "video": "视频", "image,video": "图片或视频"}
        desc = content_type_desc.get(final_content_type, "图片或视频")

        yield event.plain_result(f"已将关键词 '{keyword}' 与文件夹 '{folder_name}' 关联（{desc}），现在发送 /{keyword} 即可获取该文件夹的随机{desc}。")

    @filter.command("imgunlink")
    async def unlink_keyword(self, event: AstrMessageEvent, keyword: str = None):
        """取消关键词关联"""
        if not event.is_admin():
            yield event.plain_result("此指令仅限管理员使用")
            return

        if not keyword:
            yield event.plain_result("参数错误！格式：/imgunlink 关键词\n例如：/imgunlink 3cy")
            return

        if keyword not in self.keyword_folder_map:
            yield event.plain_result(f"关键词 '{keyword}' 不存在映射。")
            return

        del self.keyword_folder_map[keyword]
        self.save_keyword_mappings()

        yield event.plain_result(f"已删除关键词 '{keyword}' 的映射。")

    # ==================== 动态命令处理 ====================

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def handle_dynamic_commands_group(self, event: AstrMessageEvent):
        """处理群组消息中的动态命令"""
        async for result in self._process_dynamic_command(event):
            yield result
            event.stop_event()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_dynamic_commands_private(self, event: AstrMessageEvent):
        """处理私聊消息中的动态命令"""
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

        if message_text.startswith('/') and len(message_text) > 1:
            keyword = message_text[1:]

            if keyword in self.keyword_folder_map:
                mapping = self.keyword_folder_map[keyword]
                if isinstance(mapping, dict):
                    folder_name = mapping.get("folder", "")
                    content_type = mapping.get("content_type", "image,video")
                else:
                    folder_name = mapping
                    content_type = "image,video"

                result = await self.get_random_file_from_folder(folder_name, content_type)

                if isinstance(result, list):
                    yield event.chain_result(result)
                else:
                    yield event.plain_result(result)

    async def terminate(self):
        """插件销毁时的清理工作"""
        logger.info("CF图床助手已卸载")

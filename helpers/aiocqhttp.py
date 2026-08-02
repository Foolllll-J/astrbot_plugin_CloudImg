import json

from astrbot import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Reply as ApiReply


class AiocqhttpMixin:
    """aiocqhttp (OneBot/QQ) 平台专属：合并转发解析、file_id 获取。"""

    async def _onebot_get_file(
        self, event: AstrMessageEvent, file_or_id: str
    ) -> bytes | None:
        """OneBot 兜底：call_action("get_file") 按 file_id 取文件 URL 并下载字节数据。"""
        if not file_or_id or not hasattr(event, "bot") or not hasattr(event.bot, "api"):
            return None
        try:
            result = await event.bot.api.call_action("get_file", file_id=file_or_id)
            if isinstance(result, dict) and result.get("url"):
                return await self.download_image(result["url"])
        except Exception as e:
            logger.debug(f"获取文件失败(get_file): err={e}")
        return None

    async def _try_get_forward_id(
        self, event: AstrMessageEvent
    ) -> tuple[str | None, bool]:
        forward_id = None
        found_json_forward = False

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

        reply_id = None
        message_list = getattr(getattr(event, "message_obj", None), "message", None)
        if isinstance(message_list, list):
            for seg in message_list:
                if (
                    forward_id is None
                    and seg.__class__.__name__ == "Forward"
                    and hasattr(seg, "id")
                ):
                    forward_id = seg.id
                    break
                if seg.__class__.__name__ == "Reply" and hasattr(seg, "id"):
                    reply_id = seg.id

        if forward_id is not None:
            return forward_id, found_json_forward

        for seg in event.get_messages():
            if (
                isinstance(seg, ApiReply)
                and hasattr(seg, "chain")
                and isinstance(seg.chain, list)
            ):
                for inner in seg.chain:
                    if inner.__class__.__name__ == "Forward" and hasattr(inner, "id"):
                        return inner.id, found_json_forward

        if reply_id and hasattr(event, "bot") and hasattr(event.bot, "api"):
            try:
                original_msg = await event.bot.api.call_action(
                    "get_msg", message_id=reply_id
                )
                original_chain = (
                    original_msg.get("message")
                    if isinstance(original_msg, dict)
                    else None
                )
                if isinstance(original_chain, list):
                    for segment in original_chain:
                        if not isinstance(segment, dict):
                            continue
                        seg_type = segment.get("type")
                        if seg_type == "forward":
                            forward_id = segment.get("data", {}).get("id")
                            if forward_id:
                                break
                        if seg_type == "json":
                            try:
                                inner_data_str = segment.get("data", {}).get("data")
                                if inner_data_str:
                                    inner_data_str = inner_data_str.replace(
                                        "&#44;", ","
                                    )
                                    inner_json = json.loads(inner_data_str)
                                    json_forward_id = (
                                        extract_forward_id_from_multimsg_json(
                                            inner_json
                                        )
                                    )
                                    if json_forward_id:
                                        forward_id = json_forward_id
                                        found_json_forward = True
                                        break
                                    if (
                                        inner_json.get("app") == "com.tencent.multimsg"
                                        and inner_json.get("config", {}).get("forward")
                                        == 1
                                    ):
                                        found_json_forward = True
                            except Exception:
                                pass
            except Exception as e:
                logger.warning(f"获取被回复消息详情失败: {e}")

        return forward_id, found_json_forward

    async def _list_media_refs_from_forward(
        self, event: AstrMessageEvent, forward_id: str
    ) -> list[dict]:
        if not hasattr(event, "bot") or not hasattr(event.bot, "api"):
            return []

        try:
            logger.debug(f"开始拉取合并转发详情: forward_id={forward_id}")
            forward_data = await event.bot.api.call_action(
                "get_forward_msg", id=forward_id
            )
        except Exception as e:
            logger.warning(f"调用 get_forward_msg API 失败 (ID: {forward_id}): {e}")
            return []

        messages = (
            forward_data.get("messages") if isinstance(forward_data, dict) else None
        )
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
        logger.debug(
            f"合并转发媒体解析完成: total={len(filtered)}, images={img_count}, videos={vid_count}"
        )
        return filtered

import os

from astrbot import logger
from astrbot.core.platform.astr_message_event import (
    AstrMessageEvent as BaseAstrMessageEvent,
)


class TelegramMixin:
    """Telegram 平台专属：原始引用媒体提取、文件路径下载。"""

    async def _telegram_download_file(
        self, event: BaseAstrMessageEvent, source: str
    ) -> bytes | None:
        """按 Telegram file_path 拼接 Bot API 下载 URL 并拉取字节数据。"""
        if event.get_platform_name() != "telegram" or not hasattr(event, "client"):
            return None
        base_url = str(getattr(event.client, "base_url", "") or "").rstrip("/")
        if not base_url:
            return None
        download_url = f"{base_url}/file/{source.lstrip('/')}"
        logger.debug(f"读取媒体(Telegram file_path): source={source}")
        return await self.download_image(download_url)

    async def _list_telegram_raw_reply_refs(
        self, event: BaseAstrMessageEvent
    ) -> list[dict]:
        if event.get_platform_name() != "telegram":
            return []

        raw_message = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not raw_message:
            return []

        try:
            reply_to = raw_message.message.reply_to_message
            if not reply_to:
                return []

            refs: list[dict] = []
            if reply_to.photo:
                photo = reply_to.photo[-1]
                file = await photo.get_file()
                file_path = file.file_path
                if file_path:
                    refs.append(
                        {
                            "kind": "image",
                            "url": file_path,
                            "file": file_path,
                            "path": file_path,
                            "filename": os.path.basename(file_path) or "upload.jpg",
                        }
                    )
            elif reply_to.video:
                file = await reply_to.video.get_file()
                file_path = file.file_path
                if file_path:
                    refs.append(
                        {
                            "kind": "video",
                            "url": file_path,
                            "file": file_path,
                            "path": file_path,
                            "filename": os.path.basename(file_path) or "upload.mp4",
                        }
                    )
            return refs
        except Exception as e:
            logger.debug(f"读取 Telegram 原始引用媒体失败: {e}")
            return []

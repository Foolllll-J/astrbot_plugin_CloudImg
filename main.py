from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, StarTools, register
import aiohttp
import os
import json
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
            logger.error(f"图片下载失败: {e}")
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

        # 根据原始文件名确定文件扩展名
        file_ext = ".jpg"  # 默认
        content_type = "image/jpeg"  # 默认

        if original_filename:
            if original_filename.lower().endswith('.mp4'):
                file_ext = ".mp4"
                content_type = "video/mp4"
            elif original_filename.lower().endswith('.webm'):
                file_ext = ".webm"
                content_type = "video/webm"
            elif original_filename.lower().endswith('.jpg') or original_filename.lower().endswith('.jpeg'):
                file_ext = ".jpg"
                content_type = "image/jpeg"
            elif original_filename.lower().endswith('.png'):
                file_ext = ".png"
                content_type = "image/png"
            elif original_filename.lower().endswith('.gif'):
                file_ext = ".gif"
                content_type = "image/gif"
            elif original_filename.lower().endswith('.bmp'):
                file_ext = ".bmp"
                content_type = "image/bmp"

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
            logger.error(f"文件上传失败: {e}")
            return f"文件上传失败: {str(e)}"

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
    async def upload_image(self, event: AstrMessageEvent, folder_name: str = None):
        """上传图片到CloudFlare ImgBed"""
        if self.upload_admin_only:
            if not event.is_admin():
                yield event.plain_result("上传功能仅限管理员使用")
                return

        if not folder_name:
            yield event.plain_result("请指定上传的文件夹名，格式：/上传 文件夹名")
            return

        image_data = await self.get_first_image(event)

        if not image_data:
            video_data, original_filename = await self.get_first_video_from_reply(event)
            if video_data:
                result = await self.upload_to_cloudflare_imgbed(video_data, folder_name, original_filename)
            else:
                yield event.plain_result("未找到引用消息中的图片/视频")
                return
        else:
            result = await self.upload_to_cloudflare_imgbed(image_data, folder_name, None)

        if result.startswith("http"):
            if self.show_upload_link:
                yield event.plain_result(f"文件上传成功！\n链接: {result}")
            else:
                yield event.plain_result("文件上传成功！")
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

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def handle_dynamic_commands_private(self, event: AstrMessageEvent):
        """处理私聊消息中的动态命令"""
        async for result in self._process_dynamic_command(event):
            yield result

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

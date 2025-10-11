from astrbot.api.message_components import *
from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
import aiohttp

@register("astrbot_plugin_CloudImg", "Foolllll", "从图床获取随机图片。使用 /img 获取一张随机图片。", "1.0", "https://github.com/Foolllll-J/astrbot_plugin_CloudImg")
class CloudImgPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config
        self.base_url = config.get("base_url", "")
        self.random_path_suffix = "/random?form=text" 

    @filter.command("img")
    async def get_image(self, event: AstrMessageEvent):
        
        api_request_url = f"{self.base_url}{self.random_path_suffix}"
        
        if not self.base_url:
            yield event.plain_result("\n请先在配置文件中设置图床的基础地址 (base_url)")
            return
            
        # 创建一个不验证SSL的连接上下文
        ssl_context = aiohttp.TCPConnector(verify_ssl=False)
        async with aiohttp.ClientSession(connector=ssl_context) as session:
            try:
                async with session.get(api_request_url) as response:
                    # 检查 HTTP 状态码
                    if response.status != 200:
                        yield event.plain_result(f"\nAPI请求失败，状态码: {response.status}")
                        return
                        
                    relative_image_path = await response.text()
                    relative_image_path = relative_image_path.strip()
                    
                    image_url = f"{self.base_url}{relative_image_path}"                  
                    
                    # 构建消息链
                    chain = [
                        Image.fromURL(image_url)
                    ]
                    
                    yield event.chain_result(chain)
                    
            except Exception as e:
                yield event.plain_result(f"\n请求图床失败: {str(e)}。请检查 base_url 是否正确。")
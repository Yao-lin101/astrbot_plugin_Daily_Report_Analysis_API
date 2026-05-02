from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import aiohttp
import asyncio
from datetime import datetime

@register("astrbot_plugin_Daily_Report_Analysis_API", "e.e.", "联动StillAlive发送每日群聊以及与AI机器人私聊的消息汇总", "1.0.0")
class DailyReportAnalysisAPI(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.group_messages = []
        self.private_messages = []
        self.scheduled_task = None

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        # 启动定时任务，每小时执行一次
        self.scheduled_task = asyncio.create_task(self.hourly_task())

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        if self.scheduled_task:
            self.scheduled_task.cancel()

    async def send_to_api(self, data):
        """发送数据到目标API"""
        config = self.context.get_config()
        target_url = config.get('target_url')
        character_key = config.get('character_key')
        
        if not target_url or not character_key:
            logger.error("配置未设置：target_url 或 character_key")
            return
        
        headers = {
            "X-Character-Key": character_key,
            "Content-Type": "application/json"
        }
        
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(target_url, json=data, headers=headers) as response:
                    if response.status == 200:
                        logger.info("消息发送成功")
                    else:
                        logger.error(f"消息发送失败，状态码：{response.status}")
        except Exception as e:
            logger.error(f"发送请求时出错：{str(e)}")



    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听所有消息"""
        config = self.context.get_config()
        specific_user_id = config.get('specific_user_id')
        
        if not specific_user_id:
            logger.warning("插件 DailyReportAnalysisAPI: 未设置 specific_user_id，跳过消息处理。")
            return
        
        # 检查是否是特定用户
        sender_id = event.get_sender_id()
        
        # 记录用户消息
        if sender_id == specific_user_id:
            message_content = ""
            for msg_component in event.message_obj.message:
                if hasattr(msg_component, 'type') and msg_component.type == 'Plain':
                    message_content += getattr(msg_component, 'text', '')
            
            if not message_content:
                return

            time_str = datetime.fromtimestamp(event.message_obj.timestamp).strftime("%H:%M")

            if not event.message_obj.group_id:
                # 私聊消息
                logger.info(f"DailyReportAnalysisAPI: 记录私聊消息 - {event.get_sender_name()}: {message_content}")
                self.private_messages.append({
                    "时间": time_str,
                    "用户昵称": event.get_sender_name(),
                    "用户消息": message_content,
                    "机器人回复": ""
                })
            else:
                # 群聊消息 (特定用户在群里说话)
                logger.info(f"DailyReportAnalysisAPI: 记录群聊消息(特定用户) - {event.get_sender_name()}: {message_content}")
                new_msg_group = {
                    "时间": time_str,
                    "群名称": "未知群聊",
                    f"【用户】{event.get_sender_name()}": message_content
                }
                self.group_messages.append(new_msg_group)
        else:
            # 如果是群聊中其他人的消息
            if event.message_obj.group_id:
                message_content = ""
                for msg_component in event.message_obj.message:
                    if hasattr(msg_component, 'type') and msg_component.type == 'Plain':
                        message_content += getattr(msg_component, 'text', '')
                
                if message_content:
                    time_str = datetime.fromtimestamp(event.message_obj.timestamp).strftime("%H:%M")
                    logger.info(f"DailyReportAnalysisAPI: 记录群聊消息(群友) - {event.get_sender_name()}: {message_content}")
                    new_msg_group = {
                        "时间": time_str,
                        "群名称": "未知群聊",
                        f"【群友】{event.get_sender_name()}": message_content
                    }
                    self.group_messages.append(new_msg_group)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后的处理：获取机器人回复并记录"""
        result = event.get_result()
        if not result:
            return
            
        # 获取回复文本
        reply_text = result.get_plain_text()
        if not reply_text:
            return

        # 判断是群聊还是私聊并记录回复
        if event.message_obj.group_id:
            if self.group_messages and not self.group_messages[-1].get("机器人回复"):
                logger.info(f"DailyReportAnalysisAPI: 记录机器人群聊回复 - {reply_text[:20]}...")
                self.group_messages[-1]["机器人回复"] = reply_text
        else:
            if self.private_messages and not self.private_messages[-1].get("机器人回复"):
                logger.info(f"DailyReportAnalysisAPI: 记录机器人私聊回复 - {reply_text[:20]}...")
                self.private_messages[-1]["机器人回复"] = reply_text



    async def hourly_task(self):
        """每小时执行一次的任务"""
        while True:
            await asyncio.sleep(3600)  # 每小时执行一次
            
            if self.group_messages:
                # 构建群聊消息数据
                data = {
                    "type": "qq_messages",
                    "data": {
                        "group_messages": self.group_messages
                    }
                }
                
                # 发送数据到API
                await self.send_to_api(data)
                
                # 清空群聊消息记录
                self.group_messages = []

            if self.private_messages:
                # 构建私聊消息数据
                data = {
                    "type": "qq_messages",
                    "data": {
                        "private_messages": self.private_messages
                    }
                }
                
                # 发送数据到API
                await self.send_to_api(data)
                
                # 清空私聊消息记录
                self.private_messages = []


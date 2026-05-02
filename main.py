from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
import aiohttp
import asyncio
from datetime import datetime

@register("Daily_Report_Analysis_API", "e.e.", "联动StillAlive发送每日群聊以及与AI机器人私聊的消息汇总", "1.0.0")
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
            return
        
        # 检查是否是特定用户
        sender_id = event.get_sender_id()
        if sender_id != specific_user_id:
            # 如果不是特定用户，检查是否是群聊中特定用户的消息
            if event.message_obj.group_id:
                # 记录群聊消息
                for msg_component in event.message_obj.message:
                    if hasattr(msg_component, 'type') and msg_component.type == 'Plain':
                        message_content = getattr(msg_component, 'text', '')
                        time_str = datetime.fromtimestamp(event.message_obj.timestamp).strftime("%H:%M")
                        group_name = "未知群聊"
                        # 尝试获取群名称（具体实现可能需要根据消息平台适配器调整）
                        
                        # 检查消息是否来自特定用户
                        if sender_id == specific_user_id:
                            role = "【用户】"
                        else:
                            role = "【群友】"
                        
                        # 查找或创建群聊消息记录
                        found = False
                        for msg_group in self.group_messages:
                            if msg_group.get("群名称") == group_name:
                                msg_group[f"{role}{event.get_sender_name()}"] = message_content
                                found = True
                                break
                        
                        if not found:
                            new_msg_group = {
                                "时间": time_str,
                                "群名称": group_name,
                                f"{role}{event.get_sender_name()}": message_content
                            }
                            self.group_messages.append(new_msg_group)
            return
        
        # 处理特定用户的私聊消息
        if not event.message_obj.group_id:
            # 记录用户消息
            for msg_component in event.message_obj.message:
                if hasattr(msg_component, 'type') and msg_component.type == 'Plain':
                    message_content = getattr(msg_component, 'text', '')
                    time_str = datetime.fromtimestamp(event.message_obj.timestamp).strftime("%H:%M")
                    # 记录用户消息，等待机器人回复
                    self.private_messages.append({
                        "时间": time_str,
                        "用户昵称": event.get_sender_name(),
                        "用户消息": message_content,
                        "机器人回复": ""
                    })
                    break

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_bot_response(self, event: AstrMessageEvent):
        """监听机器人的回复"""
        # 检查是否是机器人的回复
        # 注意：这里需要根据实际情况判断，不同消息平台的实现可能不同
        # 暂时假设机器人回复也会触发事件，需要根据实际情况调整
        
        # 检查是否有未完成的私聊消息
        if self.private_messages and not self.private_messages[-1].get("机器人回复"):
            # 提取机器人回复内容
            for msg_component in event.message_obj.message:
                if hasattr(msg_component, 'type') and msg_component.type == 'Plain':
                    message_content = getattr(msg_component, 'text', '')
                    # 更新最后一条私聊消息的机器人回复
                    self.private_messages[-1]["机器人回复"] = message_content
                    
                    # 发送私聊消息到API
                    data = {
                        "type": "qq_messages",
                        "data": {
                            "private_messages": [{
                                "时间": self.private_messages[-1]["时间"],
                                "用户昵称": self.private_messages[-1]["用户昵称"],
                                "机器人昵称": message_content
                            }]
                        }
                    }
                    await self.send_to_api(data)
                    break

    async def after_message_sent_handler(self, event: AstrMessageEvent, result):
        """消息发送后的处理"""
        pass

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

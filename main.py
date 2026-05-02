from datetime import datetime

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageEventResult, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.event_message_type import EventMessageType
from astrbot.core.utils.plugin_kv_store import PluginKVStoreMixin


@register("daily_report_analysis_api", "e.e.", "联动StillAlive发送每日群聊以及与AI机器人私聊的消息汇总", "1.0.0")
class DailyReportAnalysisAPI(Star, PluginKVStoreMixin):
    def __init__(self, context: Context):
        super().__init__(context)
        self.plugin_id = "daily_report_analysis_api"
        self.group_messages_cache = {}

    async def initialize(self):
        """初始化插件，注册定时任务"""
        # 初始化配置项默认值
        await self._init_default_config()
        # 注册每小时执行的定时任务
        await self._register_hourly_task()

    async def _init_default_config(self):
        """初始化默认配置"""
        # 检查并设置默认配置
        if await self.get_kv_data("target_url", None) is None:
            await self.put_kv_data("target_url", "")
        if await self.get_kv_data("character_key", None) is None:
            await self.put_kv_data("character_key", "")
        if await self.get_kv_data("specific_user_id", None) is None:
            await self.put_kv_data("specific_user_id", "")

    async def _register_hourly_task(self):
        """注册每小时执行的定时任务"""
        try:
            # 每小时整点执行
            await self.context.cron_manager.add_basic_job(  # type: ignore
                name="hourly_group_message_report",
                cron_expression="0 * * * *",
                handler=self._process_hourly_group_messages,
                description="每小时从群聊中收集特定用户的对话并发送到目标URL",
                persistent=False
            )
            logger.info("已注册每小时执行的群聊消息收集任务")
        except Exception as e:
            logger.error(f"注册定时任务失败: {e}")

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    async def handle_private_message(self, event: AstrMessageEvent):
        """处理私聊消息，当特定用户发送消息且机器人回复后，发送对话内容到目标URL"""
        # 获取配置
        specific_user_id = await self.get_kv_data("specific_user_id", "")
        if not specific_user_id:
            return

        # 检查是否是特定用户
        sender_id = event.get_sender_id()
        if sender_id != specific_user_id:
            return

        # 记录用户消息
        user_message = event.message_str
        timestamp = event.created_at
        time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M")
        user_name = event.get_sender_name()

        # 存储消息信息到事件的extra中，供after_message_sent处理器使用
        event.set_extra("private_message_info", {
            "time_str": time_str,
            "user_name": user_name,
            "user_message": user_message
        })

    @filter.after_message_sent()
    async def after_message_sent_handler(self, event: AstrMessageEvent, result: MessageEventResult):
        """消息发送后的处理，用于捕获机器人回复并发送到目标URL"""
        # 检查是否有私聊消息信息
        private_message_info = event.get_extra("private_message_info")
        if private_message_info:
            # 获取机器人回复的纯文本
            bot_message = result.get_plain_text()
            # 获取机器人名称（使用平台信息作为机器人名称）
            bot_name = event.get_platform_name()
            # 发送对话内容到目标URL
            await self._send_private_message_report(
                private_message_info["time_str"],
                private_message_info["user_name"],
                private_message_info["user_message"],
                bot_name,
                bot_message
            )
            # 清除extra信息，避免重复处理
            event.set_extra("private_message_info", None)

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def handle_group_message(self, event: AstrMessageEvent):
        """处理群聊消息，缓存特定用户参与的对话"""
        # 获取配置
        specific_user_id = await self.get_kv_data("specific_user_id", "")
        if not specific_user_id:
            return

        # 检查消息是否包含特定用户
        sender_id = event.get_sender_id()
        group_id = event.get_session_id()
        # 获取群聊名称
        group_name = getattr(event.message_obj, "group", None)
        if group_name and hasattr(group_name, "name"):
            group_name = group_name.name
        else:
            group_name = group_id
        message_str = event.message_str
        timestamp = event.created_at
        time_str = datetime.fromtimestamp(timestamp).strftime("%H:%M")
        sender_name = event.get_sender_name()

        # 初始化群聊缓存
        if group_id not in self.group_messages_cache:
            self.group_messages_cache[group_id] = {
                "group_name": group_name,
                "conversations": []
            }

        # 检查是否是特定用户的消息
        if sender_id == specific_user_id:
            # 新建一个对话，包含特定用户的消息
            conversation = {
                "时间": time_str,
                "群名称": group_name,
                f"【用户】{sender_name}": message_str
            }
            self.group_messages_cache[group_id]["conversations"].append(conversation)
        else:
            # 检查是否有未完成的对话（包含特定用户的最新对话）
            conversations = self.group_messages_cache[group_id]["conversations"]
            if conversations:
                latest_conversation = conversations[-1]
                # 如果最新对话包含特定用户，则添加群友消息
                if any("【用户】" in key for key in latest_conversation):
                    latest_conversation[f"【群友】{sender_name}"] = message_str

    async def _process_hourly_group_messages(self):
        """每小时处理群聊消息并发送到目标URL"""
        try:
            # 准备群聊消息数据
            group_messages = []
            for group_data in self.group_messages_cache.values():
                group_messages.extend(group_data["conversations"])

            # 如果有消息，发送到目标URL
            if group_messages:
                await self._send_group_message_report(group_messages)

            # 清空缓存
            self.group_messages_cache.clear()
        except Exception as e:
            logger.error(f"处理群聊消息失败: {e}")

    async def _send_private_message_report(self, time_str, user_name, user_message, bot_name, bot_message):
        """发送私聊消息报告到目标URL"""
        # 获取配置
        target_url = await self.get_kv_data("target_url", "")
        character_key = await self.get_kv_data("character_key", "")
        if not target_url or not character_key:
            return

        # 构建请求数据
        data = {
            "type": "qq_messages",
            "data": {
                "private_messages": [
                    {
                        "时间": time_str,
                        "用户昵称": user_message,
                        "机器人昵称": bot_message
                    }
                ]
            }
        }

        # 发送请求
        await self._send_request(target_url, character_key, data)

    async def _send_group_message_report(self, group_messages):
        """发送群聊消息报告到目标URL"""
        # 获取配置
        target_url = await self.get_kv_data("target_url", "")
        character_key = await self.get_kv_data("character_key", "")
        if not target_url or not character_key:
            return

        # 构建请求数据
        data = {
            "type": "qq_messages",
            "data": {
                "group_messages": group_messages
            }
        }

        # 发送请求
        await self._send_request(target_url, character_key, data)

    async def _send_request(self, url, character_key, data):
        """发送HTTP请求"""
        try:
            headers = {
                "X-Character-Key": character_key,
                "Content-Type": "application/json"
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=data) as response:
                    if response.status == 200:
                        logger.info(f"发送消息到 {url} 成功")
                    else:
                        logger.error(f"发送消息到 {url} 失败，状态码: {response.status}")
        except Exception as e:
            logger.error(f"发送HTTP请求失败: {e}")

    async def terminate(self):
        """插件销毁时的清理工作"""
        # 清空缓存
        self.group_messages_cache.clear()

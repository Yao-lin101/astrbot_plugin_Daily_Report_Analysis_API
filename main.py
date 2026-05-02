import asyncio
from collections import defaultdict
from datetime import datetime

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_Daily_Report_Analysis_API",
    "e.e.",
    "联动StillAlive发送每日群聊以及与AI机器人私聊的消息汇总",
    "1.0.0",
)
class DailyReportAnalysisAPI(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # group_messages_map: {group_id: [messages]}
        self.group_messages_map = defaultdict(list)
        # 记录哪些群组在当前周期内有特定用户参与
        self.active_groups = set()

        self.private_messages = []
        self.scheduled_task = None
        self.config = config

    async def initialize(self):
        """插件初始化"""
        if not self.config:
            self.config = self.context.get_config()

        specific_user_id = self.config.get("specific_user_id")
        logger.info(
            f"DailyReportAnalysisAPI: 插件已初始化。监控用户ID: {specific_user_id}"
        )

        # 启动定时任务
        self.scheduled_task = asyncio.create_task(self.hourly_task())

    async def terminate(self):
        """插件销毁"""
        if self.scheduled_task:
            self.scheduled_task.cancel()

    async def send_to_api(self, data):
        """发送数据到目标API"""
        target_url = self.config.get("target_url")
        character_key = self.config.get("character_key")

        if not target_url or not character_key:
            logger.error(
                "DailyReportAnalysisAPI: 配置未设置：target_url 或 character_key"
            )
            return

        headers = {"X-Character-Key": character_key, "Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    target_url, json=data, headers=headers
                ) as response:
                    if response.status == 200:
                        logger.info(
                            f"DailyReportAnalysisAPI: {data.get('type')} 发送成功"
                        )
                    else:
                        logger.error(
                            f"DailyReportAnalysisAPI: 发送失败，状态码：{response.status}"
                        )
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 发送请求时出错：{str(e)}")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听所有消息"""
        sender_id = str(event.get_sender_id())
        message_content = event.message_str  # 仅文字

        if not message_content:
            return

        config = self.config or self.context.get_config()
        specific_user_id = str(config.get("specific_user_id", ""))

        if not specific_user_id:
            return

        time_str = datetime.fromtimestamp(event.message_obj.timestamp).strftime("%H:%M")
        sender_name = event.get_sender_name()

        # 处理私聊消息
        if not event.message_obj.group_id:
            if sender_id == specific_user_id:
                logger.info(f"DailyReportAnalysisAPI: 记录私聊消息 - {sender_name}")
                self.private_messages.append(
                    {
                        "时间": time_str,
                        "用户昵称": message_content,  # 按照文档格式：Key为"用户昵称"，Value为内容
                        "机器人昵称": "",
                    }
                )
        # 处理群聊消息
        else:
            group_id = event.message_obj.group_id
            group_name = "未知群聊"
            if event.message_obj.group and event.message_obj.group.group_name:
                group_name = event.message_obj.group.group_name

            # 标记特定用户参与的群组
            if sender_id == specific_user_id:
                self.active_groups.add(group_id)
                prefix = "【用户】"
            else:
                prefix = "【群友】"

            msg_obj = {
                "时间": time_str,
                "群名称": group_name,
                f"{prefix}{sender_name}": message_content,
            }
            self.group_messages_map[group_id].append(msg_obj)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """消息发送后的处理：记录机器人回复"""
        result = event.get_result()
        if not result:
            return

        reply_text = result.get_plain_text()
        if not reply_text:
            return

        if not event.message_obj.group_id:
            # 私聊回复
            if self.private_messages and not self.private_messages[-1].get(
                "机器人昵称"
            ):
                self.private_messages[-1]["机器人昵称"] = reply_text
                logger.info("DailyReportAnalysisAPI: 私聊对话完成，立即发送")
                await self._send_private_immediately()
        else:
            # 群聊回复（通常特定用户对话不会触发机器人回复的记录要求，但按文档逻辑群聊是每小时汇总）
            # 如果需要记录机器人对特定用户的回复，可以扩展此处逻辑
            pass

    async def _send_private_immediately(self):
        """立即发送私聊消息"""
        if self.private_messages:
            data = {
                "type": "qq_messages",
                "data": {"private_messages": list(self.private_messages)},
            }
            await self.send_to_api(data)
            self.private_messages = []

    async def hourly_task(self):
        """定时汇总任务"""
        while True:
            await asyncio.sleep(3600)
            await self._check_and_send_groups()

    async def _check_and_send_groups(self):
        """汇总并发送特定用户参与过的群聊对话"""
        all_to_send = []

        # 仅搜寻特定用户参与过的群组对话
        for group_id in list(self.active_groups):
            if group_id in self.group_messages_map:
                all_to_send.extend(self.group_messages_map[group_id])

        if all_to_send:
            data = {"type": "qq_messages", "data": {"group_messages": all_to_send}}
            await self.send_to_api(data)

        # 清理本周期数据
        self.group_messages_map.clear()
        self.active_groups.clear()

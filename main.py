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
        # 记录各群聊的静默期定时任务
        self.group_timers = {}
        # 记录哪些群组目前有待总结的数据
        self.active_groups = set()

        self.private_messages = []
        self.config = config

    async def initialize(self):
        """插件初始化"""
        if not self.config:
            self.config = self.context.get_config()

        specific_user_id = self.config.get("specific_user_id")
        logger.info(
            f"DailyReportAnalysisAPI: 插件已初始化。监控用户ID: {specific_user_id}"
        )

    async def terminate(self):
        """插件销毁，取消所有未完成的定时器"""
        for timer in self.group_timers.values():
            timer.cancel()
        self.group_timers.clear()

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

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为特定用户"""
        sender_id = str(event.get_sender_id())
        specific_user_id = str(self.config.get("specific_user_id", ""))
        return sender_id == specific_user_id

    @filter.command("stillalive群总结")
    async def manual_group_summary(self, event: AstrMessageEvent):
        """手动触发当前群聊的总结"""
        if not self._check_permission(event):
            yield event.plain_result("抱歉，您没有权限执行此指令。")
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("该指令仅在群聊中有效。")
            return

        if group_id not in self.active_groups:
            yield event.plain_result("当前群聊没有搜寻到待总结的特定用户参与记录。")
            return

        yield event.plain_result("正在生成当前群聊的话题总结并发送...")

        try:
            # 取消现有的定时器（如果有）
            if group_id in self.group_timers:
                self.group_timers[group_id].cancel()
                self.group_timers.pop(group_id, None)

            await self._summarize_single_group(group_id, force=True)
            yield event.plain_result("当前群聊总结发送完成。")
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 手动总结失败: {e}")
            yield event.plain_result(f"发送失败：{e}")

    @filter.command("stillalive清理缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        """手动清理当前群聊缓存"""
        if not self._check_permission(event):
            yield event.plain_result("抱歉，您没有权限执行此指令。")
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("该指令仅在群聊中有效。")
            return

        count = len(self.group_messages_map.get(group_id, []))
        self.group_messages_map.pop(group_id, None)
        self.active_groups.discard(group_id)

        if group_id in self.group_timers:
            self.group_timers[group_id].cancel()
            self.group_timers.pop(group_id, None)

        yield event.plain_result(
            f"已成功清理当前群聊缓存。清理了 {count} 条记录并重置了定时器。"
        )
        logger.info(
            f"DailyReportAnalysisAPI: 用户 {event.get_sender_id()} 手动清空了群聊 {group_id} 的缓存。"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听所有消息并管理事件驱动的定时器"""
        sender_id = str(event.get_sender_id())
        message_content = event.message_str

        if not message_content:
            return

        # 1. 排除用户指令 (通常以 / 或 . 开头)
        if message_content.startswith(("/", ".")):
            return

        specific_user_id = str(self.config.get("specific_user_id", ""))
        if not specific_user_id:
            return

        now = datetime.now().timestamp()
        time_str = datetime.fromtimestamp(event.message_obj.timestamp).strftime("%H:%M")
        sender_name = event.get_sender_name()

        # 处理私聊消息
        if not event.message_obj.group_id:
            if sender_id == specific_user_id:
                logger.debug(
                    f"DailyReportAnalysisAPI: 记录私聊消息 - {sender_name}: {message_content}"
                )
                self.private_messages.append(
                    {"时间": time_str, "用户": message_content, "你的回复": ""}
                )
        # 处理群聊消息
        else:
            group_id = event.message_obj.group_id
            group_name = "未知群聊"
            if event.message_obj.group and event.message_obj.group.group_name:
                group_name = event.message_obj.group.group_name

            is_specific_user = sender_id == specific_user_id
            prefix = "【用户】" if is_specific_user else "【群友】"

            msg_content_formatted = f"{prefix}{sender_name}: {message_content}"
            logger.debug(
                f"DailyReportAnalysisAPI: 记录群聊消息 [{group_name}] - {msg_content_formatted}"
            )

            msg_obj = {
                "时间": time_str,
                "群名称": group_name,
                "content": msg_content_formatted,
                "sender_id": sender_id,
                "timestamp": now,
            }
            self.group_messages_map[group_id].append(msg_obj)

            # 如果是特定用户发言，标记活跃并重置该群组的静默定时器
            if is_specific_user:
                self.active_groups.add(group_id)

                # 取消旧定时器
                if group_id in self.group_timers:
                    self.group_timers[group_id].cancel()

                # 开启新定时器
                self.group_timers[group_id] = asyncio.create_task(
                    self._delay_summarize_task(group_id, 600)
                )

    async def _delay_summarize_task(self, group_id, delay):
        """静默期等待任务"""
        try:
            await asyncio.sleep(delay)
            await self._summarize_single_group(group_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 延迟总结任务出错: {e}")
        finally:
            self.group_timers.pop(group_id, None)

    async def _summarize_single_group(self, group_id, force=False):
        """对单个群组进行总结发送"""
        messages = self.group_messages_map.get(group_id, [])
        if not messages:
            self.active_groups.discard(group_id)
            return

        specific_user_id = str(self.config.get("specific_user_id", ""))

        # 寻找特定用户最后一次发言的索引
        last_user_msg_index = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("sender_id") == specific_user_id:
                last_user_msg_index = i
                break

        if last_user_msg_index == -1:
            if force:
                self.active_groups.discard(group_id)
            return

        # 截断数据
        to_summarize = messages[: last_user_msg_index + 1]
        to_keep = messages[last_user_msg_index + 1 :]

        # 更新缓存
        self.group_messages_map[group_id] = to_keep
        self.active_groups.discard(group_id)

        # 构建对话文本进行总结
        group_name = to_summarize[0].get("群名称", "未知群聊")
        last_time = to_summarize[-1].get("时间", "未知时间")
        dialogue_text = "\n".join([m["content"] for m in to_summarize])

        logger.debug(
            f"DailyReportAnalysisAPI: 开始总结群 [{group_name}]，消息条数: {len(to_summarize)}"
        )

        provider_id = self.config.get("summary_provider_id")
        if provider_id:
            try:
                prompt = f"以下是一段群聊记录，请精炼地总结这段对话的话题和主要内容（50字以内）：\n\n{dialogue_text}"
                response = await self.context.llm_generate(
                    chat_provider_id=provider_id, prompt=prompt
                )
                summary = response.completion_text.strip()
            except Exception as e:
                logger.error(f"DailyReportAnalysisAPI: 总结失败: {e}")
                summary = "（总结失败）"
        else:
            summary = dialogue_text[:100] + "..."

        # 发送
        data = {
            "type": "qq_messages",
            "data": {
                "group_messages": [
                    {"时间": last_time, "群名称": group_name, "话题总结": summary}
                ]
            },
        }
        await self.send_to_api(data)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """记录机器人回复，排除指令回复，保留 LLM 回复"""
        result = event.get_result()
        if not result:
            return

        reply_text = result.get_plain_text()
        if not reply_text:
            return

        # 2. 排除指令回复。根据 ResultContentType 判断。
        # 用户要求：llm回复不算指令回复，而是正常消息。
        # AstrBot 中 LLM 回复的 content_type 是 LLM_RESULT。
        is_llm_reply = result.is_model_result()
        if not is_llm_reply:
            # 如果不是 LLM 回复，则视为指令回复或普通插件回复，排除。
            return

        time_str = datetime.now().strftime("%H:%M")

        # 处理私聊回复
        if not event.message_obj.group_id:
            if self.private_messages and not self.private_messages[-1].get("你的回复"):
                self.private_messages[-1]["你的回复"] = reply_text
                await self._send_private_immediately()
        # 处理群聊回复 (用户新需求：群聊也记录 LLM 回复)
        else:
            group_id = event.message_obj.group_id
            group_name = "未知群聊"
            if event.message_obj.group and event.message_obj.group.group_name:
                group_name = event.message_obj.group.group_name

            bot_name = event.get_self_id()  # 或者使用固定名称
            msg_content_formatted = f"【机器人】{bot_name}: {reply_text}"

            msg_obj = {
                "时间": time_str,
                "群名称": group_name,
                "content": msg_content_formatted,
                "sender_id": "bot",
                "timestamp": datetime.now().timestamp(),
            }
            self.group_messages_map[group_id].append(msg_obj)
            logger.debug(
                f"DailyReportAnalysisAPI: 记录群聊机器人回复 [{group_name}] - {msg_content_formatted}"
            )

    async def _send_private_immediately(self):
        """立即发送私聊消息"""
        if self.private_messages:
            data = {
                "type": "qq_messages",
                "data": {"private_messages": list(self.private_messages)},
            }
            await self.send_to_api(data)
            self.private_messages = []

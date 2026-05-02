import asyncio
import inspect
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
    "1.2.1",
)
class DailyReportAnalysisAPI(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        # 消息缓存: {group_id: [msg_obj, ...]}，每个群保留最近 500 条
        self.group_messages_map = defaultdict(list)
        # 进度记录: {group_id: last_summarized_id}
        self.last_summarized_id = defaultdict(int)
        # 消息 ID 计数器: {group_id: counter}
        self.message_id_counter = defaultdict(int)

        self.user_nicknames = {}
        self.bot_nicknames = {}
        self.group_events = {}
        self.group_timers = {}
        self.active_groups = set()

        self.private_messages = []
        self.config = config
        self.internal_commands = []  # 将在 initialize 中自动填充

    async def initialize(self):
        """插件初始化"""
        if not self.config:
            self.config = self.context.get_config()

        # 自动识别插件内注册的所有指令名，实现自动屏蔽
        self._auto_collect_internal_commands()

        logger.info(
            f"DailyReportAnalysisAPI: 插件已初始化。监控用户ID: {self.config.get('specific_user_id')}, 已自动注册屏蔽指令: {self.internal_commands}"
        )

    def _auto_collect_internal_commands(self):
        """通过反射获取所有被 @filter.command 装饰的指令名"""
        self.internal_commands = []
        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            # 检查方法是否有 AstrBot 过滤器标记
            # @filter.command 装饰后的方法会带有特定属性（如 _astr_filter_cmds 或类似标记）
            if hasattr(method, "__astr_filter__"):
                filt = getattr(method, "__astr_filter__")
                # 提取指令名
                if hasattr(filt, "commands"):
                    self.internal_commands.extend(filt.commands)

        # 兜底：如果自动获取失败（API变动），手动加入已知指令
        if not self.internal_commands:
            self.internal_commands = ["stillalive群总结", "stillalive清理缓存"]
        else:
            # 去重
            self.internal_commands = list(set(self.internal_commands))

    async def terminate(self):
        """插件销毁"""
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

        messages = self.group_messages_map.get(group_id, [])
        last_id = self.last_summarized_id.get(group_id, 0)
        pending_messages = [m for m in messages if m["id"] > last_id]

        specific_user_id = str(self.config.get("specific_user_id", ""))
        has_specific_user = any(
            m["sender_id"] == specific_user_id for m in pending_messages
        )

        if not has_specific_user:
            yield event.plain_result("当前未总结的消息中不包含特定用户的发言。")
            return

        yield event.plain_result("正在生成当前群聊的话题总结并发送...")

        try:
            if group_id in self.group_timers:
                self.group_timers[group_id].cancel()
                self.group_timers.pop(group_id, None)

            await self._summarize_single_group(group_id)
            yield event.plain_result("当前群聊总结发送完成。")
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 手动总结失败: {e}")
            yield event.plain_result(f"发送失败：{e}")

    @filter.command("stillalive清理缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        """手动重置总结进度"""
        if not self._check_permission(event):
            yield event.plain_result("抱歉，您没有权限执行此指令。")
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result("该指令仅在群聊中有效。")
            return

        self.last_summarized_id[group_id] = 0
        self.active_groups.add(group_id)

        if group_id in self.group_timers:
            self.group_timers[group_id].cancel()
            self.group_timers.pop(group_id, None)

        yield event.plain_result("已重置总结进度。")

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听消息"""
        message_content = event.message_str

        if not message_content:
            return

        # 仅屏蔽本插件的指令（防止在总结记录中出现）
        if any(cmd in message_content for cmd in self.internal_commands):
            return

        specific_user_id = str(self.config.get("specific_user_id", ""))
        if not specific_user_id:
            return

        sender_id = str(event.get_sender_id())
        now = datetime.now().timestamp()
        time_str = datetime.fromtimestamp(event.message_obj.timestamp).strftime("%H:%M")
        sender_name = event.get_sender_name()

        # 处理群聊消息
        if event.message_obj.group_id:
            group_id = event.message_obj.group_id
            group_name = (
                event.message_obj.group.group_name
                if event.message_obj.group
                else "未知群聊"
            )
            self.group_events[group_id] = event

            is_specific_user = sender_id == specific_user_id
            if is_specific_user:
                self.user_nicknames[group_id] = sender_name
                prefix = "【用户】"
            else:
                prefix = "【群友】"

            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]

            msg_content_formatted = f"{prefix}{sender_name}: {message_content}"
            msg_obj = {
                "id": msg_id,
                "时间": time_str,
                "群名称": group_name,
                "content": msg_content_formatted,
                "sender_id": sender_id,
                "timestamp": now,
            }
            self.group_messages_map[group_id].append(msg_obj)

            if len(self.group_messages_map[group_id]) > 500:
                self.group_messages_map[group_id] = self.group_messages_map[group_id][
                    -500:
                ]

            logger.debug(
                f"DailyReportAnalysisAPI: 记录群聊消息 ID={msg_id} [{group_name}] - {msg_content_formatted}"
            )

            if is_specific_user:
                self.active_groups.add(group_id)
                if group_id in self.group_timers:
                    self.group_timers[group_id].cancel()

                self.group_timers[group_id] = asyncio.create_task(
                    self._delay_summarize_task(group_id, 600)
                )
        else:
            # 私聊记录逻辑
            if sender_id == specific_user_id:
                self.private_messages.append(
                    {"时间": time_str, "用户": message_content, "你的回复": ""}
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

    async def _summarize_single_group(self, group_id):
        """对增量消息进行总结，但快照点维持在特定用户最后一次发言"""
        messages = self.group_messages_map.get(group_id, [])
        last_id = self.last_summarized_id.get(group_id, 0)

        # 1. 提取所有尚未总结的新消息
        pending = [m for m in messages if m["id"] > last_id]
        if not pending:
            self.active_groups.discard(group_id)
            return

        specific_user_id = str(self.config.get("specific_user_id", ""))

        # 2. 寻找特定用户在本次 pending 列表中的最后一次发言索引
        last_user_msg_index = -1
        for i in range(len(pending) - 1, -1, -1):
            if pending[i].get("sender_id") == specific_user_id:
                last_user_msg_index = i
                break

        if last_user_msg_index == -1:
            self.active_groups.discard(group_id)
            return

        # 3. 准备内容
        to_summarize = pending

        user_nickname = self.user_nicknames.get(group_id, "用户")
        bot_nickname = self.bot_nicknames.get(group_id)
        if not bot_nickname:
            event = self.group_events.get(group_id)
            if event:
                try:
                    group_data = await event.get_group()
                    if group_data and group_data.members:
                        self_id = event.get_self_id()
                        for m in group_data.members:
                            if str(m.user_id) == str(self_id):
                                bot_nickname = m.nickname
                                self.bot_nicknames[group_id] = bot_nickname
                                break
                except Exception:
                    pass
            if not bot_nickname:
                bot_nickname = self.context.get_config().get("nickname", "机器人")

        group_name = to_summarize[0].get("群名称", "未知群聊")
        last_time = to_summarize[-1].get("时间", "未知时间")
        dialogue_text = "\n".join([m["content"] for m in to_summarize])

        provider_id = self.config.get("summary_provider_id")
        if provider_id:
            try:
                # 获取插件指定人格或全局默认人格
                persona_id = self.config.get("plugin_specific_persona_id")
                system_prompt = None
                if persona_id:
                    persona_v3 = self.context.persona_manager.get_persona_v3_by_id(
                        persona_id
                    )
                    if persona_v3:
                        system_prompt = persona_v3.get("prompt")

                if not system_prompt:
                    default_persona = (
                        await self.context.persona_manager.get_default_persona_v3()
                    )
                    system_prompt = default_persona.get("prompt")

                # 增加调试日志
                logger.debug(
                    f"DailyReportAnalysisAPI: 请求总结。使用人格 ID: {persona_id or '默认'}, 系统提示词长度: {len(system_prompt) if system_prompt else 0}"
                )

                # 强化 Prompt 指令
                prompt = (
                    f"对话背景：你在本群的昵称是【{bot_nickname}】，特定用户的昵称是【{user_nickname}】。\n"
                    f"任务目标：精炼地总结以下这段群聊记录（50字以内）。\n"
                    f"特别要求：请务必以你的人设口吻进行总结，并重点体现出特定用户【{user_nickname}】在对话中参与了哪些讨论或表达了什么核心观点。\n"
                    f"输出格式要求（务必严格遵守）：\n"
                    f"话题：<这里填写一句话话题>\n"
                    f"内容：<这里填写总结内容>\n"
                    f"规则：1. 不要使用Markdown格式符号（如 ** 或 #）；2. 不要添加多余解释；3. 不要改变此输出结构或增加字段。\n\n"
                    f"{dialogue_text}"
                )

                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
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
                    {
                        "时间": last_time,
                        "群名称": group_name,
                        "用户在本群昵称": user_nickname,
                        "你在本群昵称": bot_nickname,
                        "话题总结": summary,
                    }
                ]
            },
        }
        await self.send_to_api(data)

        # 4. 快照 ID 推进
        self.last_summarized_id[group_id] = pending[last_user_msg_index]["id"]
        self.active_groups.discard(group_id)

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        """记录机器人回复"""
        result = event.get_result()
        if not result or not result.is_model_result():
            return

        reply_text = result.get_plain_text()
        if not reply_text:
            return

        time_str = datetime.now().strftime("%H:%M")

        # 处理私聊回复
        if not event.message_obj.group_id:
            if self.private_messages and not self.private_messages[-1].get("你的回复"):
                self.private_messages[-1]["你的回复"] = reply_text
                await self._send_private_immediately()
        # 处理群聊回复
        else:
            group_id = event.message_obj.group_id
            group_name = (
                event.message_obj.group.group_name
                if event.message_obj.group
                else "未知群聊"
            )

            bot_name = self.bot_nicknames.get(group_id)
            if not bot_name:
                bot_name = self.context.get_config().get(
                    "nickname", event.get_self_id()
                )

            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]

            msg_content_formatted = f"【机器人】{bot_name}: {reply_text}"
            msg_obj = {
                "id": msg_id,
                "时间": time_str,
                "群名称": group_name,
                "content": msg_content_formatted,
                "sender_id": "bot",
                "timestamp": datetime.now().timestamp(),
            }
            self.group_messages_map[group_id].append(msg_obj)

            if len(self.group_messages_map[group_id]) > 500:
                self.group_messages_map[group_id] = self.group_messages_map[group_id][
                    -500:
                ]

            logger.debug(
                f"DailyReportAnalysisAPI: 记录机器人回复 ID={msg_id} [{group_name}] - {msg_content_formatted}"
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

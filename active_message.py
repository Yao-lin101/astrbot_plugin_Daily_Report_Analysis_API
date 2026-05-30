import asyncio
import json
import random
import re
import traceback
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.astr_message_event import AstrMessageEvent
from astrbot.core.platform.message_type import MessageType

from .prompts import (
    ACTIVE_MSG_CHECK_STATUS_PROMPT,
    ACTIVE_MSG_WAKE_PROMPT,
)


class MockEvent:
    def __init__(self, unified_msg_origin: str):
        self._unified_msg_origin = unified_msg_origin
        self._extras = {}
        self._result = None

        parts = unified_msg_origin.split(":")
        self.platform_id = parts[0]
        self.platform_name = parts[0]
        self._session_id = parts[-1]
        self.plugins_name = ["astrbot_plugin_meme_manager", "meme_manager"]

    @property
    def unified_msg_origin(self) -> str:
        return self._unified_msg_origin

    @unified_msg_origin.setter
    def unified_msg_origin(self, value: str) -> None:
        self._unified_msg_origin = value

    @property
    def session_id(self) -> str:
        return self._session_id

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._session_id = value

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def get_result(self):
        return self._result

    def set_result(self, result):
        self._result = result

    def get_platform_name(self):
        return self.platform_name

    def get_sender_id(self):
        return self.session_id

    def get_sender_name(self):
        return ""

    def is_stopped(self):
        return False

    def clear_result(self):
        self._result = None


class ActiveMessageWakeEvent(AstrMessageEvent):
    """Synthetic event used when an active message check triggers the native reply pipeline."""

    def __init__(
        self,
        *,
        context,
        session_str: str,
        message: str,
        sender_id: str,
        sender_name: str = "System",
        message_type: MessageType = MessageType.FRIEND_MESSAGE,
        at_user: bool = True,
    ) -> None:
        import time
        import uuid

        from astrbot.core.message.components import Plain
        from astrbot.core.platform.astrbot_message import AstrBotMessage, MessageMember
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.platform_metadata import PlatformMetadata

        session = MessageSession.from_str(session_str)
        platform_meta = PlatformMetadata(
            name=session.platform_name,
            description="Active Message Wake Event",
            id=session.platform_name,
        )

        msg_obj = AstrBotMessage()
        msg_obj.type = message_type
        msg_obj.self_id = sender_id
        msg_obj.session_id = session.session_id
        msg_obj.message_id = uuid.uuid4().hex
        msg_obj.sender = MessageMember(user_id=session.session_id, nickname=sender_name)
        msg_obj.message = [Plain(message)]
        msg_obj.message_str = message
        msg_obj.raw_message = message
        msg_obj.timestamp = int(time.time())

        if message_type == MessageType.GROUP_MESSAGE:
            msg_obj.group_id = session.session_id
            from astrbot.core.platform.astrbot_message import Group

            msg_obj.group = Group(session.session_id)

        super().__init__(message, msg_obj, platform_meta, session.session_id)

        self.session = session
        self.context_obj = context
        self.is_at_or_wake_command = True
        self.is_wake = True
        self.target_user_id = sender_id
        self.at_user = at_user
        self._at_sent = False

        # 设置 extra 标识为主动消息唤醒事件
        self.set_extra("is_active_message_wake", True)

    async def send(self, message: MessageChain) -> None:
        if message is None:
            return

        if (
            self.session.message_type == MessageType.GROUP_MESSAGE
            and self.target_user_id
            and self.at_user
            and not self._at_sent
        ):
            from astrbot.core.message.components import At, Plain

            # 避免重复 At
            has_at = any(
                isinstance(comp, At) and str(comp.qq) == str(self.target_user_id)
                for comp in message.chain
            )
            if not has_at:
                message.chain.insert(0, At(qq=self.target_user_id))
                # 如果第一项后面是 Plain 文本，我们可以给 Plain 文本加个空格以改善格式
                if len(message.chain) > 1 and isinstance(message.chain[1], Plain):
                    message.chain[1].text = " " + message.chain[1].text.lstrip()
            self._at_sent = True

        await self.context_obj.send_message(self.session, message)
        await super().send(message)

    async def send_streaming(self, generator, use_fallback: bool = False) -> None:
        async for chain in generator:
            await self.send(chain)


class ActiveMessageHandler:
    def __init__(self, plugin):
        self.plugin = plugin
        self.context = plugin.context
        self.api_service = plugin.api_service
        self.db = plugin.db
        self.loop_task = None

        # 从数据库恢复状态
        try:
            next_check_str = self.db.get_plugin_meta("active_msg_next_check_time")
            self.next_check_time = (
                datetime.fromisoformat(next_check_str)
                if next_check_str
                else datetime.now() + timedelta(minutes=1)
            )

            self.messages_sent_today = int(
                self.db.get_plugin_meta("active_msg_sent_today", 0)
            )

            last_reset_str = self.db.get_plugin_meta("active_msg_last_reset_date")
            self.last_reset_date = (
                datetime.strptime(last_reset_str, "%Y-%m-%d").date()
                if last_reset_str
                else datetime.now().date()
            )

            self._user_unified_origin = self.db.get_plugin_meta(
                "active_msg_user_origin"
            )
            # 记录用户最后活跃的群聊及时间 (内存变量即可，因为主要用于 5 分钟内的即时判断)
            self.last_active_group_id = None
            self.last_active_time = 0
        except Exception as e:
            logger.error(f"ActiveMessageHandler: 恢复状态失败: {e}")
            self.next_check_time = datetime.now() + timedelta(minutes=1)
            self.messages_sent_today = 0
            self.last_reset_date = datetime.now().date()
            self._user_unified_origin = None

    @property
    def user_unified_origin(self):
        return self._user_unified_origin

    @user_unified_origin.setter
    def user_unified_origin(self, value):
        self._user_unified_origin = value
        self.db.update_plugin_meta("active_msg_user_origin", value)

    def start(self):
        if self.loop_task:
            self.loop_task.cancel()
        self.loop_task = asyncio.create_task(self._main_loop())

    def stop(self):
        if self.loop_task:
            self.loop_task.cancel()
            self.loop_task = None

    async def _main_loop(self):
        last_enabled = None
        try:
            while True:
                config = self.plugin.config or {}
                enabled = config.get("enable_active_messaging", False)
                max_msgs = config.get("max_active_messages_per_day", 3)

                if enabled and last_enabled is not True:
                    logger.info("ActiveMessageHandler: 主动消息检测机制已激活。")
                elif not enabled and last_enabled is True:
                    logger.info("ActiveMessageHandler: 主动消息检测机制已停止。")

                last_enabled = enabled

                if not enabled:
                    await asyncio.sleep(60)
                    continue

                now = datetime.now()

                if now.date() > self.last_reset_date:
                    self.messages_sent_today = 0
                    self.last_reset_date = now.date()
                    self.db.update_plugin_meta("active_msg_sent_today", 0)
                    self.db.update_plugin_meta(
                        "active_msg_last_reset_date", self.last_reset_date.isoformat()
                    )

                if self.messages_sent_today >= max_msgs:
                    next_day = now + timedelta(days=1)
                    self.next_check_time = next_day.replace(
                        hour=0, minute=0, second=0, microsecond=0
                    )
                    self.db.update_plugin_meta(
                        "active_msg_next_check_time", self.next_check_time.isoformat()
                    )

                if now >= self.next_check_time:
                    sent = False
                    if self.messages_sent_today < max_msgs:
                        sent = await self._check_and_action()
                        if sent:
                            self.messages_sent_today += 1
                            self.db.update_plugin_meta(
                                "active_msg_sent_today", self.messages_sent_today
                            )
                            logger.info(
                                f"ActiveMessageHandler: 今日已发送主动消息 {self.messages_sent_today}/{max_msgs} 条"
                            )

                    if sent:
                        # 发送成功后，拉长下一次轮询的间隔
                        self.reset_polling(reason="发送消息")
                    else:
                        # 未发送（观望中）。如果 _check_and_action 内部没有更新 next_check_time（即仍为过去时间），则随机重置
                        if self.next_check_time <= datetime.now():
                            self.reset_polling(reason="观望结束")

                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: loop failed - {e}\n{traceback.format_exc()}"
            )
            await asyncio.sleep(60)

    def reset_polling(self, min_int=None, max_int=None, reason="互动"):
        config = self.plugin.config or {}
        if not config.get("enable_active_messaging", False):
            return
        min_interval = (
            min_int
            if min_int is not None
            else config.get("active_msg_min_interval", 30)
        )
        max_interval = (
            max_int
            if max_int is not None
            else config.get("active_msg_max_interval", 60)
        )
        offset_minutes = random.randint(min_interval, max_interval)
        self.next_check_time = datetime.now() + timedelta(minutes=offset_minutes)

        # 持久化
        self.db.update_plugin_meta(
            "active_msg_next_check_time", self.next_check_time.isoformat()
        )

        logger.info(
            f"ActiveMessageHandler: 收到{reason}，已重置主动消息轮询时间至 {self.next_check_time} ({offset_minutes}分钟后)"
        )

    def update_user_activity(self, group_id=None, unified_origin=None):
        """更新用户的最后活跃记录"""
        self.last_active_time = datetime.now().timestamp()
        self.last_active_group_id = group_id
        if unified_origin:
            self.user_unified_origin = unified_origin

    async def _get_system_prompt(self):
        config = self.plugin.config or {}
        persona_id = config.get("plugin_specific_persona_id")
        system_prompt = None
        if persona_id:
            persona_v3 = self.context.persona_manager.get_persona_v3_by_id(persona_id)
            if persona_v3:
                system_prompt = persona_v3.get("prompt")
        if not system_prompt:
            default_persona = (
                await self.context.persona_manager.get_default_persona_v3()
            )
            system_prompt = default_persona.get("prompt")
        return system_prompt

    def _parse_json(self, raw_result):
        from .message_utils import parse_json_robust

        return parse_json_robust(raw_result)

    async def _check_and_action(self):
        logger.info("ActiveMessageHandler: 到达观察时间，正在评估状态...")
        status_res = await self.api_service.fetch_status(memory="short")
        if not status_res or "prompt" not in status_res:
            logger.error("ActiveMessageHandler: 无法获取 short 状态，稍后重试。")
            return False

        status_data = status_res["prompt"]
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 获取最近对话上下文
        conversation_context = await self._get_recent_conversation_context(limit=10)

        config = self.plugin.config or {}
        min_int = config.get("active_msg_min_interval", 30)
        max_int = config.get("active_msg_max_interval", 60)

        system_prompt = await self._get_system_prompt()
        prompt = ACTIVE_MSG_CHECK_STATUS_PROMPT.format(
            current_time=current_time,
            status_data=status_data,
            conversation_context=conversation_context,
            min_interval=min_int,
            max_interval=max_int,
        )

        logger.info(f"ActiveMessageHandler: 评估提示词详情:\n---\n{prompt}\n---")

        provider_id = self.plugin.config.get("summary_provider_id")
        if not provider_id:
            return False

        try:
            response = await self.context.llm_generate(
                chat_provider_id=provider_id,
                system_prompt=system_prompt,
                prompt=prompt,
            )
        except Exception as e:
            logger.warning(f"ActiveMessageHandler: 评估状态时 LLM 请求失败: {e}")
            return False

        try:
            result_json = self._parse_json(response.completion_text)
            need_message = result_json.get("need_message", False)
            reason = result_json.get("reason", "")
            message_type = result_json.get("message_type", "care")

            if need_message:
                logger.info(
                    f"ActiveMessageHandler: 判定需要发消息，类型：{message_type}，理由：{reason}。准备唤醒原生回复..."
                )
                self.db.add_active_message_decision(
                    True, message_type, reason, 0, datetime.now().isoformat()
                )
                await self._wake_native_reply(reason, message_type, status_data)
                return True
            else:
                delay_minutes = result_json.get("delay_minutes", 0)
                try:
                    now = datetime.now()
                    delay_val = int(delay_minutes)

                    # 增加一层代码逻辑兜底：单次建议延迟最长不超过 6 小时 (360分钟)
                    # 以防止模型出现异常计算导致失联一整天
                    safe_delay = min(delay_val, 360)

                    self.next_check_time = now + timedelta(minutes=safe_delay)

                    self.db.update_plugin_meta(
                        "active_msg_next_check_time",
                        self.next_check_time.isoformat(),
                    )

                    # 记录决策到数据库
                    self.db.add_active_message_decision(
                        False,
                        message_type,
                        reason,
                        safe_delay,
                        self.next_check_time.isoformat(),
                    )

                    logger.info(
                        f"ActiveMessageHandler: 遵循模型建议，将在 {safe_delay} 分钟后（{self.next_check_time}）再次观察 (理由: {reason}{' [已截断]' if safe_delay < delay_val else ''})"
                    )
                    return False
                except Exception as e:
                    logger.debug(f"应用延迟时间失败: {e}")
                    # 如果应用延迟失败，也记录一下
                    self.db.add_active_message_decision(
                        False,
                        message_type,
                        f"Error: {e}. Original Reason: {reason}",
                        0,
                        datetime.now().isoformat(),
                    )

                logger.info("ActiveMessageHandler: 判定不需要发消息。")
                return False

        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: 解析评估状态失败 {e}。LLM 返回: {response.completion_text if 'response' in locals() else 'No response'}"
            )
            return False

    async def _get_recent_conversation_context(self, limit: int = 15) -> str:
        """获取最近的对话上下文（通过 AstrBot 原生对话记录构建，过滤群聊中其他用户的发言）"""
        specific_user_id = self.plugin.config.get("specific_user_id")
        if not specific_user_id:
            return "（暂无最近对话记录）"

        # 1. 确定当前活跃会话 session_str
        actual_platform = "aiocqhttp"
        if self.user_unified_origin and ":" in self.user_unified_origin:
            actual_platform = self.user_unified_origin.split(":")[0]

        now_ts = datetime.now().timestamp()
        use_group = False
        target_group_id = None
        if self.last_active_group_id and (
            now_ts - self.last_active_time < 3600
        ):  # 1小时内活跃则视为群聊活跃
            use_group = True
            target_group_id = self.last_active_group_id

        if use_group:
            session_str = f"{actual_platform}:GroupMessage:{target_group_id}"
        else:
            session_str = f"{actual_platform}:FriendMessage:{specific_user_id}"

        try:
            # 2. 从 AstrBot 获取当前会话的原生对话历史
            curr_cid = await self.context.conversation_manager.get_curr_conversation_id(
                session_str
            )
            if not curr_cid:
                return "（暂无最近对话记录）"

            conv = await self.context.conversation_manager.get_conversation(
                session_str, curr_cid
            )
            if not conv or not conv.history:
                return "（暂无最近对话记录）"

            history = json.loads(conv.history)

            # 3. 过滤并提取与特定用户的对话
            filtered_messages = []

            if use_group:
                # 群聊消息过滤：
                # - user 消息必须包含 "User ID: {specific_user_id}"
                # - assistant 消息必须紧跟在合格的 user 消息后面
                i = 0
                while i < len(history):
                    msg = history[i]
                    role = msg.get("role")
                    content = msg.get("content")

                    if role == "user":
                        # 检查是否包含 target user ID
                        matched = False
                        if isinstance(content, str):
                            if f"User ID: {specific_user_id}" in content:
                                matched = True
                        elif isinstance(content, list):
                            for part in content:
                                if (
                                    isinstance(part, dict)
                                    and part.get("type") == "text"
                                ):
                                    if f"User ID: {specific_user_id}" in part.get(
                                        "text", ""
                                    ):
                                        matched = True
                                        break

                        if matched:
                            filtered_messages.append(
                                {"role": "user", "content": content}
                            )
                            # 如果下一条是机器人回复，也抓取进来
                            if (
                                i + 1 < len(history)
                                and history[i + 1].get("role") == "assistant"
                            ):
                                filtered_messages.append(
                                    {
                                        "role": "assistant",
                                        "content": history[i + 1].get("content"),
                                    }
                                )
                                i += 1  # 跳过机器人那一条，避免重复扫描
                    i += 1
            else:
                # 私聊消息直接取全部
                for msg in history:
                    if msg.get("role") in ["user", "assistant"]:
                        filtered_messages.append(
                            {
                                "role": msg.get("role"),
                                "content": msg.get("content"),
                            }
                        )

            # 只保留最近的 limit 条消息
            recent_msgs = filtered_messages[-limit:]
            if not recent_msgs:
                return "（暂无最近对话记录）"

            # 4. 格式化输出
            context_lines = []
            for m in recent_msgs:
                role_label = "【用户】" if m["role"] == "user" else "【你】"
                content = m["content"]

                # 如果是列表多模态消息，合并文字内容
                if isinstance(content, list):
                    text_parts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            text_parts.append(part.get("text", ""))
                    content = "".join(text_parts)

                if not isinstance(content, str):
                    content = ""

                # 去除 <system_reminder> 块和 <reasoning> 等辅助排版标记，保留纯文本
                content = re.sub(
                    r"<system_reminder>.*?</system_reminder>",
                    "",
                    content,
                    flags=re.DOTALL,
                )
                content = re.sub(
                    r"<reasoning>.*?</reasoning>", "", content, flags=re.DOTALL
                )
                content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)
                content = re.sub(
                    r"<RAG-Faiss-Memory>.*?</RAG-Faiss-Memory>",
                    "",
                    content,
                    flags=re.DOTALL,
                )

                # 去除可能的前缀，统一为 "【用户】: ..." 样式
                clean_content = re.sub(r"^【.*?】.*?: ", "", content.strip())
                if clean_content:
                    context_lines.append(f"{role_label}: {clean_content}")

            return "\n".join(context_lines)
        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: 从 AstrBot 历史获取上下文失败: {e}\n{traceback.format_exc()}"
            )
            return "（暂无最近对话记录）"

    async def _wake_native_reply(
        self, reason: str, message_type: str, status_data: str
    ):
        specific_user_id = self.plugin.config.get("specific_user_id")
        if not specific_user_id:
            logger.error("ActiveMessageHandler: 未配置 specific_user_id，无法唤醒。")
            return

        # 平台兼容性处理
        actual_platform = "aiocqhttp"
        if self.user_unified_origin and ":" in self.user_unified_origin:
            actual_platform = self.user_unified_origin.split(":")[0]

        now_ts = datetime.now().timestamp()
        use_group = False
        target_group_id = None
        if self.last_active_group_id and (now_ts - self.last_active_time < 300):
            use_group = True
            target_group_id = self.last_active_group_id

        if use_group:
            session_str = f"{actual_platform}:GroupMessage:{target_group_id}"
            msg_type = MessageType.GROUP_MESSAGE
        else:
            session_str = f"{actual_platform}:FriendMessage:{specific_user_id}"
            msg_type = MessageType.FRIEND_MESSAGE

        # 格式化唤醒 Prompt 作为 Synthetic Message
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        wake_prompt = ACTIVE_MSG_WAKE_PROMPT.format(
            current_time=current_time,
            reason=reason,
            message_type=message_type,
        )

        logger.info(
            f"ActiveMessageHandler: 正在发送合成唤醒事件到队列 (UMO: {session_str}, Reason: {reason})"
        )

        # 创建 Synthetic Event 并提交到 Event Queue
        wake_event = ActiveMessageWakeEvent(
            context=self.context,
            session_str=session_str,
            message=wake_prompt,
            sender_id=str(specific_user_id),
            sender_name="System",
            message_type=msg_type,
        )

        self.context.get_event_queue().put_nowait(wake_event)

import asyncio
import json
import random
import re
import traceback
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.core.star.star_tools import StarTools

from .prompts import (
    ACTIVE_MSG_CHECK_STATUS_PROMPT,
    ACTIVE_MSG_GENERATE_PROMPT,
    ACTIVE_MSG_PREDICT_TIME_PROMPT,
)


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
        logger.info("DailyReportAnalysisAPI: 主动消息机制已启动。")

    def stop(self):
        if self.loop_task:
            self.loop_task.cancel()
            self.loop_task = None

    async def _main_loop(self):
        try:
            while True:
                config = self.plugin.config or {}
                enabled = config.get("enable_active_messaging", False)
                max_msgs = config.get("max_active_messages_per_day", 3)

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
        raw_result = raw_result.strip()
        if raw_result.startswith("```json"):
            raw_result = raw_result[7:]
        elif raw_result.startswith("```"):
            raw_result = raw_result[3:]
        if raw_result.endswith("```"):
            raw_result = raw_result[:-3]
        return json.loads(raw_result.strip())

    async def _predict_first_active_time(self, now: datetime):
        logger.info("ActiveMessageHandler: 正在获取作息推测今日首次活动时间...")
        status_res = await self.api_service.fetch_status(memory="short")
        if not status_res or "prompt" not in status_res:
            logger.error("ActiveMessageHandler: 无法获取 short 状态，30分钟后重试。")
            self.next_check_time = now + timedelta(minutes=30)
            return

        status_data = status_res["prompt"]
        system_prompt = await self._get_system_prompt()
        prompt = ACTIVE_MSG_PREDICT_TIME_PROMPT.format(status_data=status_data)

        provider_id = self.plugin.config.get("summary_provider_id")
        if not provider_id:
            logger.warning(
                "ActiveMessageHandler: 未配置 summary_provider_id，无法推测。"
            )
            self.next_check_time = now + timedelta(minutes=60)
            return

        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=prompt,
        )

        try:
            result_json = self._parse_json(response.completion_text)
            time_str = result_json.get("next_check_time", "")
            if ":" in time_str:
                h, m = map(int, time_str.split(":"))
                check_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
                if check_time <= now:
                    # 如果推测的时间比现在早，且时间差超过 12 小时（比如现在是深夜，推测明天早上的活动），则算作明天
                    # 否则（比如现在是早上 10 点，推测 8 点半），说明是今天的活动时间被错过了，保留为今天以便系统立刻触发补偿检查
                    if (now - check_time).total_seconds() > 12 * 3600:
                        check_time += timedelta(days=1)
                self.next_check_time = check_time
                logger.info(
                    f"ActiveMessageHandler: 推测出下一次活动时间为 {self.next_check_time}"
                )
            else:
                raise ValueError("Invalid time format")
        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: 推测时间失败 {e}。LLM 返回: {response.completion_text}"
            )
            self.next_check_time = now + timedelta(minutes=60)

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

        system_prompt = await self._get_system_prompt()
        prompt = ACTIVE_MSG_CHECK_STATUS_PROMPT.format(
            current_time=current_time,
            status_data=status_data,
            conversation_context=conversation_context,
        )

        provider_id = self.plugin.config.get("summary_provider_id")
        if not provider_id:
            return False

        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=prompt,
        )

        try:
            result_json = self._parse_json(response.completion_text)
            need_message = result_json.get("need_message", False)
            reason = result_json.get("reason", "")
            message_type = result_json.get("message_type", "care")

            if need_message:
                logger.info(
                    f"ActiveMessageHandler: 判定需要发消息，类型：{message_type}，理由：{reason}。准备生成回复..."
                )
                await self._generate_and_send_message(reason, message_type, status_data)
                return True
            else:
                delay_minutes = result_json.get("delay_minutes", 0)
                if (
                    delay_minutes
                    and isinstance(delay_minutes, (int, float))
                    and delay_minutes > 0
                ):
                    try:
                        now = datetime.now()
                        self.next_check_time = now + timedelta(
                            minutes=int(delay_minutes)
                        )
                        self.db.update_plugin_meta(
                            "active_msg_next_check_time",
                            self.next_check_time.isoformat(),
                        )
                        logger.info(
                            f"ActiveMessageHandler: 遵循模型建议，将在 {delay_minutes} 分钟后（{self.next_check_time}）再次观察 (理由: {reason})"
                        )
                        return False
                    except Exception as e:
                        logger.debug(f"应用延迟时间失败: {e}")

                logger.info("ActiveMessageHandler: 判定不需要发消息。")
                return False

        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: 解析评估状态失败 {e}。LLM 返回: {response.completion_text if 'response' in locals() else 'No response'}"
            )
            return False

    def _clean_message_content(self, content):
        """清洗消息内容，去除 think 块、system_reminder 和 JSON 结构"""
        if isinstance(content, list):
            # 处理 AstrBot 的组件化消息格式
            text_parts = []
            for part in content:
                if isinstance(part, dict):
                    if part.get("type") == "text":
                        text_parts.append(part.get("text", ""))
                    # 忽略 type 为 think 的部分
                elif isinstance(part, str):
                    text_parts.append(part)
            content = "".join(text_parts)

        if not isinstance(content, str):
            return ""

        # 去除 <system_reminder>...</system_reminder> 及其内容
        content = re.sub(
            r"<system_reminder>.*?</system_reminder>", "", content, flags=re.DOTALL
        )
        # 去除可能的 Markdown 思考块
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL)

        return content.strip()

    async def _get_recent_conversation_context(self, limit: int = 15) -> str:
        """获取最近的合并对话上下文（私聊+群聊中与用户的互动）并格式化为字符串"""
        specific_user_id = self.plugin.config.get("specific_user_id")
        if not specific_user_id:
            return "（暂无最近对话记录）"

        try:
            # 使用合并后的记录
            messages = self.db.get_recent_combined_messages(
                str(specific_user_id), limit=limit
            )
            if not messages:
                return "（暂无最近对话记录）"

            context_lines = []
            for m in messages:
                role_label = "用户" if m["role"] == "user" else "你"
                ts = m.get("timestamp")
                time_prefix = ""
                if ts:
                    time_prefix = (
                        f"[{datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')}] "
                    )
                
                content = m["content"]
                # 统一格式：去除可能存在的 【用户】Nickname: 或 【你】Nickname: 前缀，实现合并后无额外标注
                # 这样无论是私聊还是群聊，在上下文中都呈现为 "用户: ..." 或 "你: ..."
                clean_content = re.sub(r"^【.*?】.*?: ", "", content)
                context_lines.append(f"{time_prefix}{role_label}: {clean_content}")
            return "\n".join(context_lines)
        except Exception as e:
            logger.error(f"ActiveMessageHandler: 获取合并上下文失败: {e}\n{traceback.format_exc()}")
            return "（暂无最近对话记录）"

    async def _generate_and_send_message(
        self, reason: str, message_type: str = "care", short_data: str = ""
    ):
        if message_type == "chat":
            hybrid_res = await self.api_service.fetch_status(memory="hybrid")
            if not hybrid_res or "prompt" not in hybrid_res:
                logger.error(
                    "ActiveMessageHandler: 获取 hybrid 状态失败，退回使用 short 状态。"
                )
                memory_data = short_data
            else:
                memory_data = hybrid_res["prompt"]
        else:
            memory_data = short_data
        
        system_prompt = await self._get_system_prompt()
        conversation_context = await self._get_recent_conversation_context(limit=15)
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        # 判断发送频道：如果 5 分钟内在群里活跃过，则发到群里并 @
        now_ts = datetime.now().timestamp()
        use_group = False
        target_group_id = None
        if self.last_active_group_id and (now_ts - self.last_active_time < 300):
            use_group = True
            target_group_id = self.last_active_group_id

        gen_prompt = ACTIVE_MSG_GENERATE_PROMPT.format(
            current_time=current_time,
            reason=reason,
            memory_data=memory_data,
            conversation_context=conversation_context if conversation_context else "（暂无最近对话记录）",
        )
        
        if use_group:
            gen_prompt += f"\n\n注意：这条消息将发送到群聊（{target_group_id}）并 @ 用户，请以此氛围回复。"

        provider_id = self.plugin.config.get("summary_provider_id")
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=gen_prompt,
        )

        message_content = response.completion_text.strip()
        if not message_content:
            return

        logger.info(f"ActiveMessageHandler: 生成主动消息：{message_content} (频道: {'群聊' if use_group else '私聊'})")

        specific_user_id = self.plugin.config.get("specific_user_id")
        if not specific_user_id: return

        try:
            from astrbot.api.message_components import At, Plain
            from astrbot.core.message.message_event_result import MessageChain

            # 平台兼容性处理
            target_platform = "aiocqhttp"
            if self.user_unified_origin and ":" in self.user_unified_origin:
                raw_platform = self.user_unified_origin.split(":")[0]
                if raw_platform.lower() in ["arisu", "onebot", "gocq"]:
                    target_platform = "aiocqhttp"
                else:
                    target_platform = raw_platform

            if use_group:
                chain = MessageChain(chain=[At(qq=specific_user_id), Plain(f" {message_content}")])
                await StarTools.send_message_by_id(
                    type="GroupMessage",
                    id=target_group_id,
                    message_chain=chain,
                    platform=target_platform,
                )
                
                # 记录到群消息数据库
                group_meta = self.db.get_group_meta(target_group_id)
                bot_name = (group_meta[4] if group_meta and len(group_meta) > 4 else "机器人") or "机器人"
                msg_content_formatted = f"【你】{bot_name}: {message_content}"
                
                new_msg_id = (group_meta[2] if group_meta else 0) + 1
                
                self.db.add_group_message(
                    target_group_id,
                    new_msg_id,
                    "bot",
                    bot_name,
                    msg_content_formatted,
                    datetime.now().timestamp(),
                    "active_msg_" + str(int(datetime.now().timestamp())),
                    is_specific_user=True
                )
                self.db.update_group_meta(target_group_id, message_id_counter=new_msg_id)
            else:
                chain = MessageChain().message(message_content)
                await StarTools.send_message_by_id(
                    type="FriendMessage",
                    id=specific_user_id,
                    message_chain=chain,
                    platform=target_platform,
                )
                # 记录到私聊数据库
                self.db.add_private_message(
                    str(specific_user_id),
                    "bot",
                    message_content,
                    datetime.now().timestamp(),
                )

            # 统一写入对话历史（AstrBot 核心缓存）
            if self.user_unified_origin:
                try:
                    # 获取当前会话正在使用的对话 ID
                    curr_cid = await self.context.conversation_manager.get_curr_conversation_id(self.user_unified_origin)
                    if curr_cid:
                        conv = await self.context.conversation_manager.get_conversation(
                            self.user_unified_origin, curr_cid
                        )
                        if conv:
                            history = json.loads(conv.history)
                            history.append({"role": "assistant", "content": message_content})
                            await self.context.conversation_manager.update_conversation(
                                self.user_unified_origin, curr_cid, history=history
                            )
                            logger.info(f"ActiveMessageHandler: 已将主动消息写入对话历史 (Session: {self.user_unified_origin}, CID: {curr_cid})。")
                except Exception as e:
                    logger.error(f"ActiveMessageHandler: 写入对话历史失败: {e}\n{traceback.format_exc()}")

            logger.info("ActiveMessageHandler: 主动消息发送成功。")

        except Exception as e:
            logger.error(f"ActiveMessageHandler: 发送主动消息失败：{e}\n{traceback.format_exc()}")

import asyncio
import json
import traceback
import random
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.star.star_tools import StarTools

PROMPT_CHECK_STATUS = """任务目标：根据以下角色的当前实时状态记录，判断现在是否是一个合适的时机去主动发消息关怀用户，或者发起闲聊。
- “关怀”（care）：例如提醒休息、提醒运动、早安晚安问候等。
- “闲聊”（chat）：如果角色距离当前时间很近的活动是在 QQ 或者其他娱乐项目（如打游戏、看视频），可以考虑发起闲聊，假装不经意间提起某件事。

如果当前不适合发消息，请将 need_message 置为 false。

输出 JSON 格式要求（必须只返回合法的 JSON 对象，不带 Markdown 符号等包裹）：
{{
  "need_message": true, // 是否需要立刻发送主动消息
  "message_type": "chat", // "care" 或 "chat"（如果不发消息可为空）
  "reason": "用户正在看视频，可以假装不经意问一下在看什么或者分享个趣事" // 简短说明理由
}}

--- 状态记录开始 ---
{status_data}
--- 状态记录结束 ---"""

PROMPT_CHECK_STATUS = """任务目标：根据以下角色的当前实时状态记录，判断现在是否是一个合适的时机去主动发消息关怀用户，或者发起闲聊。
- “关怀”（care）：例如提醒休息、提醒运动、早安晚安问候等。
- “闲聊”（chat）：如果角色距离当前时间很近的活动是在 QQ 或者其他娱乐项目（如打游戏、看视频），可以考虑发起闲聊，假装不经意间提起某件事。

如果你觉得当前不需要打扰，请判断下次什么时间再来观察（如果判断今天都不合适，可以将 continue_observing 设为 false）。

输出 JSON 格式要求（必须只返回合法的 JSON 对象，不带 Markdown 符号等包裹）：
{{
  "need_message": true, // 是否需要立刻发送主动消息
  "message_type": "chat", // "care" 或 "chat"（如果不发消息可为空）
  "reason": "用户正在看视频，可以假装不经意问一下在看什么或者分享个趣事", // 简短说明理由
  "continue_observing": false, // 如果 need_message 为 false，是否需要后续继续观察
  "next_check_time": "12:30" // 格式为 HH:MM，24小时制。如果不继续观察，该字段可为空
}}

--- 状态记录开始 ---
{status_data}
--- 状态记录结束 ---"""


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
            self.next_check_time = datetime.fromisoformat(next_check_str) if next_check_str else datetime.now() + timedelta(minutes=1)
            
            self.messages_sent_today = int(self.db.get_plugin_meta("active_msg_sent_today", 0))
            
            last_reset_str = self.db.get_plugin_meta("active_msg_last_reset_date")
            self.last_reset_date = datetime.strptime(last_reset_str, "%Y-%m-%d").date() if last_reset_str else datetime.now().date()
            
            self._user_unified_origin = self.db.get_plugin_meta("active_msg_user_origin")
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
                    self.db.update_plugin_meta("active_msg_last_reset_date", self.last_reset_date.isoformat())

                if self.messages_sent_today >= max_msgs:
                    next_day = now + timedelta(days=1)
                    self.next_check_time = next_day.replace(hour=0, minute=0, second=0, microsecond=0)
                    self.db.update_plugin_meta("active_msg_next_check_time", self.next_check_time.isoformat())

                if now >= self.next_check_time:
                    sent = False
                    if self.messages_sent_today < max_msgs:
                        sent = await self._check_and_action()
                        if sent:
                            self.messages_sent_today += 1
                            self.db.update_plugin_meta("active_msg_sent_today", self.messages_sent_today)
                            logger.info(f"ActiveMessageHandler: 今日已发送主动消息 {self.messages_sent_today}/{max_msgs} 条")
                    
                    if sent:
                        # 发送成功后，拉长下一次轮询的间隔
                        self.reset_polling(min_int=60, max_int=120, reason="发送消息")
                    else:
                        # 未发送（观望中），保持原有频率
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
        min_interval = min_int if min_int is not None else config.get("active_msg_min_interval", 30)
        max_interval = max_int if max_int is not None else config.get("active_msg_max_interval", 60)
        offset_minutes = random.randint(min_interval, max_interval)
        self.next_check_time = datetime.now() + timedelta(minutes=offset_minutes)
        
        # 持久化
        self.db.update_plugin_meta("active_msg_next_check_time", self.next_check_time.isoformat())
        
        logger.info(f"ActiveMessageHandler: 收到{reason}，已重置主动消息轮询时间至 {self.next_check_time} ({offset_minutes}分钟后)")

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
        prompt = PROMPT_PREDICT_TIME.format(status_data=status_data)

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
        logger.info(
            f"ActiveMessageHandler: 到达观察时间，正在评估状态..."
        )
        now = datetime.now()
        status_res = await self.api_service.fetch_status(memory="short")
        if not status_res or "prompt" not in status_res:
            logger.error("ActiveMessageHandler: 无法获取 short 状态，稍后重试。")
            return False

        status_data = status_res["prompt"]
        system_prompt = await self._get_system_prompt()
        prompt = PROMPT_CHECK_STATUS.format(status_data=status_data)

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
                logger.info("ActiveMessageHandler: 判定不需要发消息。")
                return False

        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: 解析评估状态失败 {e}。LLM 返回: {response.completion_text}"
            )
            return False

    def _clean_message_content(self, content):
        """清洗消息内容，去除 think 块、system_reminder 和 JSON 结构"""
        import re
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
        content = re.sub(r'<system_reminder>.*?</system_reminder>', '', content, flags=re.DOTALL)
        # 去除可能的 Markdown 思考块
        content = re.sub(r'<think>.*?</think>', '', content, flags=re.DOTALL)
        
        return content.strip()

    async def _generate_and_send_message(self, reason: str, message_type: str = "care", short_data: str = ""):
        if message_type == "chat":
            hybrid_res = await self.api_service.fetch_status(memory="hybrid")
            if not hybrid_res or "prompt" not in hybrid_res:
                logger.error("ActiveMessageHandler: 获取 hybrid 状态失败，退回使用 short 状态。")
                memory_data = short_data
            else:
                memory_data = hybrid_res["prompt"]
        else:
            # 关怀类直接使用短时记忆
            memory_data = short_data
        system_prompt = await self._get_system_prompt()
        
        # 获取最近的私聊对话上下文
        conversation_context = ""
        unified_origin = self.user_unified_origin
        if not unified_origin:
             # 如果还没收到过消息，尝试在数据库中寻找包含该用户 ID 的会话
             specific_user_id = self.plugin.config.get("specific_user_id")
             if specific_user_id:
                 try:
                     # 优先尝试常见的几种拼凑方式
                     possible_origins = [
                         f"aiocqhttp:person:{specific_user_id}",
                         f"onebot:person:{specific_user_id}",
                         f"qq_official:person:{specific_user_id}"
                     ]
                     
                     for po in possible_origins:
                         temp_cid = await self.context.conversation_manager.get_curr_conversation_id(po)
                         if temp_cid:
                             unified_origin = po
                             break
                     
                     if not unified_origin:
                         # 最后的兜底：遍历所有近期会话寻找匹配项
                         all_convs = await self.context.conversation_manager.get_filtered_conversations(page_size=100)
                         for conv_obj, _cnt in [all_convs] if isinstance(all_convs, tuple) else [(all_convs, 0)]:
                             for c in conv_obj:
                                 if str(specific_user_id) in c.user_id:
                                     unified_origin = c.user_id
                                     break
                             if unified_origin: break
                 except Exception as e:
                     logger.error(f"ActiveMessageHandler: 自动搜索会话来源失败: {e}")
        
        logger.info(f"ActiveMessageHandler: 确定的会话来源为: {unified_origin}")
        
        cid = None
        if unified_origin:
            try:
                cid = await self.context.conversation_manager.get_curr_conversation_id(unified_origin)
                logger.info(f"ActiveMessageHandler: 获取到的对话 ID (cid): {cid}")
                if cid:
                    conv = await self.context.conversation_manager.get_conversation(unified_origin, cid)
                    if conv:
                        history = json.loads(conv.history)
                        # 取最近 10 条
                        recent = history[-10:]
                        for m in recent:
                            role = "用户" if m["role"] == "user" else "你"
                            raw_content = m.get("content", "")
                            content = self._clean_message_content(raw_content)
                            if content:
                                conversation_context += f"{role}: {content}\n"
                        logger.info(f"ActiveMessageHandler: 成功提取到 {len(recent)} 条对话上下文。")
            except Exception as e:
                logger.error(f"ActiveMessageHandler: 获取对话上下文失败: {e}")

        gen_prompt = f"""任务目标：根据你的人设以及下面的角色实时状态与历史记忆档案，主动给用户发一条消息。
当前的发送动机是：{reason}

--- 状态与记忆档案 ---
{memory_data}
--- 档案结束 ---

--- 最近的私聊对话上下文 ---
{conversation_context if conversation_context else "（暂无最近对话记录）"}
--- 上下文结束 ---

【重要字数与风格限制】：
1. 必须非常简短，最多 1-2 句话（尽量在 30 个字以内）。
2. 就像朋友日常聊天随意开场一样，不要长篇大论，不要写成小作文。
3. 最好用一个简单的问候、一个小发现或一个轻松的提问来结尾，目的是“让用户愿意轻松地回复你”。

请直接输出你要发送的消息内容，不要有任何 Markdown 包裹或附加的说明文字。"""

        logger.info(f"ActiveMessageHandler: 准备发送给大模型的生成提示词：\n{gen_prompt}")

        provider_id = self.plugin.config.get("summary_provider_id")
        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=gen_prompt,
        )

        message_content = response.completion_text.strip()
        if not message_content:
            return

        logger.info(f"ActiveMessageHandler: 生成主动消息：{message_content}")

        # 发送消息
        specific_user_id = self.plugin.config.get("specific_user_id")
        if specific_user_id:
            try:
                from astrbot.core.message.message_event_result import MessageChain

                chain = MessageChain().message(message_content)
                # 这里我们假设用户是以私聊为主，目标是 specific_user_id
                await StarTools.send_message_by_id(
                    type="FriendMessage",
                    id=specific_user_id,
                    message_chain=chain,
                    platform="aiocqhttp",
                )
                logger.info("ActiveMessageHandler: 主动消息发送成功。")
                
                # 记录到对话历史中
                if cid and unified_origin:
                    try:
                        conv = await self.context.conversation_manager.get_conversation(unified_origin, cid)
                        if conv:
                             history = json.loads(conv.history)
                             history.append({"role": "assistant", "content": message_content})
                             await self.context.conversation_manager.update_conversation(unified_origin, cid, history=history)
                             logger.info("ActiveMessageHandler: 已将主动消息写入对话历史。")
                    except Exception as e:
                         logger.error(f"ActiveMessageHandler: 写入对话历史失败: {e}")

                # 将这条主动发送的消息写入 private_messages 中
                time_str = datetime.now().strftime("%H:%M")
                # 因为是主动发消息，"用户" 可以为空或者注明是主动发起
                self.plugin.private_messages.append(
                    {
                        "时间": time_str,
                        "用户": "[机器人主动发起]",
                        "你的回复": message_content,
                    }
                )
                self.plugin._save_data()

            except Exception as e:
                logger.error(
                    f"ActiveMessageHandler: 发送主动消息失败：{e}\n{traceback.format_exc()}"
                )

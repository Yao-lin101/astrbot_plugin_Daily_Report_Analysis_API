import asyncio
import json
from datetime import datetime

from astrbot.api import logger

from .message_utils import get_bot_nickname
from .prompts import PRIVATE_SUMMARY_PROMPT_TEMPLATE, SUMMARY_PROMPT_TEMPLATE


class Summarizer:
    def __init__(self, plugin):
        self.plugin = plugin
        self.context = plugin.context
        self.db = plugin.db
        self.config = plugin.config
        self.api_service = plugin.api_service

    async def summarize_single_group(self, group_id):
        """对增量消息进行总结，但快照点维持在特定用户最后一次发言"""
        if group_id not in self.plugin.last_summarized_id:
            self.plugin._get_group_context(group_id)

        last_id = self.plugin.last_summarized_id.get(group_id, 0)
        pending = self.db.get_pending_messages(group_id, last_id, limit=200)

        if not pending:
            self.plugin.active_groups.discard(group_id)
            return

        specific_user_id = str(self.config.get("specific_user_id", ""))

        # 寻找特定用户在本次待处理消息中的活动范围
        first_user_msg_index = -1
        last_user_msg_index = -1
        for i in range(len(pending)):
            if str(pending[i].get("sender_id")) == specific_user_id:
                if first_user_msg_index == -1:
                    first_user_msg_index = i
                last_user_msg_index = i

        if last_user_msg_index == -1:
            self.plugin.active_groups.discard(group_id)
            return

        # 优化：向前追溯背景，避免无关消息干扰（保留用户首条发言前 15 条消息）
        context_start = max(0, first_user_msg_index - 15)
        to_summarize = pending[context_start:]

        # 严格连续消息预合并
        merged_messages = []
        for msg in to_summarize:
            if merged_messages and merged_messages[-1]["sender_id"] == msg["sender_id"]:
                # 连续发送，追加内容
                content_parts = msg["content"].split(": ", 1)
                text_to_add = (
                    content_parts[-1] if len(content_parts) > 1 else msg["content"]
                )
                merged_messages[-1]["content"] += "，" + text_to_add
            else:
                merged_messages.append(msg.copy())

        user_nickname = self.plugin.user_nicknames.get(group_id, "用户")
        event = self.plugin.group_events.get(group_id)
        bot_nickname = await get_bot_nickname(
            self.context, event, group_id, self.plugin.bot_nicknames
        )

        group_name = self.plugin.group_names.get(group_id, "未知群聊")
        dialogue_text = "\n".join(
            [f"[{m['id']}] {m['content']}" for m in merged_messages]
        )

        logger.debug(
            f"DailyReportAnalysisAPI: 喂给 LLM 的对话文本详情:\n---\n{dialogue_text}\n---"
        )

        provider_id = self.config.get("summary_provider_id")
        if provider_id:
            try:
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

                prompt = SUMMARY_PROMPT_TEMPLATE.format(
                    bot_nickname=bot_nickname,
                    user_nickname=user_nickname,
                    dialogue_text=dialogue_text,
                )

                response = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    system_prompt=system_prompt,
                    prompt=prompt,
                )
                raw_result = response.completion_text.strip()

                # 尝试剥离 Markdown 包裹
                if raw_result.startswith("```json"):
                    raw_result = raw_result[7:]
                elif raw_result.startswith("```"):
                    raw_result = raw_result[3:]
                if raw_result.endswith("```"):
                    raw_result = raw_result[:-3]
                raw_result = raw_result.strip()

                try:
                    result_json = json.loads(raw_result)
                except json.JSONDecodeError:
                    logger.error(
                        f"DailyReportAnalysisAPI: LLM 返回的不是合法 JSON: {raw_result}"
                    )
                    result_json = {
                        "status": "IGNORED",
                        "next_start_id": to_summarize[-1]["id"],
                    }

                status = result_json.get("status", "IGNORED")
                topics = result_json.get("topics", [])
                next_start_id = result_json.get("next_start_id", to_summarize[-1]["id"])

                # --- 安全检查：防止 LLM 幻觉导致 ID 越界 ---
                max_valid_id = to_summarize[-1]["id"]
                min_valid_id = to_summarize[0]["id"]

                if not isinstance(next_start_id, int):
                    try:
                        next_start_id = int(next_start_id)
                    except (ValueError, TypeError):
                        next_start_id = max_valid_id

                if next_start_id > max_valid_id:
                    logger.warning(
                        f"DailyReportAnalysisAPI: LLM 返回的 ID {next_start_id} 超过上限 {max_valid_id}，已修正。"
                    )
                    next_start_id = max_valid_id
                elif next_start_id < min_valid_id:
                    logger.warning(
                        f"DailyReportAnalysisAPI: LLM 返回的 ID {next_start_id} 低于下限 {min_valid_id}，已修正。"
                    )
                    next_start_id = max_valid_id  # 默认移动到末尾，防止死循环
                # ------------------------------------------

                logger.info(
                    f"DailyReportAnalysisAPI: LLM判定状态={status}, topics_count={len(topics)}, next_start_id={next_start_id}"
                )

                last_time = datetime.fromtimestamp(
                    to_summarize[-1]["timestamp"]
                ).strftime("%H:%M")

                if status == "COMPLETED" and topics:
                    # 只有在总结成功且有话题时，才更新进度
                    self.plugin.last_summarized_id[group_id] = next_start_id
                    self.db.update_group_meta(
                        group_id, last_summarized_id=next_start_id
                    )

                    for t in topics:
                        topic_str = t.get("topic", "未知话题")
                        content_str = t.get("content", "")
                        data = {
                            "type": "qq_messages",
                            "data": {
                                "group_messages": [
                                    {
                                        "时间": last_time,
                                        "群名称": group_name,
                                        "用户在本群昵称": user_nickname,
                                        "你在本群昵称": bot_nickname,
                                        "话题总结": f"话题：{topic_str}\n内容：{content_str}",
                                    }
                                ]
                            },
                        }
                        await self.api_service.send_data("/api/v1/status/sync/", data)

                # 更新进度
                self.plugin.last_summarized_id[group_id] = next_start_id
                self.db.update_group_meta(group_id, last_summarized_id=next_start_id)

                if status == "ONGOING":
                    # 主动重试（重新发起30分钟计时）
                    if group_id in self.plugin.group_timers:
                        self.plugin.group_timers[group_id].cancel()
                    self.plugin.group_timers[group_id] = asyncio.create_task(
                        self.plugin._delay_summarize_task(group_id, 1800)
                    )

            except Exception as e:
                logger.error(f"DailyReportAnalysisAPI: 总结处理失败: {e}")
                # 出现异常时也安全推进，防止卡死
                self.plugin.last_summarized_id[group_id] = to_summarize[-1]["id"]
                self.db.update_group_meta(
                    group_id, last_summarized_id=to_summarize[-1]["id"]
                )

        self.plugin.active_groups.discard(group_id)

    async def summarize_private_messages(self):
        specific_user_id = str(self.config.get("specific_user_id", ""))
        if not specific_user_id:
            return False

        # 1. 获取上一次总结的最后一条消息 ID
        last_id_str = self.db.get_plugin_meta("last_private_summarized_id", "0")
        last_id = int(last_id_str)

        # 2. 从数据库获取自上次以来的新私聊记录
        messages = self.db.get_pending_private_messages(
            specific_user_id, last_id, limit=50
        )
        if not messages:
            return False

        # 组装对话文本用于 LLM
        dialogue_text = ""
        user_msgs = []
        for m in messages:
            role_label = "用户" if m["role"] == "user" else "你"
            dialogue_text += f"{role_label}: {m['content']}\n"
            if m["role"] == "user":
                user_msgs.append(m["content"])

        first_time = datetime.fromtimestamp(messages[0]["timestamp"]).strftime("%H:%M")
        provider_id = self.config.get("summary_provider_id")

        summary_topic = ""
        summary_content = ""

        if len(messages) <= 2:
            # 对话较短，直接拼接
            summary_topic = "私聊互动"
            summary_content = " / ".join(user_msgs)
        else:
            # 多轮对话，调用 LLM 进行压缩精简
            if provider_id:
                try:
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

                    prompt = PRIVATE_SUMMARY_PROMPT_TEMPLATE.format(
                        dialogue_text=dialogue_text
                    )
                    response = await self.context.llm_generate(
                        chat_provider_id=provider_id,
                        system_prompt=system_prompt,
                        prompt=prompt,
                    )

                    raw_result = response.completion_text.strip()
                    # 清理 Markdown 标记
                    if raw_result.startswith("```json"):
                        raw_result = raw_result[7:]
                    elif raw_result.startswith("```"):
                        raw_result = raw_result[3:]
                    if raw_result.endswith("```"):
                        raw_result = raw_result[:-3]

                    try:
                        result_json = json.loads(raw_result.strip())
                        summary_topic = result_json.get("topic", "")
                        summary_content = result_json.get("content", "")
                    except Exception:
                        logger.error(
                            f"DailyReportAnalysisAPI: 私聊 LLM 返回解析失败: {raw_result}"
                        )
                except Exception as e:
                    logger.error(f"DailyReportAnalysisAPI: 私聊总结处理失败: {e}")

        if len(messages) <= 2:
            # 短对话：恢复原有的“问答对”格式
            user_content = ""
            bot_content = ""
            for m in messages:
                if m["role"] == "user":
                    user_content = m["content"]
                else:
                    bot_content = m["content"]

            payload_dict = {
                "时间": first_time,
                "用户": user_content or "（机器人主动发起）",
                "你的回复": bot_content,
            }
        else:
            # 最终保底逻辑 (长对话)
            if not summary_topic or not summary_content:
                summary_topic = "私聊对话"
                summary_content = " / ".join(user_msgs)[:100]

            payload_dict = {
                "时间": first_time,
                "话题": summary_topic,
                "总结": summary_content,
            }

        # 上报
        data = {
            "type": "qq_messages",
            "data": {"private_messages": [payload_dict]},
        }

        try:
            await self.api_service.send_data("/api/v1/status/sync/", data)

            # 3. 上报成功后，记录最新总结到的 ID 进度
            new_last_id = messages[-1]["id"]
            self.db.update_plugin_meta("last_private_summarized_id", new_last_id)

            logger.info(
                f"DailyReportAnalysisAPI: 私聊总结已成功上报，当前进度 ID={new_last_id}。"
            )
            return True
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 私聊总结上报失败: {e}")
            return False

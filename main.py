import asyncio
import inspect
from collections import defaultdict
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Node, Nodes, Plain, Reply
from astrbot.api.star import Context, Star, register

from .api_service import APIService
from .message_utils import format_full_message, get_bot_nickname
from .report_handler import ReportHandler


@register(
    "astrbot_plugin_Daily_Report_Analysis_API",
    "e.e.",
    "联动StillAlive发送每日群聊以及与AI机器人私聊的消息汇总，并支持获取日报图片。",
    "1.3.0",
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
        self.group_names = {}
        self.group_events = {}
        self.group_timers = {}
        self.active_groups = set()

        self.private_messages = []
        self.config = config
        self.internal_commands = []
        self.api_service = None

    def _get_resp(self, key: str, default: str = "", **kwargs) -> str:
        """从配置获取回复模板并格式化"""
        tmpl = self.config.get(key, default)
        try:
            return tmpl.format(**kwargs)
        except Exception:
            return tmpl

    async def initialize(self):
        """插件初始化"""
        if not self.config:
            self.config = self.context.get_config()

        # 初始化 API 服务
        target_url = self.config.get("target_url", "")
        character_key = self.config.get("character_key", "")
        self.api_service = APIService(target_url, character_key)

        # 自动识别插件内注册的所有指令名，实现自动屏蔽
        self._auto_collect_internal_commands()

        logger.info(
            f"DailyReportAnalysisAPI: 插件已初始化。监控用户ID: {self.config.get('specific_user_id')}, 目标URL: {target_url}, 已自动注册屏蔽指令: {self.internal_commands}"
        )

    def _auto_collect_internal_commands(self):
        """通过反射获取所有被 @filter.command 装饰的指令名"""
        self.internal_commands = []
        for name, method in inspect.getmembers(self, predicate=inspect.ismethod):
            if hasattr(method, "__astr_filter__"):
                filt = getattr(method, "__astr_filter__")
                if hasattr(filt, "commands"):
                    self.internal_commands.extend(filt.commands)

        if not self.internal_commands:
            self.internal_commands = [
                "stillalive群总结",
                "stillalive清理缓存",
                "stillalive日报",
            ]
        else:
            self.internal_commands = list(set(self.internal_commands))

    async def terminate(self):
        """插件销毁"""
        for timer in self.group_timers.values():
            timer.cancel()
        self.group_timers.clear()

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为特定用户"""
        sender_id = str(event.get_sender_id())
        specific_user_id = str(self.config.get("specific_user_id", ""))
        return sender_id == specific_user_id

    def _get_group_name(self, event: AstrMessageEvent) -> str:
        """获取群名称，优先从事件获取，其次从缓存，最后保底"""
        group_id = event.message_obj.group_id
        if not group_id:
            return "未知群聊"

        group_name = None
        if event.message_obj.group and event.message_obj.group.group_name:
            group_name = event.message_obj.group.group_name
            self.group_names[group_id] = group_name

        if not group_name:
            group_name = self.group_names.get(group_id, "未知群聊")

        return group_name

    @filter.command("stillalive日报")
    async def get_stillalive_report(self, event: AstrMessageEvent, date: str = None):
        """获取并发送指定日期的日报图片。格式: stillalive日报 [YYYY-MM-DD]"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

        # 验证日期格式
        try:
            datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            yield event.plain_result(self._get_resp("resp_invalid_date"))
            return

        yield event.plain_result(self._get_resp("resp_daily_loading", date=date))

        report_data = await self.api_service.fetch_report(date)
        if not report_data:
            yield event.plain_result(self._get_resp("resp_daily_conn_error", date=date))
            return

        # 检查错误字段 (包含 error 和 detail)
        error_msg = report_data.get("error") or report_data.get("detail")
        if error_msg:
            # 针对性提示：日报不存在
            if "日报不存在" in error_msg or "No DailyReport matches" in error_msg:
                yield event.plain_result(
                    self._get_resp("resp_daily_not_found", date=date)
                )
            else:
                yield event.plain_result(
                    self._get_resp("resp_daily_unknown_error", error=error_msg)
                )
            return

        image_path = await ReportHandler.render_report(report_data)
        if image_path:
            from pathlib import Path

            try:
                # 读取图片
                p = Path(image_path)
                if p.exists():
                    try:
                        # 第一层保护：尝试发送图片
                        chain = event.chain_result([Image.fromFileSystem(str(p))])
                        await event.send(chain)
                    except Exception as e:
                        logger.error(
                            f"DailyReport: 图片发送失败，尝试发送转发文本: {e}"
                        )

                        # 第二层保护：尝试以合并转发形式发送文本总结
                        # 明确优先级：markdown -> report_md -> report_markdown
                        report_text = (
                            report_data.get("markdown")
                            or report_data.get("report_md")
                            or report_data.get("report_markdown")
                        )

                        # 如果都没有，尝试从 report_html 中剥离标签作为保底文本
                        if not report_text and "report_html" in report_data:
                            import re

                            html = report_data["report_html"]
                            # 先将块级标签和换行标签替换为真正的换行符
                            html = re.sub(r"<(br|p|div|li|h[1-6])[^>]*>", "\n", html)
                            html = re.sub(r"</(p|div|li|h[1-6])>", "\n", html)
                            report_text = re.sub(r"<[^>]+>", "", html)
                            report_text = report_text.replace("&nbsp;", " ").strip()
                            # 压缩过多的连续换行
                            report_text = re.sub(r"\n\s*\n", "\n\n", report_text)

                        if report_text:
                            # 确定机器人账号 (容错处理)
                            bot_id = str(
                                getattr(
                                    event, "robot_id", getattr(event, "self_id", "0")
                                )
                            )
                            try:
                                logger.info(
                                    f"DailyReport: 正在尝试发送合并转发消息 (内容长度: {len(report_text)})"
                                )
                                # 构造转发节点
                                node = Node(content=[Plain(report_text)])
                                node.name = "甜筒爱丽丝"
                                node.uin = bot_id
                                nodes = Nodes(nodes=[node])
                                await event.send(event.chain_result([nodes]))
                                logger.info("DailyReport: 合并转发报告发送成功")
                                return
                            except Exception as e_node:
                                logger.error(
                                    f"DailyReport: 合并转发发送失败 ({e_node})，尝试直接发送纯文本报告"
                                )
                                try:
                                    # 如果转发失败，尝试直接发纯文本（虽然会很长）
                                    await event.send(event.plain_result(report_text))
                                    return
                                except Exception as e_plain:
                                    logger.error(
                                        f"DailyReport: 纯文本回退发送也失败了: {e_plain}"
                                    )

                        # 第三层保护：最后的报错提示
                        yield event.plain_result(
                            self._get_resp(
                                "resp_image_transmit_error",
                                error="图片发送失败，且文本回退发送均未成功。",
                            )
                        )
                else:
                    yield event.plain_result(
                        self._get_resp("resp_image_file_not_found")
                    )
            except Exception as e:
                logger.error(f"处理图片发送失败: {e}")
                yield event.plain_result(
                    self._get_resp("resp_image_transmit_error", error=str(e))
                )
        else:
            yield event.plain_result(self._get_resp("resp_render_error"))

    @filter.command("stillalive群总结")
    async def manual_group_summary(self, event: AstrMessageEvent):
        """手动触发当前群聊的总结"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result(self._get_resp("resp_group_only"))
            return

        messages = self.group_messages_map.get(group_id, [])
        last_id = self.last_summarized_id.get(group_id, 0)
        pending_messages = [m for m in messages if m["id"] > last_id]

        specific_user_id = str(self.config.get("specific_user_id", ""))
        has_specific_user = any(
            m["sender_id"] == specific_user_id for m in pending_messages
        )

        if not has_specific_user:
            yield event.plain_result(self._get_resp("resp_no_specific_user"))
            return

        yield event.plain_result(self._get_resp("resp_summary_start"))

        try:
            if group_id in self.group_timers:
                self.group_timers[group_id].cancel()
                self.group_timers.pop(group_id, None)

            await self._summarize_single_group(group_id)
            yield event.plain_result(self._get_resp("resp_summary_success"))
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 手动总结失败: {e}")
            yield event.plain_result(
                self._get_resp("resp_image_transmit_error", error=str(e))
            )

    @filter.command("stillalive清理缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        """手动重置总结进度"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result(self._get_resp("resp_group_only"))
            return

        self.last_summarized_id[group_id] = 0
        self.active_groups.add(group_id)

        if group_id in self.group_timers:
            self.group_timers[group_id].cancel()
            self.group_timers.pop(group_id, None)

        yield event.plain_result(
            self._get_resp("resp_summary_success")
        )  # 这里借用成功的提示

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听消息"""
        # 仅屏蔽本插件的指令
        if any(cmd in event.message_str for cmd in self.internal_commands):
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
            group_name = self._get_group_name(event)
            self.group_events[group_id] = event

            # 使用增强版解析逻辑保留 At 信息和回复信息
            message_content = await format_full_message(
                event, self.group_messages_map.get(group_id)
            )

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
                "platform_msg_id": event.message_obj.message_id,
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
                # 检查是否为实质性发言（包含非空白文字或 At）
                has_substance = any(
                    (isinstance(comp, Plain) and comp.text.strip())
                    or isinstance(comp, At)
                    or isinstance(comp, Reply)
                    for comp in event.message_obj.message
                )

                if has_substance:
                    self.active_groups.add(group_id)
                    if group_id in self.group_timers:
                        self.group_timers[group_id].cancel()

                    self.group_timers[group_id] = asyncio.create_task(
                        self._delay_summarize_task(group_id, 600)
                    )
        else:
            # 私聊记录逻辑
            if sender_id == specific_user_id:
                message_content = await format_full_message(event)
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

        pending = [m for m in messages if m["id"] > last_id]
        if not pending:
            self.active_groups.discard(group_id)
            return

        specific_user_id = str(self.config.get("specific_user_id", ""))

        # 寻找特定用户在本次待处理消息中的活动范围
        first_user_msg_index = -1
        last_user_msg_index = -1
        for i in range(len(pending)):
            if pending[i].get("sender_id") == specific_user_id:
                if first_user_msg_index == -1:
                    first_user_msg_index = i
                last_user_msg_index = i

        if last_user_msg_index == -1:
            self.active_groups.discard(group_id)
            return

        # 优化：向前追溯背景，避免无关消息干扰（保留用户首条发言前 15 条消息）
        context_start = max(0, first_user_msg_index - 15)
        to_summarize = pending[context_start:]

        # 限制最大长度，防止消息过多导致 LLM 偏离重点（保留最近的 100 条）
        if len(to_summarize) > 100:
            to_summarize = to_summarize[-100:]

        user_nickname = self.user_nicknames.get(group_id, "用户")
        event = self.group_events.get(group_id)
        bot_nickname = await get_bot_nickname(
            self.context, event, group_id, self.bot_nicknames
        )

        group_name = to_summarize[0].get("群名称") or self.group_names.get(
            group_id, "未知群聊"
        )
        last_time = to_summarize[-1].get("时间", "未知时间")
        dialogue_text = "\n".join([m["content"] for m in to_summarize])

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

                prompt = (
                    f"对话背景：你在本群的昵称是【{bot_nickname}】，特定用户的昵称是【{user_nickname}】。\n"
                    f"角色说明（重要）：\n"
                    f"- 消息中带有【你】前缀的是你自己（{bot_nickname}）的发言；\n"
                    f"- 带有【用户】前缀的是特定用户（{user_nickname}）的发言；\n"
                    f"- 带有【群友】前缀的是其他群成员的发言，他们不是你，也不是特定用户。\n\n"
                    f"任务目标：精炼地总结以下这段群聊记录（50字以内）。\n"
                    f"特别要求：请务必以你的人设口吻进行总结，并重点体现出特定用户【{user_nickname}】参与了哪些互动或发表了什么观点。\n"
                    f"输出格式要求（务必严格遵守）：\n"
                    f"话题：<这里填写一句话话题>\n"
                    f"内容：<这里填写总结内容>\n"
                    f"规则：1. 不要使用Markdown格式符号；2. 不要添加多余解释；3. 不要改变结构。\n\n"
                    f"--- 对话记录开始 ---\n"
                    f"{dialogue_text}\n"
                    f"--- 对话记录结束 ---"
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
        await self.api_service.send_data("/api/v1/status/sync/", data)

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

        if not event.message_obj.group_id:
            if self.private_messages and not self.private_messages[-1].get("你的回复"):
                self.private_messages[-1]["你的回复"] = reply_text
                await self._send_private_immediately()
        else:
            group_id = event.message_obj.group_id
            group_name = self._get_group_name(event)
            bot_name = await get_bot_nickname(
                self.context, event, group_id, self.bot_nicknames
            )

            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]

            msg_content_formatted = f"【你】{bot_name}: {reply_text}"
            msg_obj = {
                "id": msg_id,
                "platform_msg_id": event.message_obj.message_id,
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
            await self.api_service.send_data("/api/v1/status/sync/", data)
            self.private_messages = []

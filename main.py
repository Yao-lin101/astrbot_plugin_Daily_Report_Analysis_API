import asyncio
import inspect
import json
import os
from collections import defaultdict, deque
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.provider.entities import ProviderRequest

from .active_message import ActiveMessageHandler
from .api_service import APIService
from .command_handler import CommandHandler
from .message_utils import format_full_message, get_bot_nickname
from .storage import Storage
from .summarizer import Summarizer


@register(
    "astrbot_plugin_Daily_Report_Analysis_API",
    "e.e.",
    "联动StillAlive发送每日群聊以及与AI机器人私聊的消息汇总，并支持获取日报图片。",
    "1.3.0",
)
class DailyReportAnalysisAPI(Star):
    def __init__(self, context: Context, config: any = None):
        super().__init__(context)
        # 消息缓存: {group_id: deque([msg_obj, ...])}
        self.group_messages_map = defaultdict(lambda: deque(maxlen=100))
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

        self.private_timer = None
        self.private_task_id = 0  # 追踪私聊总结任务 ID
        self.group_task_ids = defaultdict(int)  # 追踪群聊总结任务 ID
        self.config = config

        # 初始化数据库
        db_path = os.path.join(StarTools.get_data_dir(), "storage.db")
        self.db = Storage(db_path)

        # 运行时组件
        self.api_service = None
        self.active_message_handler = None
        self.summarizer = None
        self.cmd_handler = None

        self.internal_commands = []
        self.cleanup_task = None

        # 自动同步配置到 JSON 文件，供 MCP 服务器读取
        self._sync_config_to_json()

    def _sync_config_to_json(self):
        try:
            config_dir = os.path.join(StarTools.get_data_dir(), "config")
            os.makedirs(config_dir, exist_ok=True)
            config_path = os.path.join(
                config_dir, "astrbot_plugin_Daily_Report_Analysis_API_config.json"
            )
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=4, ensure_ascii=False)
            logger.info(f"DailyReportAnalysisAPI: 已同步配置到 {config_path}")
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 同步配置失败: {e}")

    def _get_group_context(self, group_id):
        """从数据库恢复群组上下文"""
        meta = self.db.get_group_meta(group_id)
        if meta:
            self.group_names[group_id] = meta[0]
            self.last_summarized_id[group_id] = meta[1]
            self.message_id_counter[group_id] = meta[2]
            self.user_nicknames[group_id] = meta[3] or "用户"
            self.bot_nicknames[group_id] = meta[4] or "机器人"
        else:
            self.group_names[group_id] = "未知群聊"
            self.last_summarized_id[group_id] = 0
            self.message_id_counter[group_id] = 0
            self.user_nicknames[group_id] = "用户"
            self.bot_nicknames[group_id] = "机器人"

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
        display_code = self.config.get("display_code", "")
        self.api_service = APIService(target_url, character_key, display_code)

        # 初始化组件
        self.summarizer = Summarizer(self)
        self.cmd_handler = CommandHandler(self)
        self.active_message_handler = ActiveMessageHandler(self)
        self.active_message_handler.start()

        # 自动识别插件内注册的所有指令名
        self._auto_collect_internal_commands()

        self.cleanup_task = asyncio.create_task(self._cleanup_loop())
        await self._startup_backlog_check()

        logger.info(
            f"DailyReportAnalysisAPI: 插件已初始化。监控用户ID: {self.config.get('specific_user_id')}, 已自动注册屏蔽指令: {self.internal_commands}"
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
                "stillalive私聊上报",
                "stillalive状态观望",
                "stillalive强行关怀",
                "stillalive重置主动消息计数",
                "stillalive白名单添加",
                "stillalive白名单删除",
                "stillalive白名单列表",
            ]
        else:
            self.internal_commands = list(set(self.internal_commands))

    async def terminate(self):
        """插件销毁"""
        for timer in self.group_timers.values():
            timer.cancel()
        self.group_timers.clear()
        if self.private_timer:
            self.private_timer.cancel()
        if self.active_message_handler:
            self.active_message_handler.stop()
        if self.cleanup_task:
            self.cleanup_task.cancel()

    def _get_group_name(self, event: AstrMessageEvent) -> str | None:
        """获取群名称"""
        group_id = event.message_obj.group_id
        if not group_id:
            return None
        if event.message_obj.group and event.message_obj.group.group_name:
            group_name = event.message_obj.group.group_name
            self.group_names[group_id] = group_name
            return group_name
        return self.group_names.get(group_id)

    # --- 指令入口 (委托给 CommandHandler) ---

    @filter.command("stillalive日报")
    async def get_stillalive_report(self, event: AstrMessageEvent, date: str = None):
        """获取并发送指定日期的日报图片。格式: stillalive日报 [YYYY-MM-DD]"""
        async for res in self.cmd_handler.get_stillalive_report(event, date):
            yield res

    @filter.command("stillalive私聊上报")
    async def force_private_summary(self, event: AstrMessageEvent):
        """手动强制触发私聊记录的总结与上报"""
        async for res in self.cmd_handler.force_private_summary(event):
            yield res

    @filter.command("stillalive群总结")
    async def manual_group_summary(self, event: AstrMessageEvent):
        """手动触发当前群聊的总结"""
        async for res in self.cmd_handler.manual_group_summary(event):
            yield res

    @filter.command("stillalive清理缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        """手动重置总结进度"""
        async for res in self.cmd_handler.clear_cache(event):
            yield res

    @filter.command("stillalive状态观望")
    async def test_check_status(self, event: AstrMessageEvent):
        """测试指令：根据当前状态判断是否需要发消息或继续观望"""
        async for res in self.cmd_handler.test_check_status(event):
            yield res

    @filter.command("stillalive重置主动消息计数")
    async def reset_active_msg_count(self, event: AstrMessageEvent):
        """测试指令：重置今日主动发消息的计数"""
        async for res in self.cmd_handler.reset_active_msg_count(event):
            yield res

    @filter.command("stillalive强行关怀")
    async def test_force_care(
        self,
        event: AstrMessageEvent,
        message_type: str = "care",
        reason: str = "强制触发主动消息",
    ):
        """测试指令：直接生成并发送主动关怀消息"""
        async for res in self.cmd_handler.test_force_care(event, message_type, reason):
            yield res

    @filter.command("stillalive白名单添加")
    async def add_group_whitelist(
        self, event: AstrMessageEvent, group_id: str | None = None
    ):
        """添加群聊白名单"""
        async for res in self.cmd_handler.add_group_whitelist(event, group_id):
            yield res

    @filter.command("stillalive白名单删除")
    async def remove_group_whitelist(
        self, event: AstrMessageEvent, group_id: str | None = None
    ):
        """移除群聊白名单"""
        async for res in self.cmd_handler.remove_group_whitelist(event, group_id):
            yield res

    @filter.command("stillalive白名单列表")
    async def list_group_whitelist(self, event: AstrMessageEvent):
        """查看群聊白名单"""
        async for res in self.cmd_handler.list_group_whitelist(event):
            yield res

    # --- 消息监听与异步任务 ---

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        if event.get_extra("is_active_message_wake"):
            return
        sender_id = str(event.get_sender_id())
        specific_user_id = str(self.config.get("specific_user_id", ""))
        group_id = (
            str(event.message_obj.group_id)
            if event.message_obj.group_id is not None
            else None
        )

        # 1. 预解析消息内容并清洗
        message_content = await format_full_message(
            self.context,
            event,
            self.group_messages_map.get(group_id) if group_id else None,
            self.bot_nicknames,
        )

        # 2. 过滤掉空消息（如：正在输入状态、特殊插件事件等没有实质内容的“消息”）
        if not message_content or not message_content.strip():
            return

        # 3. 拦截内部指令，不记录到上下文
        if any(cmd in event.message_str for cmd in self.internal_commands):
            return

        # 当特定用户发言时（无论私聊还是群聊），重置主动消息的轮询计时器并更新活跃状态
        if specific_user_id and sender_id == specific_user_id:
            if self.active_message_handler:
                # 只有私聊才强制重置 polling（推迟主动找人）。群聊仅更新位置/时间，不推迟计时，
                # 这样如果用户在群里活跃，主动消息依然可以按原计划触发并发送到群里。
                if not group_id or getattr(event, "is_at_or_wake_command", False):
                    self.active_message_handler.reset_polling(reason="用户活跃(互动)")

                self.active_message_handler.update_user_activity(
                    group_id=group_id, unified_origin=event.unified_msg_origin
                )

        if event.message_obj.group_id and self.config.get("group_whitelist"):
            if str(event.message_obj.group_id) not in [
                str(i) for i in self.config["group_whitelist"]
            ]:
                return

        if not specific_user_id:
            return

        now = datetime.now().timestamp()
        sender_name = event.get_sender_name()

        if event.message_obj.group_id:
            group_id = str(event.message_obj.group_id)
            group_name = self._get_group_name(event)
            self.group_events[group_id] = event

            if group_id not in self.last_summarized_id:
                self._get_group_context(group_id)

            is_it_you = sender_id == specific_user_id
            # 判定是否产生了直接互动（私聊，或者群聊中 At/回复/唤醒机器人）
            is_interacted = not event.message_obj.group_id or getattr(
                event, "is_at_or_wake_command", False
            )

            # 标签固定为【用户】，但数据库标记 is_specific_user 仅在有互动时为 1
            prefix = "【用户】" if is_it_you else "【群友】"
            is_specific_user = is_it_you and is_interacted

            if is_it_you:
                self.user_nicknames[group_id] = sender_name

            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]
            msg_content_formatted = f"{prefix}{sender_name}: {message_content}"

            row_id = self.db.add_group_message(
                group_id,
                msg_id,
                sender_id,
                sender_name,
                msg_content_formatted,
                now,
                event.message_obj.message_id,
                is_specific_user=is_specific_user,
            )
            event.set_extra("report_db_row_id", row_id)
            event.set_extra("report_db_table", "group_messages")
            event.set_extra("report_prefix", f"{prefix}{sender_name}: ")
            meta_update = {"group_name": group_name, "message_id_counter": msg_id}
            if is_specific_user:
                meta_update["user_nickname"] = sender_name
            self.db.update_group_meta(group_id, **meta_update)

            self.group_messages_map[group_id].append(
                {"id": msg_id, "content": msg_content_formatted, "sender_id": sender_id}
            )

            if is_it_you:
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

                    self.group_task_ids[group_id] += 1
                    current_task_id = self.group_task_ids[group_id]
                    self.group_timers[group_id] = asyncio.create_task(
                        self._delay_summarize_task(group_id, 1800, current_task_id)
                    )
        else:
            if sender_id == specific_user_id:
                row_id = self.db.add_private_message(
                    sender_id, "user", message_content, now
                )
                event.set_extra("report_db_row_id", row_id)
                event.set_extra("report_db_table", "private_messages")
                event.set_extra("report_prefix", "")
                if self.private_timer:
                    self.private_timer.cancel()

                self.private_task_id += 1
                current_task_id = self.private_task_id
                self.private_timer = asyncio.create_task(
                    self._delay_private_summary_task(600, current_task_id)
                )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if not result or not result.is_model_result():
            return
        reply_text = result.get_plain_text()
        if not reply_text:
            return

        # 检查是否包含推理过程
        if hasattr(result, "reasoning_content") and result.reasoning_content:
            reply_text = (
                f"<reasoning>\n{result.reasoning_content}\n</reasoning>\n{reply_text}"
            )

        if not event.message_obj.group_id:
            specific_user_id = str(self.config.get("specific_user_id", ""))
            if str(event.get_sender_id()) == specific_user_id:
                self.db.add_private_message(
                    specific_user_id, "bot", reply_text, datetime.now().timestamp()
                )
                if self.private_timer:
                    self.private_timer.cancel()

                self.private_task_id += 1
                current_task_id = self.private_task_id
                self.private_timer = asyncio.create_task(
                    self._delay_private_summary_task(600, current_task_id)
                )
        else:
            group_id = str(event.message_obj.group_id)
            group_name = self._get_group_name(event)
            bot_name = await get_bot_nickname(
                self.context, event, group_id, self.bot_nicknames
            )
            if group_id not in self.message_id_counter:
                self._get_group_context(group_id)
            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]

            trigger_sender_id = str(event.get_sender_id())
            specific_user_id = str(self.config.get("specific_user_id", ""))
            is_reply_to_specific = trigger_sender_id == specific_user_id

            prefix = "【你】" if is_reply_to_specific else "【你(群回复)】"
            msg_content_formatted = f"{prefix}{bot_name}: {reply_text}"

            self.db.add_group_message(
                group_id,
                msg_id,
                "bot",
                bot_name,
                msg_content_formatted,
                datetime.now().timestamp(),
                event.message_obj.message_id,
                is_specific_user=is_reply_to_specific,
            )
            self.db.update_group_meta(
                group_id,
                group_name=group_name,
                message_id_counter=msg_id,
                bot_nickname=bot_name,
            )
            self.group_messages_map[group_id].append(
                {"id": msg_id, "content": msg_content_formatted, "sender_id": "bot"}
            )

        # 统一写入对话历史（因为设置了 request.conversation = None 使得核心无法自动保存）
        if event.get_extra("is_active_message_wake"):
            try:
                curr_cid = (
                    await self.context.conversation_manager.get_curr_conversation_id(
                        event.unified_msg_origin
                    )
                )
                if curr_cid:
                    conv = await self.context.conversation_manager.get_conversation(
                        event.unified_msg_origin, curr_cid
                    )
                    if conv:
                        history = json.loads(conv.history)
                        history.append({"role": "assistant", "content": reply_text})
                        await self.context.conversation_manager.update_conversation(
                            event.unified_msg_origin, curr_cid, history=history
                        )
                        logger.info(
                            f"DailyReportAnalysisAPI: 已将主动消息写入对话历史 (Session: {event.unified_msg_origin}, CID: {curr_cid})。"
                        )
            except Exception as e:
                logger.error(f"DailyReportAnalysisAPI: 写入对话历史失败: {e}")

    async def _delay_summarize_task(self, group_id, delay, task_id):
        try:
            await asyncio.sleep(delay)
            if self.group_task_ids.get(group_id) != task_id:
                return
            await self.summarizer.summarize_single_group(group_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 延迟总结任务出错: {e}")
        finally:
            if self.group_task_ids.get(group_id) == task_id:
                self.group_timers.pop(group_id, None)

    async def _delay_private_summary_task(self, delay: int, task_id: int):
        try:
            await asyncio.sleep(delay)
            if self.private_task_id != task_id:
                return
            await self.summarizer.summarize_private_messages()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 私聊延迟总结任务出错: {e}")
        finally:
            if self.private_task_id == task_id:
                self.private_timer = None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, request: ProviderRequest):
        """
        当 LLM 请求发起时，更新数据库中的消息内容。
        此时消息已经经过了图片转述、引用回复处理等，内容更加丰富。
        """
        if event.get_extra("is_active_message_wake"):
            request.conversation = None
            return

        # 检测是否为特定用户的 LLM 交互，如果是，则更新活动状态并重置主动消息的轮询
        sender_id = str(event.get_sender_id())
        specific_user_id = str(self.config.get("specific_user_id", ""))
        group_id = (
            str(event.message_obj.group_id)
            if event.message_obj.group_id is not None
            else None
        )

        if specific_user_id and sender_id == specific_user_id:
            if self.active_message_handler:
                self.active_message_handler.reset_polling(reason="用户活跃(LLM互动)")
                self.active_message_handler.update_user_activity(
                    group_id=group_id, unified_origin=event.unified_msg_origin
                )
        row_id = event.get_extra("report_db_row_id")
        table_name = event.get_extra("report_db_table")
        if not row_id or not table_name:
            return

        # 只有包含图片或文件时才更新，避免普通文本消息重复处理（虽然处理了也没事）
        has_media = any(isinstance(comp, Image) for comp in event.message_obj.message)
        if not has_media:
            return

        full_parts = []
        if request.prompt and request.prompt != "<attachment>":
            full_parts.append(request.prompt)

        for part in request.extra_user_content_parts:
            text = ""
            if isinstance(part, dict):
                text = part.get("text", "")
            elif hasattr(part, "text"):
                text = part.text

            if text:
                # 剔除 system_reminder 部分，避免重复注入
                if "<system_reminder>" in text:
                    continue
                full_parts.append(text)

        if not full_parts:
            return

        rich_content = "\n".join(full_parts)
        prefix = event.get_extra("report_prefix") or ""
        self.db.update_message_content(table_name, row_id, f"{prefix}{rich_content}")
        logger.debug(f"DailyReportAnalysisAPI: 已更新消息 ID {row_id} 的富文本内容。")

    async def _cleanup_loop(self):
        """Periodically clean up database messages older than 7 days.

        Runs in a background loop every 24 hours.
        """
        try:
            while True:
                logger.info("DailyReportAnalysisAPI: Running database cleanup task...")
                try:
                    self.db.clean_old_messages(days=7)
                except Exception as e:
                    logger.error(f"DailyReportAnalysisAPI: Failed to clean old messages: {e}")
                await asyncio.sleep(86400)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: Cleanup loop encountered an error: {e}")

    async def _startup_backlog_check(self):
        """Check for and handle message backlogs on startup.

        This method advances the database markers past historical messages from
        previous days to avoid processing them, and schedules immediate summary
        tasks for any unsummarized messages sent today.
        """
        try:
            specific_user_id = str(self.config.get("specific_user_id", ""))
            if not specific_user_id:
                return

            # Today's 0:00 local time timestamp
            today_0 = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

            # 1. Process group backlogs
            groups = self.db.get_all_groups()
            for group_id in groups:
                # Advance progress to today's 0:00 (skipping older messages)
                self.db.advance_group_backlog_to_today(group_id, today_0)

                # Initialize local context cache
                if group_id not in self.last_summarized_id:
                    self._get_group_context(group_id)

                # Check if there are any remaining pending messages from today
                last_id = self.last_summarized_id.get(group_id, 0)
                pending = self.db.get_pending_messages(group_id, last_id, limit=1)
                if pending:
                    # Check if the specific user spoke today
                    all_pending = self.db.get_pending_messages(group_id, last_id, limit=200)
                    has_specific_user = any(
                        str(m.get("sender_id")) == specific_user_id for m in all_pending
                    )
                    if has_specific_user:
                        self.group_task_ids[group_id] += 1
                        current_task_id = self.group_task_ids[group_id]
                        self.group_timers[group_id] = asyncio.create_task(
                            self._delay_summarize_task(group_id, 5, current_task_id)
                        )
                        logger.info(
                            f"DailyReportAnalysisAPI: Found today's backlog for group {group_id}. Scheduled summary in 5s."
                        )

            # 2. Process private message backlog
            self.db.advance_private_backlog_to_today(specific_user_id, today_0)
            last_private_id_str = self.db.get_plugin_meta("last_private_summarized_id", "0")
            last_private_id = int(last_private_id_str)
            pending_private = self.db.get_pending_private_messages(
                specific_user_id, last_private_id, limit=1
            )
            if pending_private:
                self.private_task_id += 1
                current_task_id = self.private_task_id
                self.private_timer = asyncio.create_task(
                    self._delay_private_summary_task(5, current_task_id)
                )
                logger.info(
                    f"DailyReportAnalysisAPI: Found today's private message backlog. Scheduled summary in 5s."
                )

        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: Startup backlog check failed: {e}")

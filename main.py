import asyncio
import inspect
import json
import os
from collections import defaultdict, deque
from datetime import datetime

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register

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
        self.api_service = APIService(target_url, character_key)

        # 初始化组件
        self.summarizer = Summarizer(self)
        self.cmd_handler = CommandHandler(self)
        self.active_message_handler = ActiveMessageHandler(self)
        self.active_message_handler.start()

        # 自动识别插件内注册的所有指令名
        self._auto_collect_internal_commands()

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
        async for res in self.cmd_handler.get_stillalive_report(event, date):
            yield res

    @filter.command("stillalive私聊上报")
    async def force_private_summary(self, event: AstrMessageEvent):
        async for res in self.cmd_handler.force_private_summary(event):
            yield res

    @filter.command("stillalive群总结")
    async def manual_group_summary(self, event: AstrMessageEvent):
        async for res in self.cmd_handler.manual_group_summary(event):
            yield res

    @filter.command("stillalive清理缓存")
    async def clear_cache(self, event: AstrMessageEvent):
        async for res in self.cmd_handler.clear_cache(event):
            yield res

    @filter.command("stillalive状态观望")
    async def test_check_status(self, event: AstrMessageEvent):
        async for res in self.cmd_handler.test_check_status(event):
            yield res

    @filter.command("stillalive重置主动消息计数")
    async def reset_active_msg_count(self, event: AstrMessageEvent):
        async for res in self.cmd_handler.reset_active_msg_count(event):
            yield res

    @filter.command("stillalive强行关怀")
    async def test_force_care(
        self,
        event: AstrMessageEvent,
        message_type: str = "care",
        reason: str = "强制触发主动消息",
    ):
        async for res in self.cmd_handler.test_force_care(event, message_type, reason):
            yield res

    @filter.command("stillalive白名单添加")
    async def add_group_whitelist(
        self, event: AstrMessageEvent, group_id: str | None = None
    ):
        async for res in self.cmd_handler.add_group_whitelist(event, group_id):
            yield res

    @filter.command("stillalive白名单删除")
    async def remove_group_whitelist(
        self, event: AstrMessageEvent, group_id: str | None = None
    ):
        async for res in self.cmd_handler.remove_group_whitelist(event, group_id):
            yield res

    @filter.command("stillalive白名单列表")
    async def list_group_whitelist(self, event: AstrMessageEvent):
        async for res in self.cmd_handler.list_group_whitelist(event):
            yield res

    # --- 消息监听与异步任务 ---

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        sender_id = str(event.get_sender_id())
        specific_user_id = str(self.config.get("specific_user_id", ""))

        # 仅当用户发起私聊互动时，才重置主动消息的轮询计时器
        if (
            specific_user_id
            and sender_id == specific_user_id
            and not event.message_obj.group_id
        ):
            if self.active_message_handler:
                self.active_message_handler.reset_polling(reason="用户互动")
                self.active_message_handler.user_unified_origin = (
                    event.unified_msg_origin
                )

        if any(cmd in event.message_str for cmd in self.internal_commands):
            return

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
            group_id = event.message_obj.group_id
            group_name = self._get_group_name(event)
            self.group_events[group_id] = event

            if group_id not in self.last_summarized_id:
                self._get_group_context(group_id)

            message_content = await format_full_message(
                self.context,
                event,
                self.group_messages_map.get(group_id),
                self.bot_nicknames,
            )
            is_specific_user = sender_id == specific_user_id
            prefix = "【用户】" if is_specific_user else "【群友】"
            if is_specific_user:
                self.user_nicknames[group_id] = sender_name

            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]
            msg_content_formatted = f"{prefix}{sender_name}: {message_content}"

            self.db.add_group_message(
                group_id,
                msg_id,
                sender_id,
                sender_name,
                msg_content_formatted,
                now,
                event.message_obj.message_id,
            )
            meta_update = {"group_name": group_name, "message_id_counter": msg_id}
            if is_specific_user:
                meta_update["user_nickname"] = sender_name
            self.db.update_group_meta(group_id, **meta_update)

            self.group_messages_map[group_id].append(
                {
                    "id": msg_id,
                    "content": msg_content_formatted,
                    "sender_id": sender_id,
                    "platform_msg_id": event.message_obj.message_id,
                }
            )

            if is_specific_user:
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
                        self._delay_summarize_task(group_id, 1800)
                    )
        else:
            if sender_id == specific_user_id:
                message_content = await format_full_message(
                    self.context, event, bot_nicknames=self.bot_nicknames
                )
                self.db.add_private_message(sender_id, "user", message_content, now)
                if self.private_timer:
                    self.private_timer.cancel()
                self.private_timer = asyncio.create_task(
                    self._delay_private_summary_task(600)
                )

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        result = event.get_result()
        if not result or not result.is_model_result():
            return
        group_id = event.message_obj.group_id
        logger.info(
            f"DailyReportAnalysisAPI: 正在处理 bot 回复. group_id: {group_id}, result.chain: {result.chain}"
        )
        reply_text = await format_full_message(
            self.context,
            event,
            self.group_messages_map.get(group_id),
            self.bot_nicknames,
            message_chain=result.chain,
        )
        if not reply_text:
            return

        if not event.message_obj.group_id:
            specific_user_id = str(self.config.get("specific_user_id", ""))
            if str(event.get_sender_id()) == specific_user_id:
                self.db.add_private_message(
                    specific_user_id, "bot", reply_text, datetime.now().timestamp()
                )
                if self.private_timer:
                    self.private_timer.cancel()
                self.private_timer = asyncio.create_task(
                    self._delay_private_summary_task(600)
                )
        else:
            group_id = event.message_obj.group_id
            group_name = self._get_group_name(event)
            bot_name = await get_bot_nickname(
                self.context, event, group_id, self.bot_nicknames
            )
            if group_id not in self.message_id_counter:
                self._get_group_context(group_id)
            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]
            msg_content_formatted = f"【你】{bot_name}: {reply_text}"
            self.db.add_group_message(
                group_id,
                msg_id,
                "bot",
                bot_name,
                msg_content_formatted,
                datetime.now().timestamp(),
                event.message_obj.message_id,
            )
            self.db.update_group_meta(
                group_id,
                group_name=group_name,
                message_id_counter=msg_id,
                bot_nickname=bot_name,
            )
            self.group_messages_map[group_id].append(
                {
                    "id": msg_id,
                    "content": msg_content_formatted,
                    "sender_id": "bot",
                    "platform_msg_id": event.message_obj.message_id,
                }
            )

    async def _delay_summarize_task(self, group_id, delay):
        try:
            await asyncio.sleep(delay)
            await self.summarizer.summarize_single_group(group_id)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 延迟总结任务出错: {e}")
        finally:
            self.group_timers.pop(group_id, None)

    async def _delay_private_summary_task(self, delay: int):
        try:
            await asyncio.sleep(delay)
            await self.summarizer.summarize_private_messages()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 私聊延迟总结任务出错: {e}")
        finally:
            self.private_timer = None

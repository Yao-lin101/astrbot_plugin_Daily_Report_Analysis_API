import asyncio
import inspect
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timezone, timedelta
from .storage import Storage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import At, Image, Node, Nodes, Plain, Reply
from astrbot.api.star import Context, Star, StarTools, register

from .active_message import ActiveMessageHandler
from .api_service import APIService
from .message_utils import format_full_message, get_bot_nickname
from .report_handler import ReportHandler

SUMMARY_PROMPT_TEMPLATE = """对话背景：你在本群的昵称是【{bot_nickname}】，特定用户的昵称是【{user_nickname}】。
角色说明（重要）：
- 消息中带有【你】前缀的是你自己的发言；
- 带有【用户】前缀的是特定用户的发言；
- 带有【群友】前缀的是其他群成员的发言。

任务目标：根据你的AI人设，判断以下群聊记录是否包含值得记录的观察、动态或话题，并以严格的 JSON 格式输出结果。
特别要求：只有在你觉得符合人设观察价值时才进行总结。请忽略无意义的水群（如单发表情包、哈哈哈、收到等）。

输出 JSON 格式要求（必须只返回合法的 JSON 对象，不带 Markdown 符号等包裹）：
{{
  "status": "COMPLETED", // 状态枚举：COMPLETED(有价值且话题已结束)、ONGOING(有价值但话题还在继续)、IGNORED(符合人设判断的毫无价值的水群)
  "topics": [
    {{
      "topic": "这里填写一句话话题名称",
      "content": "这里填写总结内容（50字内，以你的人设口吻讲述特定用户的动态）"
    }}
  ],
  "next_start_id": 1050 // 整数。必须提供！你认为下一次总结应该从哪一条消息ID开始截断（通常是当前话题结束后的新话题起点ID，或最后一条消息的ID）
}}

--- 对话记录开始 ---
{dialogue_text}
--- 对话记录结束 ---"""

PRIVATE_SUMMARY_PROMPT_TEMPLATE = """任务目标：以下是你与特定用户的多轮私聊记录，请根据你的AI人设，判断这段对话的核心内容，并提炼成“话题”与“总结”，以便记录到你的日志中。
特别要求：
1. 提取这段多轮对话最核心的意图、探讨的问题或最终结论。
2. 保持你的人设口吻进行描述。

输出 JSON 格式要求（必须只返回合法的 JSON 对象，不带 Markdown 符号等包裹）：
{{
  "topic": "这里填写一句话话题名称",
  "content": "这里填写这段私聊的总结内容（100字内）"
}}

--- 私聊记录开始 ---
{dialogue_text}
--- 私聊记录结束 ---"""


@register(
    "astrbot_plugin_Daily_Report_Analysis_API",
    "e.e.",
    "联动StillAlive发送每日群聊以及与AI机器人私聊的消息汇总，并支持获取日报图片。",
    "1.3.0",
)
class DailyReportAnalysisAPI(Star):
    def __init__(self, context: Context, config: any = None):
        super().__init__(context)
        # 消息缓存: {group_id: deque([msg_obj, ...])}，每个群保留最近 500 条
        self.group_messages_map = defaultdict(lambda: deque(maxlen=500))
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
        
        # 运行时缓存
        self.private_messages = []
        self.internal_commands = []
        self.api_service = None
        self.active_message_handler = None
        
        # 自动同步配置到 JSON 文件，供 MCP 服务器读取
        try:
            config_dir = os.path.join(StarTools.get_data_dir(), "config")
            os.makedirs(config_dir, exist_ok=True)
            config_path = os.path.join(config_dir, "astrbot_plugin_Daily_Report_Analysis_API_config.json")
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
        else:
            self.group_names[group_id] = "未知群聊"
            self.last_summarized_id[group_id] = 0
            self.message_id_counter[group_id] = 0

    def _save_data(self):
        """此方法已弃用，数据已实时保存至 SQLite"""
        pass

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

        if self.private_messages:
            self.private_timer = asyncio.create_task(
                self._delay_private_summary_task(10)
            )

        # 初始化 API 服务
        target_url = self.config.get("target_url", "")
        character_key = self.config.get("character_key", "")
        self.api_service = APIService(target_url, character_key)

        self.active_message_handler = ActiveMessageHandler(self)
        self.active_message_handler.start()

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
        self._save_data()

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

    @filter.command("stillalive私聊上报")
    async def force_private_summary(self, event: AstrMessageEvent):
        """手动强制触发私聊记录的总结与上报"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not self.private_messages:
            yield event.plain_result("当前没有待上报的私聊记录。")
            return

        yield event.plain_result("开始强制总结并上报私聊记录...")

        try:
            if self.private_timer:
                self.private_timer.cancel()
                self.private_timer = None

            await self._summarize_private_messages()
            yield event.plain_result(self._get_resp("resp_summary_success"))
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 手动私聊上报失败: {e}")
            yield event.plain_result(f"私聊上报失败: {str(e)}")

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

    @filter.command("stillalive状态观望")
    async def test_check_status(self, event: AstrMessageEvent):
        """测试指令：根据当前状态判断是否需要发消息或继续观望"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not self.active_message_handler:
            yield event.plain_result("主动消息机制未初始化。")
            return

        yield event.plain_result("正在进行状态观望与评估...")
        await self.active_message_handler._check_and_action()
        check_time = self.active_message_handler.next_check_time
        yield event.plain_result(
            f"观望评估完成。目前的 next_check_time 状态为: {check_time}"
        )

    @filter.command("stillalive重置主动消息计数")
    async def reset_active_msg_count(self, event: AstrMessageEvent):
        """测试指令：重置今日主动发消息的计数"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not self.active_message_handler:
            yield event.plain_result("主动消息机制未初始化。")
            return
            
        self.active_message_handler.messages_sent_today = 0
        self.active_message_handler.last_reset_date = datetime.now().date()
        yield event.plain_result("今日主动消息发送计数已重置为 0。")

    @filter.command("stillalive强行关怀")
    async def test_force_care(
        self,
        event: AstrMessageEvent,
        message_type: str = "care",
        reason: str = "强制触发主动消息，随便说点什么吧。",
    ):
        """测试指令：直接生成并发送主动关怀消息"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not self.active_message_handler:
            yield event.plain_result("主动消息机制未初始化。")
            return

        yield event.plain_result(f"正在强行生成并发送消息（类型：{message_type}，动机：{reason}）...")
        await self.active_message_handler._generate_and_send_message(reason, message_type, short_data="[这是强行关怀的默认短时记忆，由于直接跳过了第一步，此处短时记忆为空]")
        yield event.plain_result("执行结束。如果成功，指定用户应该已经收到了主动私聊。")

    @filter.command("stillalive白名单添加")
    async def add_group_whitelist(
        self, event: AstrMessageEvent, group_id: str | None = None
    ):
        """添加群聊白名单"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return
        target_id = group_id or event.message_obj.group_id
        if not target_id:
            yield event.plain_result("请在群聊中使用或指定群号")
            return
        target_id = str(target_id)
        if "group_whitelist" not in self.config:
            self.config["group_whitelist"] = []
        if target_id in self.config["group_whitelist"]:
            yield event.plain_result(f"群号 {target_id} 已在白名单中")
            return
        self.config["group_whitelist"].append(target_id)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result(f"已添加群号 {target_id} 到白名单")

    @filter.command("stillalive白名单删除")
    async def remove_group_whitelist(
        self, event: AstrMessageEvent, group_id: str | None = None
    ):
        """移除群聊白名单"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return
        target_id = group_id or event.message_obj.group_id
        if not target_id:
            yield event.plain_result("请在群聊中使用或指定群号")
            return
        target_id = str(target_id)
        if (
            "group_whitelist" not in self.config
            or target_id not in self.config["group_whitelist"]
        ):
            yield event.plain_result(f"群号 {target_id} 不在白名单中")
            return
        self.config["group_whitelist"].remove(target_id)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result(f"已从白名单移除群号 {target_id}")

    @filter.command("stillalive白名单列表")
    async def list_group_whitelist(self, event: AstrMessageEvent):
        """查看群聊白名单"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return
        whitelist = self.config.get("group_whitelist", [])
        yield event.plain_result(
            f"当前群聊白名单: {whitelist if whitelist else '全部群聊'}"
        )

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_all_message(self, event: AstrMessageEvent):
        """监听消息"""
        sender_id = str(event.get_sender_id())
        specific_user_id = str(self.config.get("specific_user_id", ""))
        
        # 只要收到特定用户的消息（无论是指令还是闲聊），就重置主动消息轮询并记录来源
        if specific_user_id and sender_id == specific_user_id:
            if self.active_message_handler:
                self.active_message_handler.reset_polling(min_int=60, max_int=120, reason="用户互动")
                self.active_message_handler.user_unified_origin = event.unified_msg_origin

        # 仅屏蔽本插件的指令
        if any(cmd in event.message_str for cmd in self.internal_commands):
            return

        # 群聊白名单检查
        if event.message_obj.group_id and self.config.get("group_whitelist"):
            if str(event.message_obj.group_id) not in [
                str(i) for i in self.config["group_whitelist"]
            ]:
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
            
            # 确保计数器和上下文已加载
            if group_id not in self.message_id_counter:
                self._get_group_context(group_id)

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
            
            # 写入数据库
            self.db.add_group_message(
                group_id, msg_id, sender_id, sender_name, 
                msg_content_formatted, now, event.message_obj.message_id
            )
            self.db.update_group_meta(group_id, group_name=group_name, message_id_counter=msg_id)
            
            # 维护一小段内存缓存用于 format_full_message 解析
            msg_obj = {"id": msg_id, "content": msg_content_formatted, "sender_id": sender_id}
            if group_id not in self.group_messages_map:
                self.group_messages_map[group_id] = deque(maxlen=100)
            self.group_messages_map[group_id].append(msg_obj)

            logger.debug(
                f"DailyReportAnalysisAPI: 记录群聊消息 ID={msg_id} [{group_name}] - {msg_content_formatted}"
            )

            if is_specific_user:
                # 检查是否为实质性发言
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
            # 私聊记录逻辑
            if sender_id == specific_user_id:
                message_content = await format_full_message(event)
                self.db.add_private_message(sender_id, message_content, now)
                
                if self.private_timer:
                    self.private_timer.cancel()
                self.private_timer = asyncio.create_task(
                    self._delay_private_summary_task(600)
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
        if group_id not in self.last_summarized_id:
            self._get_group_context(group_id)

        last_id = self.last_summarized_id.get(group_id, 0)
        pending = self.db.get_pending_messages(group_id, last_id, limit=200)

        if not pending:
            self.active_groups.discard(group_id)
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
            self.active_groups.discard(group_id)
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

        user_nickname = self.user_nicknames.get(group_id, "用户")
        event = self.group_events.get(group_id)
        bot_nickname = await get_bot_nickname(
            self.context, event, group_id, self.bot_nicknames
        )

        group_name = self.group_names.get(group_id, "未知群聊")
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
                    import json

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
                    except:
                        next_start_id = max_valid_id
                
                if next_start_id > max_valid_id:
                    logger.warning(f"DailyReportAnalysisAPI: LLM 返回的 ID {next_start_id} 超过上限 {max_valid_id}，已修正。")
                    next_start_id = max_valid_id
                elif next_start_id < min_valid_id:
                    logger.warning(f"DailyReportAnalysisAPI: LLM 返回的 ID {next_start_id} 低于下限 {min_valid_id}，已修正。")
                    next_start_id = max_valid_id # 默认移动到末尾，防止死循环
                # ------------------------------------------

                logger.info(
                    f"DailyReportAnalysisAPI: LLM判定状态={status}, topics_count={len(topics)}, next_start_id={next_start_id}"
                )

                if status == "COMPLETED" and topics:
                    # 只有在总结成功且有话题时，才更新进度
                    self.last_summarized_id[group_id] = next_start_id
                    self.db.update_group_meta(group_id, last_summarized_id=next_start_id)
                    
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

                # 更新截断点为下一个周期的开始
                self.last_summarized_id[group_id] = next_start_id - 1

                if status == "ONGOING":
                    # 主动重试（重新发起30分钟计时）
                    if group_id in self.group_timers:
                        self.group_timers[group_id].cancel()
                    self.group_timers[group_id] = asyncio.create_task(
                        self._delay_summarize_task(group_id, 1800)
                    )

            except Exception as e:
                logger.error(f"DailyReportAnalysisAPI: 总结处理失败: {e}")
                # 出现异常时也安全推进，防止卡死
                self.last_summarized_id[group_id] = to_summarize[-1]["id"]
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
            self.last_summarized_id[group_id] = to_summarize[-1]["id"]

        self.active_groups.discard(group_id)
        self._save_data()

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

            # 确保计数器已加载
            if group_id not in self.message_id_counter:
                self._get_group_context(group_id)

            self.message_id_counter[group_id] += 1
            msg_id = self.message_id_counter[group_id]

            msg_content_formatted = f"【你】{bot_name}: {reply_text}"
            
            # 写入数据库
            self.db.add_group_message(
                group_id, msg_id, "bot", bot_name, 
                msg_content_formatted, datetime.now().timestamp(), event.message_obj.message_id
            )
            self.db.update_group_meta(group_id, group_name=group_name, message_id_counter=msg_id)

            # 维护一小段内存缓存用于 format_full_message 解析
            msg_obj = {"id": msg_id, "content": msg_content_formatted, "sender_id": "bot"}
            if group_id not in self.group_messages_map:
                self.group_messages_map[group_id] = deque(maxlen=100)
            self.group_messages_map[group_id].append(msg_obj)

            logger.debug(
                f"DailyReportAnalysisAPI: 记录机器人回复 ID={msg_id} [{group_name}] - {msg_content_formatted}"
            )

    async def _delay_private_summary_task(self, delay: int):
        try:
            await asyncio.sleep(delay)
            await self._summarize_private_messages()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 私聊延迟总结任务出错: {e}")
        finally:
            self.private_timer = None

    async def _summarize_private_messages(self):
        specific_user_id = str(self.config.get("specific_user_id", ""))
        if not specific_user_id:
            return

        # 从数据库获取最近的私聊记录
        to_summarize = self.db.get_recent_private_messages(specific_user_id, limit=30)
        if not to_summarize:
            return
            
        # 查找哪些消息还未标记“你的回复”
        unreplied_msgs = [m for m in to_summarize if not m.get("你的回复")]
        if not unreplied_msgs:
            return

        first_time = to_summarize[0].get("时间", datetime.now().strftime("%H:%M"))

        dialogue_text = ""
        for idx, m in enumerate(to_summarize):
            dialogue_text += f"[回合{idx + 1}]\n用户：{m.get('用户', '')}\n你：{m.get('你的回复', '')}\n"

        provider_id = self.config.get("summary_provider_id")

        summary_topic = ""
        summary_content = ""

        if len(to_summarize) == 1:
            # 只有一轮对话，绝对不丢失，直接上报原格式
            payload_dict = {
                "时间": first_time,
                "用户": to_summarize[0].get("用户", ""),
                "你的回复": to_summarize[0].get("你的回复", ""),
            }
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
                    if raw_result.startswith("```json"):
                        raw_result = raw_result[7:]
                    elif raw_result.startswith("```"):
                        raw_result = raw_result[3:]
                    if raw_result.endswith("```"):
                        raw_result = raw_result[:-3]
                    raw_result = raw_result.strip()

                    try:
                        import json

                        result_json = json.loads(raw_result)
                        summary_topic = result_json.get("topic", "")
                        summary_content = result_json.get("content", "")
                    except json.JSONDecodeError:
                        logger.error(
                            f"DailyReportAnalysisAPI: 私聊 LLM 返回的不是合法 JSON: {raw_result}"
                        )
                except Exception as e:
                    logger.error(f"DailyReportAnalysisAPI: 私聊总结处理失败: {e}")

            # 多轮对话格式组装
            if not summary_topic and not summary_content:
                # 兜底机制：如果 LLM 失败或返回空，暴力拼接，绝对不丢弃任何多轮私聊
                summary_topic = "多轮私聊记录"
                summary_content = " / ".join(
                    [m.get("用户", "") for m in to_summarize if m.get("用户")]
                )
                if len(summary_content) > 100:
                    summary_content = summary_content[:100] + "..."

            payload_dict = {
                "时间": first_time,
                "话题": summary_topic,
                "总结": summary_content,
            }

        # 上报以首条消息时间为准
        data = {
            "type": "qq_messages",
            "data": {"private_messages": [payload_dict]},
        }
        await self.api_service.send_data("/api/v1/status/sync/", data)

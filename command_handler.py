from datetime import datetime
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import Image, Node, Nodes, Plain

from .report_handler import ReportHandler


class CommandHandler:
    def __init__(self, plugin):
        self.plugin = plugin
        self.context = plugin.context
        self.config = plugin.config
        self.db = plugin.db
        self.api_service = plugin.api_service

    def _check_permission(self, event: AstrMessageEvent) -> bool:
        """检查发送者是否为特定用户"""
        sender_id = str(event.get_sender_id())
        specific_user_id = str(self.config.get("specific_user_id", ""))
        return sender_id == specific_user_id

    def _get_resp(self, key: str, default: str = "", **kwargs) -> str:
        """从配置获取回复模板并格式化"""
        tmpl = self.config.get(key, default)
        try:
            return tmpl.format(**kwargs)
        except Exception:
            return tmpl

    async def get_stillalive_report(self, event: AstrMessageEvent, date: str = None):
        """获取并发送指定日期的日报图片。格式: stillalive日报 [YYYY-MM-DD]"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not date:
            date = datetime.now().strftime("%Y-%m-%d")

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

        error_msg = report_data.get("error") or report_data.get("detail")
        if error_msg:
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
            p = Path(image_path)
            if p.exists():
                try:
                    chain = event.chain_result([Image.fromFileSystem(str(p))])
                    await event.send(chain)
                except Exception as e:
                    logger.error(f"DailyReport: 图片发送失败，尝试发送转发文本: {e}")
                    report_text = (
                        report_data.get("markdown")
                        or report_data.get("report_md")
                        or report_data.get("report_markdown")
                    )

                    if not report_text and "report_html" in report_data:
                        import re

                        html = report_data["report_html"]
                        html = re.sub(r"<(br|p|div|li|h[1-6])[^>]*>", "\n", html)
                        html = re.sub(r"</(p|div|li|h[1-6])>", "\n", html)
                        report_text = re.sub(r"<[^>]+>", "", html)
                        report_text = report_text.replace("&nbsp;", " ").strip()
                        report_text = re.sub(r"\n\s*\n", "\n\n", report_text)

                    if report_text:
                        bot_id = str(
                            getattr(event, "robot_id", getattr(event, "self_id", "0"))
                        )
                        try:
                            logger.info(
                                f"DailyReport: 正在尝试发送合并转发消息 (内容长度: {len(report_text)})"
                            )
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
                                await event.send(event.plain_result(report_text))
                                return
                            except Exception as e_plain:
                                logger.error(
                                    f"DailyReport: 纯文本回退发送也失败了: {e_plain}"
                                )

                    yield event.plain_result(
                        self._get_resp(
                            "resp_image_transmit_error",
                            error="图片发送失败，且文本回退发送均未成功。",
                        )
                    )
            else:
                yield event.plain_result(self._get_resp("resp_image_file_not_found"))
        else:
            yield event.plain_result(self._get_resp("resp_render_error"))

    async def force_private_summary(self, event: AstrMessageEvent):
        """手动强制触发私聊记录的总结与上报"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        yield event.plain_result("正在检查并尝试上报私聊记录...")

        try:
            if self.plugin.private_timer:
                self.plugin.private_timer.cancel()
                self.plugin.private_timer = None

            success = await self.plugin.summarizer.summarize_private_messages()
            if success:
                yield event.plain_result("私聊总结与上报完成！")
            else:
                yield event.plain_result("当前没有待上报的新私聊记录。")
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 手动私聊上报失败: {e}")
            yield event.plain_result(f"私聊上报失败: {str(e)}")

    async def manual_group_summary(self, event: AstrMessageEvent):
        """手动触发当前群聊的总结"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result(self._get_resp("resp_group_only"))
            return

        # 优化：直接从数据库获取待总结的消息，不再仅依赖内存缓存
        if group_id not in self.plugin.last_summarized_id:
            self.plugin._get_group_context(group_id)

        last_id = self.plugin.last_summarized_id.get(group_id, 0)
        pending_messages = self.db.get_pending_messages(group_id, last_id, limit=200)

        specific_user_id = str(self.config.get("specific_user_id", ""))
        has_specific_user = any(
            str(m.get("sender_id")) == specific_user_id for m in pending_messages
        )

        if not has_specific_user:
            yield event.plain_result(self._get_resp("resp_no_specific_user"))
            return

        yield event.plain_result(self._get_resp("resp_summary_start"))

        try:
            if group_id in self.plugin.group_timers:
                self.plugin.group_timers[group_id].cancel()
                self.plugin.group_timers.pop(group_id, None)

            await self.plugin.summarizer.summarize_single_group(group_id)
            yield event.plain_result(self._get_resp("resp_summary_success"))
        except Exception as e:
            logger.error(f"DailyReportAnalysisAPI: 手动总结失败: {e}")
            yield event.plain_result(
                self._get_resp("resp_image_transmit_error", error=str(e))
            )

    async def clear_cache(self, event: AstrMessageEvent):
        """手动重置总结进度"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        group_id = event.message_obj.group_id
        if not group_id:
            yield event.plain_result(self._get_resp("resp_group_only"))
            return

        # 重置内存和数据库中的总结进度
        self.plugin.last_summarized_id[group_id] = 0
        self.db.update_group_meta(group_id, last_summarized_id=0)

        self.plugin.active_groups.add(group_id)

        if group_id in self.plugin.group_timers:
            self.plugin.group_timers[group_id].cancel()
            self.plugin.group_timers.pop(group_id, None)

        yield event.plain_result(self._get_resp("resp_summary_success"))

    async def test_check_status(self, event: AstrMessageEvent):
        """测试指令：根据当前状态判断是否需要发消息或继续观望"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not self.plugin.active_message_handler:
            yield event.plain_result("主动消息机制未初始化。")
            return

        yield event.plain_result("正在进行状态观望与评估...")
        await self.plugin.active_message_handler._check_and_action()
        check_time = self.plugin.active_message_handler.next_check_time
        yield event.plain_result(
            f"观望评估完成。目前的 next_check_time 状态为: {check_time}"
        )

    async def reset_active_msg_count(self, event: AstrMessageEvent):
        """测试指令：重置今日主动发消息的计数"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return

        if not self.plugin.active_message_handler:
            yield event.plain_result("主动消息机制未初始化。")
            return

        # 同步重置内存与数据库中的计数
        handler = self.plugin.active_message_handler
        handler.messages_sent_today = 0
        handler.last_reset_date = datetime.now().date()

        self.db.update_plugin_meta("active_msg_sent_today", 0)
        self.db.update_plugin_meta(
            "active_msg_last_reset_date", handler.last_reset_date.isoformat()
        )

        yield event.plain_result("今日主动消息发送计数已重置为 0（已同步至数据库）。")

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

        if not self.plugin.active_message_handler:
            yield event.plain_result("主动消息机制未初始化。")
            return

        yield event.plain_result(
            f"正在强行生成并发送消息（类型：{message_type}，动机：{reason}）..."
        )
        await self.plugin.active_message_handler._generate_and_send_message(
            reason,
            message_type,
            short_data="[这是强行关怀的默认短时记忆，由于直接跳过了第一步，此处短时记忆为空]",
        )
        yield event.plain_result("执行结束。如果成功，指定用户应该已经收到了主动私聊。")

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

    async def list_group_whitelist(self, event: AstrMessageEvent):
        """查看群聊白名单"""
        if not self._check_permission(event):
            yield event.plain_result(self._get_resp("resp_permission_denied"))
            return
        whitelist = self.config.get("group_whitelist", [])
        yield event.plain_result(
            f"当前群聊白名单: {whitelist if whitelist else '全部群聊'}"
        )

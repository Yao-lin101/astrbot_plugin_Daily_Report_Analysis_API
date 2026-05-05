import asyncio
import json
import traceback
from datetime import datetime, timedelta

from astrbot.api import logger
from astrbot.core.message.components import Plain
from astrbot.core.star.star_tools import StarTools

PROMPT_PREDICT_TIME = """任务目标：根据以下角色的实时状态和近日活动记录，推测用户今天的第一次活跃时间（即用户今天可能醒来/开始看手机的时间）。
如果用户当前正在活跃（例如熬夜中），请推测他们睡觉后醒来的时间。

输出 JSON 格式要求（必须只返回合法的 JSON 对象，不带 Markdown 符号等包裹）：
{
  "next_check_time": "08:30" // 格式必须为 HH:MM，24小时制
}

--- 状态记录开始 ---
{status_data}
--- 状态记录结束 ---"""

PROMPT_CHECK_STATUS = """任务目标：根据以下角色的当前实时状态记录，判断现在是否是一个合适的时机去主动发消息关怀用户（例如：提醒休息、提醒运动、早安问候等）。
如果你觉得当前不需要打扰，请判断下次什么时间再来观察（如果判断今天都不合适，可以将 continue_observing 设为 false）。

输出 JSON 格式要求（必须只返回合法的 JSON 对象，不带 Markdown 符号等包裹）：
{
  "need_message": true, // 是否需要立刻发送主动消息
  "reason": "用户似乎刚刚醒来，适合发一句早安并提醒喝水", // 简短说明理由
  "continue_observing": false, // 如果 need_message 为 false，是否需要后续继续观察
  "next_check_time": "12:30" // 格式为 HH:MM，24小时制。如果不继续观察，该字段可为空
}

--- 状态记录开始 ---
{status_data}
--- 状态记录结束 ---"""


class ActiveMessageHandler:
    def __init__(self, plugin):
        self.plugin = plugin
        self.context = plugin.context
        self.api_service = plugin.api_service
        self.loop_task = None
        self.next_check_time = None  # datetime object

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
        # 初始启动时，如果还没有预测时间，先预测一下
        try:
            while True:
                config = self.plugin.config or {}
                enabled = config.get("enable_active_messaging", False)

                if not enabled:
                    await asyncio.sleep(60)
                    continue

                now = datetime.now()

                # 每天0点（或尚未初始化下次时间且非0点的情况下）获取作息预测
                if self.next_check_time is None or (now.hour == 0 and now.minute < 5):
                    await self._predict_first_active_time(now)
                    # 避免在0点重复触发
                    if now.hour == 0 and now.minute < 5:
                        await asyncio.sleep(300)
                    continue

                if now >= self.next_check_time:
                    # 到达推测时间，执行检查
                    await self._check_and_action()

                # 每分钟检查一次
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: loop failed - {e}\n{traceback.format_exc()}"
            )
            # 异常恢复
            await asyncio.sleep(60)
            self.start()

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
                    # 如果推测的时间比现在还早（可能是跨天），加一天
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
            f"ActiveMessageHandler: 到达观察时间 {self.next_check_time}，正在评估状态..."
        )
        now = datetime.now()
        status_res = await self.api_service.fetch_status(memory="short")
        if not status_res or "prompt" not in status_res:
            logger.error("ActiveMessageHandler: 无法获取 short 状态，稍后重试。")
            self.next_check_time = now + timedelta(minutes=15)
            return

        status_data = status_res["prompt"]
        system_prompt = await self._get_system_prompt()
        prompt = PROMPT_CHECK_STATUS.format(status_data=status_data)

        provider_id = self.plugin.config.get("summary_provider_id")
        if not provider_id:
            return

        response = await self.context.llm_generate(
            chat_provider_id=provider_id,
            system_prompt=system_prompt,
            prompt=prompt,
        )

        try:
            result_json = self._parse_json(response.completion_text)
            need_message = result_json.get("need_message", False)
            reason = result_json.get("reason", "")
            continue_observing = result_json.get("continue_observing", False)
            time_str = result_json.get("next_check_time", "")

            if need_message:
                logger.info(
                    f"ActiveMessageHandler: 判定需要发消息，理由：{reason}。准备获取 hybrid 状态并生成回复..."
                )
                await self._generate_and_send_message(reason)
                # 发送完后，停止今日观察，除非有新机制。这里设为None，会在次日0点重新预测。
                self.next_check_time = (now + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0
                )
            else:
                logger.info("ActiveMessageHandler: 判定不需要发消息。")
                if continue_observing and time_str and ":" in time_str:
                    h, m = map(int, time_str.split(":"))
                    check_time = now.replace(hour=h, minute=m, second=0, microsecond=0)
                    if check_time <= now:
                        check_time += timedelta(days=1)
                    # 如果推测到了明天，就直接设为明天0点由主循环处理
                    if check_time.date() > now.date():
                        self.next_check_time = check_time.replace(
                            hour=0, minute=0, second=0
                        )
                    else:
                        self.next_check_time = check_time
                    logger.info(
                        f"ActiveMessageHandler: 下次观察时间设为 {self.next_check_time}"
                    )
                else:
                    # 停止观望
                    logger.info("ActiveMessageHandler: 停止观望，等待次日0点。")
                    self.next_check_time = (now + timedelta(days=1)).replace(
                        hour=0, minute=0, second=0
                    )

        except Exception as e:
            logger.error(
                f"ActiveMessageHandler: 解析评估状态失败 {e}。LLM 返回: {response.completion_text}"
            )
            self.next_check_time = now + timedelta(minutes=60)

    async def _generate_and_send_message(self, reason: str):
        hybrid_res = await self.api_service.fetch_status(memory="hybrid")
        if not hybrid_res or "prompt" not in hybrid_res:
            logger.error("ActiveMessageHandler: 获取 hybrid 状态失败，取消发送。")
            return

        hybrid_data = hybrid_res["prompt"]
        system_prompt = await self._get_system_prompt()

        gen_prompt = f"""任务目标：根据你的人设以及下面的角色实时状态与历史记忆档案，主动给用户发一条消息。
当前的发送动机是：{reason}
请直接输出你要发送的消息内容，不要有任何 Markdown 包裹或说明文字。

--- 状态与记忆档案 ---
{hybrid_data}"""

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

                chain = MessageChain().message([Plain(message_content)])
                # 这里我们假设用户是以私聊为主，目标是 specific_user_id
                await StarTools.send_message_by_id(
                    type="FriendMessage",
                    id=specific_user_id,
                    message_chain=chain,
                    platform="aiocqhttp",
                )
                logger.info("ActiveMessageHandler: 主动消息发送成功。")

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

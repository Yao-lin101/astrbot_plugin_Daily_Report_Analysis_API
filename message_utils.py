import re

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Face, Image, Plain, Reply, Video


async def get_bot_nickname(
    context, event: AstrMessageEvent, group_id: str, bot_nicknames: dict
) -> str:
    """获取机器人在群组中的昵称"""
    if group_id in bot_nicknames:
        return bot_nicknames[group_id]

    if event:
        try:
            group_data = await event.get_group()
            if group_data and group_data.members:
                self_id = str(event.get_self_id())
                for m in group_data.members:
                    if str(m.user_id) == self_id:
                        nick = m.nickname or m.user_id
                        bot_nicknames[group_id] = str(nick)
                        return str(nick)
        except Exception:
            pass

    nickname = context.get_config().get("nickname")
    if not nickname and event:
        nickname = str(event.get_self_id())
    return str(nickname or "机器人")


async def resolve_nickname(event: AstrMessageEvent, user_id: str, group_id: str) -> str:
    """尝试解析任意用户的昵称"""
    user_id = str(user_id)
    # 如果是机器人自己
    if user_id == str(event.get_self_id()):
        return "机器人"  # 这里的 resolve 仅用于消息解析中的 @

    # 尝试从群成员缓存找
    try:
        group_data = await event.get_group()
        if group_data and group_data.members:
            for m in group_data.members:
                if str(m.user_id) == user_id:
                    return m.nickname or str(m.user_id)
    except Exception:
        pass

    return user_id


async def format_full_message(
    event: AstrMessageEvent, group_messages: list = None
) -> str:
    """解析消息组件，保留并转化 At 信息为文本格式，同时保留图片等媒体占位符，支持引用回复解析"""
    full_content = ""
    group_id = event.message_obj.group_id

    for comp in event.message_obj.message:
        if isinstance(comp, Plain):
            full_content += comp.text
        elif isinstance(comp, At):
            target_id = getattr(comp, "qq", getattr(comp, "user_id", None))
            if target_id:
                nickname = await resolve_nickname(event, target_id, group_id)
                full_content += f"@{nickname} "
        elif isinstance(comp, Image):
            full_content += " [图片] "
        elif isinstance(comp, Face):
            full_content += " [表情] "
        elif isinstance(comp, Video):
            full_content += " [视频] "
        elif isinstance(comp, Reply):
            # 引用回复处理
            target_id = getattr(comp, "message_id", getattr(comp, "id", None))
            found_text = None
            if target_id and group_messages:
                # 尝试从历史记录中寻找被引用的内容
                for m in reversed(group_messages):
                    if str(m.get("platform_msg_id")) == str(target_id):
                        content = m.get("content", "")
                        # 尝试从格式化的内容中提取 发言人 和 实际内容
                        # content 格式通常为 "【群友/用户/你】名字: 内容"
                        if ": " in content:
                            prefix, actual_content = content.split(": ", 1)
                            # 提取名字，去除前缀标识
                            target_name = (
                                prefix.split("】", 1)[-1] if "】" in prefix else prefix
                            )
                            # 清洗内容：去除被引用消息中可能存在的嵌套回复占位符，防止递归冗余
                            clean_content = re.sub(
                                r"（回复: \".*?\"）\s*", "", actual_content
                            )
                            found_text = f"{target_name}: {clean_content}"
                        else:
                            found_text = content
                        break
            if found_text:
                full_content += f'（回复: "{found_text}"） '
            else:
                full_content += "（回复了某条消息） "
        elif type(comp).__name__ in ["Record", "Audio"]:
            full_content += " [语音] "
        elif type(comp).__name__ == "File":
            full_content += " [文件] "

    return full_content.strip() or event.message_str

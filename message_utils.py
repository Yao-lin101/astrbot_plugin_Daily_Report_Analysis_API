from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Plain


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


async def format_full_message(event: AstrMessageEvent) -> str:
    """解析消息组件，保留并转化 At 信息为文本格式"""
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

    return full_content.strip() or event.message_str

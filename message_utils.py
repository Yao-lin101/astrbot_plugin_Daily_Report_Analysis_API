import re

from astrbot.api.event import AstrMessageEvent
from astrbot.api.message_components import At, Face, Image, Plain, Reply, Video


async def get_bot_nickname(
    context, event: AstrMessageEvent, group_id: str, bot_nicknames: dict
) -> str:
    """获取机器人在群组中的昵称"""
    if group_id in bot_nicknames and bot_nicknames[group_id] != "机器人":
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
    context,
    event: AstrMessageEvent,
    group_messages: list = None,
    bot_nicknames: dict = None,
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
                if str(target_id) == str(event.get_self_id()):
                    nickname = await get_bot_nickname(
                        context, event, group_id, bot_nicknames or {}
                    )
                else:
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


def parse_json_robust(raw_result: str) -> dict:
    """Robustly parse JSON from LLM response, stripping markdown formatting and extra tags/text.

    Args:
        raw_result: The raw string response from the LLM.

    Returns:
        The parsed JSON dictionary.

    Raises:
        json.JSONDecodeError: If the string cannot be parsed as JSON even after cleanups.
    """
    import json

    def _fix_unescaped_quotes(json_str: str) -> str:
        """Escape unescaped double quotes inside JSON string values.

        Args:
            json_str: The JSON string to repair.

        Returns:
            The repaired JSON string with internal double quotes escaped.
        """
        result = []
        in_string = False
        escape = False
        i = 0
        n = len(json_str)
        while i < n:
            char = json_str[i]
            if in_string:
                if escape:
                    result.append(char)
                    escape = False
                elif char == "\\":
                    result.append(char)
                    escape = True
                elif char == '"':
                    # Check if this double quote is followed by structural characters:
                    # skip whitespace first
                    next_idx = i + 1
                    while next_idx < n and json_str[next_idx].isspace():
                        next_idx += 1

                    # What can follow a string in valid JSON?
                    # - ':' (if it was a key)
                    # - ',' (if it was a value/element or key-value pair separator)
                    # - '}' (end of object)
                    # - ']' (end of array)
                    if next_idx < n and json_str[next_idx] in (":", ",", "}", "]"):
                        result.append(char)
                        in_string = False
                    else:
                        result.append('\\"')
                else:
                    result.append(char)
            else:
                if char == '"':
                    in_string = True
                result.append(char)
            i += 1
        return "".join(result)

    raw_result = raw_result.strip()

    # 1. Clean markdown code blocks if any
    if raw_result.startswith("```json"):
        raw_result = raw_result[7:]
    elif raw_result.startswith("```"):
        raw_result = raw_result[3:]
    if raw_result.endswith("```"):
        raw_result = raw_result[:-3]

    raw_result = raw_result.strip()

    # 2. Try standard json.loads
    try:
        return json.loads(raw_result)
    except json.JSONDecodeError as e:
        # Try repairing quotes
        try:
            fixed = _fix_unescaped_quotes(raw_result)
            return json.loads(fixed)
        except json.JSONDecodeError:
            pass

        # 3. Fallback: find first '{' and try parsing from largest matching closing brace
        start_idx = raw_result.find("{")
        if start_idx != -1:
            indices = [i for i, char in enumerate(raw_result) if char == "}"]
            for end_idx in reversed(indices):
                if end_idx > start_idx:
                    try:
                        candidate = raw_result[start_idx : end_idx + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            fixed_candidate = _fix_unescaped_quotes(candidate)
                            return json.loads(fixed_candidate)
                    except json.JSONDecodeError:
                        continue

        # 4. Fallback: find first '[' and try parsing from largest matching closing bracket
        start_arr = raw_result.find("[")
        if start_arr != -1:
            indices = [i for i, char in enumerate(raw_result) if char == "]"]
            for end_arr in reversed(indices):
                if end_arr > start_arr:
                    try:
                        candidate = raw_result[start_arr : end_arr + 1]
                        try:
                            return json.loads(candidate)
                        except json.JSONDecodeError:
                            fixed_candidate = _fix_unescaped_quotes(candidate)
                            return json.loads(fixed_candidate)
                    except json.JSONDecodeError:
                        continue

        raise e

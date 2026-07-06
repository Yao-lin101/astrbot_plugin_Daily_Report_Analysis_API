from datetime import datetime, timedelta, timezone

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


def _parse_utc_date(date_str: str) -> datetime:
    """将 ISO 8601 日期字符串解析为带有 UTC 时区的 datetime 对象。

    Args:
        date_str: ISO 8601 格式的日期字符串，例如 "2026-07-06T10:46:42Z"。

    Returns:
        包含 UTC 时区信息的 datetime 对象。
    """
    if date_str.endswith("Z"):
        date_str = date_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(date_str)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


async def get_stillalive_status_impl(
    plugin: any, event: AstrMessageEvent, name: str
) -> str:
    """获取指定角色的真实实时数据及今日日报。

    Args:
        plugin: 插件实例。
        event: 消息事件对象。
        name: 角色名称。

    Returns:
        解析后的角色实时数据和今日日报文本。
    """
    characters = plugin.config.get("characters", [])
    matched = None
    for char in characters:
        if char.get("name", "").strip().lower() == name.strip().lower():
            matched = char
            break

    if not matched:
        available_names = [c.get("name") for c in characters if c.get("name")]
        if available_names:
            return f"未找到名为 '{name}' 的角色。当前已配置的可直接查询角色列表：{', '.join(available_names)}"
        else:
            return f"未找到名为 '{name}' 的角色。当前未配置任何角色列表，请在后台插件配置中添加角色。"

    base_url = plugin.config.get("target_url", "https://alive.ineed.asia/").rstrip("/")
    display_code = matched.get("display_code")
    key = matched.get("key")

    now_utc = datetime.now(timezone.utc)
    today_str = now_utc.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")

    headers = {}
    if key:
        headers["X-Character-Key"] = key

    config_data = {}
    status_data = {}
    report_data = {}

    try:
        timeout = aiohttp.ClientTimeout(total=15.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{base_url}/api/v1/d/{display_code}/", headers=headers
            ) as resp:
                if resp.status == 200:
                    config_data = await resp.json()
            async with session.get(
                f"{base_url}/api/v1/d/{display_code}/status/", headers=headers
            ) as resp:
                if resp.status == 200:
                    status_data = await resp.json()
            async with session.get(
                f"{base_url}/api/v1/d/{display_code}/reports/detail/?date={today_str}",
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    report_data = await resp.json()
    except Exception as e:
        logger.error(f"DailyReportAnalysisAPI (get_stillalive_status): 请求异常 {e}")
        return f"获取状态失败，网络请求异常: {str(e)}"

    if not config_data or not status_data:
        return f"获取角色 '{name}' 的状态失败，API 返回异常或未成功获取数据。"

    report_content = report_data.get("markdown", "今日暂无日报记录。")
    vital_config_dict = config_data.get("status_config", {}).get("vital_signs", {})
    config_map = {item["key"]: item for item in vital_config_dict.values()}

    # 解析动态数据块
    sections = []
    status_categories = status_data.get("status_data", {})

    for cat_key, cat_info in status_categories.items():
        cat_data = cat_info.get("data", {})
        cat_time_str = cat_info.get("updated_at")

        if not cat_data:
            continue

        time_display = ""
        if cat_time_str:
            try:
                cat_time = _parse_utc_date(cat_time_str)
                delta = now_utc - cat_time
                delta_seconds = delta.total_seconds()

                if delta_seconds > 86400:  # 忽略超过 24 小时的数据
                    continue

                if delta_seconds < 60:
                    t_ago = "刚刚"
                elif delta_seconds < 3600:
                    t_ago = f"{int(delta_seconds // 60)} 分钟前"
                else:
                    t_ago = f"{int(delta_seconds // 3600)} 小时前"
                time_display = f"({t_ago})"
            except Exception:
                pass

        lines = []
        for k, v in cat_data.items():
            if k in config_map:
                cfg = config_map[k]
                desc = cfg.get("description", cfg.get("label", k))
                suffix = cfg.get("suffix", "")
                lines.append(f"  - {desc}：{v}{suffix}")
            else:
                lines.append(f"  - {k}：{v}")

        if lines:
            sections.append(f"[{cat_key}] {time_display}\n" + "\n".join(lines))

    realtime_text = "\n\n".join(sections) if sections else "暂无实时数据。"

    # 解析最新活跃设备/App
    mac_info = status_categories.get("mac", {})
    vital_info = status_categories.get("vital_signs", {})

    m_time = datetime.min.replace(tzinfo=timezone.utc)
    v_time = datetime.min.replace(tzinfo=timezone.utc)

    if mac_info.get("updated_at"):
        try:
            m_time = _parse_utc_date(mac_info["updated_at"])
        except Exception:
            pass
    if vital_info.get("updated_at"):
        try:
            v_time = _parse_utc_date(vital_info["updated_at"])
        except Exception:
            pass

    if m_time > v_time:
        latest_time = m_time
        app_name = mac_info.get("data", {}).get("mac", "未知")
        latest_str = f"Mac 正在使用：{app_name}"
    else:
        latest_time = v_time
        app_name = vital_info.get("data", {}).get("phone", "未知")
        latest_str = f"手机正在使用：{app_name}"

    try:
        delta = now_utc - latest_time
        delta_seconds = delta.total_seconds()

        if delta_seconds < 60:
            t_ago = "刚刚"
        elif delta_seconds < 3600:
            t_ago = f"{int(delta_seconds // 60)} 分钟前"
        elif delta_seconds < 86400:
            t_ago = f"{int(delta_seconds // 3600)} 小时前"
        else:
            t_ago = f"{int(delta_seconds // 86400)} 天前"

        latest_formatted = f"{latest_str} （最后活跃：{t_ago}）"
    except Exception:
        latest_formatted = "无法解析最新活跃时间"

    return f"""角色 {matched.get("name")} 在 {today_str} 的日报内容：
{report_content}

实时数据：
{realtime_text}

概览：
{latest_formatted}"""


async def search_stillalive_memory_impl(
    plugin: any, event: AstrMessageEvent, name: str, query: str
) -> str:
    """通过语义搜索角色的长时记忆档案、过去发生的事件或话题偏好。

    Args:
        plugin: 插件实例。
        event: 消息事件对象。
        name: 角色名称。
        query: 检索词。

    Returns:
        检索到的长期记忆文本。
    """
    characters = plugin.config.get("characters", [])
    matched = None
    for char in characters:
        if char.get("name", "").strip().lower() == name.strip().lower():
            matched = char
            break

    if not matched:
        available_names = [c.get("name") for c in characters if c.get("name")]
        if available_names:
            return f"未找到名为 '{name}' 的角色。当前已配置的可直接查询角色列表：{', '.join(available_names)}"
        else:
            return f"未找到名为 '{name}' 的角色。当前未配置任何角色列表，请在后台插件配置中添加角色。"

    key = matched.get("key")
    if not key:
        return f"无法检索角色 '{matched.get('name')}' 的记忆，因为未配置其检索秘钥（key）。"

    base_url = plugin.config.get("target_url", "https://alive.ineed.asia/").rstrip("/")
    url = f"{base_url}/api/v1/bot/status/"
    headers = {
        "X-Character-Key": key,
    }
    params = {"memory": "long", "q": query}

    try:
        timeout = aiohttp.ClientTimeout(total=15.0)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    prompt_text = data.get("prompt", "暂无匹配的长期记忆。")
                    replacements = [
                        "## 可用的长期重要记忆\n",
                        "## 可能相关的长期重要记忆\n",
                        "\n可以在这些内容中寻找话题。",
                        "\n请把这些记忆当作背景线索自然使用；如果今天数据不相关，不要强行提及。",
                    ]
                    for r in replacements:
                        prompt_text = prompt_text.replace(r, "")
                    return prompt_text.strip()
                else:
                    return f"记忆检索失败，状态码：{resp.status}"
    except Exception as e:
        logger.error(f"DailyReportAnalysisAPI (search_stillalive_memory): 请求异常 {e}")
        return f"记忆检索失败，网络请求异常: {str(e)}"

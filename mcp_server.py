import os
from datetime import datetime

import httpx
from dateutil.parser import parse
from mcp.server.fastmcp import FastMCP

import json

# 初始化 MCP 服务器
mcp = FastMCP("StillAlive-Status-MCP")

# 尝试从 AstrBot 的插件配置中加载数据
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "../../config/astrbot_plugin_Daily_Report_Analysis_API_config.json",
)

plugin_config = {}
if os.path.exists(CONFIG_PATH):
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            plugin_config = json.load(f)
    except Exception:
        pass

# 环境变量获取配置，提供默认值
BASE_URL = plugin_config.get("target_url")
if not BASE_URL:
    BASE_URL = os.environ.get("STILLALIVE_BASE_URL", "")
BASE_URL = BASE_URL.rstrip("/")

if BASE_URL and not BASE_URL.startswith("http"):
    # 如果配置里没写协议，尝试补全
    BASE_URL = "http://" + BASE_URL

CHARACTER_KEY = plugin_config.get("character_key")
if not CHARACTER_KEY:
    CHARACTER_KEY = os.environ.get("STILLALIVE_CHARACTER_KEY", "")


# 如果有全局的请求头鉴权需求，可以在这里添加
def get_headers():
    headers = {}
    if CHARACTER_KEY:
        headers["X-Character-Key"] = CHARACTER_KEY
    return headers


SPECIFIC_USER_ID = plugin_config.get("specific_user_id") or "e.e."


@mcp.tool(
    description=(
        f"【核心状态工具】获取目标用户（{SPECIFIC_USER_ID}）的真实实时数据。 "
        f"当用户（即 {SPECIFIC_USER_ID}，或其自称为'老师'、'我'）询问其目前的实时状态、"
        "步数、电量、正在使用的App、活动动向或查看今日日报时，你必须调用此工具。 "
        "请记住：你本身没有任何实时监控能力，严禁通过推测或编造来回复，必须以此工具返回的数据为准。"
    )
)
async def get_character_status() -> str:
    today_str = datetime.now().strftime("%Y-%m-%d")

    async with httpx.AsyncClient() as client:
        # 1. 并发获取三个 API 的数据
        headers = get_headers()
        try:
            config_res = await client.get(f"{BASE_URL}/api/v1/d/e.e./", headers=headers)
            status_res = await client.get(
                f"{BASE_URL}/api/v1/d/e.e./status/", headers=headers
            )
            report_res = await client.get(
                f"{BASE_URL}/api/v1/d/e.e./reports/detail/?date={today_str}",
                headers=headers,
            )

            if config_res.status_code != 200 or status_res.status_code != 200:
                return f"获取状态失败，API 返回异常。\nConfig Status: {config_res.status_code}\nStatus API: {status_res.status_code}\nReport API: {report_res.status_code}\nHeaders sent: {headers}"

            config_data = config_res.json()
            status_data = status_res.json()
            report_data = report_res.json() if report_res.status_code == 200 else {}
        except Exception as e:
            return f"获取状态失败，网络请求异常: {str(e)}"

    # ========== 解析 1: 当日日报 ==========
    report_content = report_data.get("markdown")
    if not report_content:
        report_content = "今日暂无日报记录。"

    # ========== 解析 2: 实时数据映射 ==========
    vital_config_dict = config_data.get("status_config", {}).get("vital_signs", {})
    # 转换为按 key 索引的字典，方便查找：{"battery": {"description": "手机电量", "suffix": "%", ...}}
    config_map = {item["key"]: item for item in vital_config_dict.values()}

    realtime_lines = []
    status_categories = status_data.get("status_data", {})

    # 遍历所有类别提取实时数据
    for category_name, category_info in status_categories.items():
        cat_data = category_info.get("data", {})
        for k, v in cat_data.items():
            if k in config_map:
                cfg = config_map[k]
                desc = cfg.get("description", cfg.get("label", k))
                suffix = cfg.get("suffix", "")
                realtime_lines.append(f"{desc}：{v}{suffix}")
            else:
                # 兜底：如果配置里没有，但在 status_data 中存在的数据，直接原样显示
                # 如果不需要兜底，可以注释掉下面这一行
                realtime_lines.append(f"{k}：{v}")

    # ========== 解析 3: 最新状态（对比 mac 和 phone） ==========
    mac_info = status_categories.get("mac", {})
    vital_info = status_categories.get("vital_signs", {})

    # 默认时间设为很久以前
    mac_time_str = mac_info.get("updated_at", "1970-01-01T00:00:00Z")
    phone_time_str = vital_info.get("updated_at", "1970-01-01T00:00:00Z")

    try:
        mac_time = parse(mac_time_str)
        phone_time = parse(phone_time_str)
        now = datetime.now(mac_time.tzinfo)  # 保持时区一致

        # 比较最新时间
        if mac_time > phone_time:
            latest_time = mac_time
            app_name = mac_info.get("data", {}).get("mac", "未知App")
            latest_str = f"mac：{app_name}"
        else:
            latest_time = phone_time
            app_name = vital_info.get("data", {}).get("phone", "未知App")
            latest_str = f"phone：{app_name}"

        # 计算过去了多久
        delta_seconds = (now - latest_time).total_seconds()
        if delta_seconds < 60:
            time_ago = "刚刚"
        elif delta_seconds < 3600:
            time_ago = f"{int(delta_seconds // 60)} 分钟前"
        else:
            time_ago = f"{int(delta_seconds // 3600)} 小时前"

        latest_formatted = f"{latest_str} （数据时间：{latest_time.strftime('%H:%M')}，距今 {time_ago}）"
    except Exception as e:
        latest_formatted = f"解析时间失败：{str(e)}"

    # ========== 最终拼接 ==========
    # 如果 realtime_lines 为空，给一个兜底文本
    realtime_text = "\n".join(realtime_lines) if realtime_lines else "暂无实时数据。"

    final_output = f"""当日日报：
{report_content}

实时数据：
{realtime_text}

最新状态：
{latest_formatted}"""

    return final_output


@mcp.tool(
    description=(
        f"【长期记忆检索】通过语义搜索关于用户（{SPECIFIC_USER_ID}）的长时记忆档案、过去发生的事件或话题偏好。 "
        "当用户询问以前发生过的事、过去的经历、曾经的讨论或需要查阅过去的资料时调用。 "
        "请利用此工具获取真实的历史线索，避免回答‘我不记得了’。"
    )
)
async def search_historical_memory(query: str) -> str:
    """
    Args:
        query: 搜索关键词，例如 "周末去哪里玩"、"关于找工作的讨论"
    """
    async with httpx.AsyncClient() as client:
        headers = get_headers()
        url = f"{BASE_URL}/api/v1/bot/status/"
        try:
            res = await client.get(
                url, headers=headers, params={"memory": "long", "q": query}
            )
            if res.status_code == 200:
                data = res.json()
                prompt_text = data.get("prompt", "暂无匹配的长期记忆。")
                # 剔除首尾冗余的指导语，因为 MCP 环境下不需要对模型进行额外暗示
                prompt_text = prompt_text.replace("## 可用的长期重要记忆\n", "")
                prompt_text = prompt_text.replace("## 可能相关的长期重要记忆\n", "")
                prompt_text = prompt_text.replace("\n可以在这些内容中寻找话题。", "")
                prompt_text = prompt_text.replace(
                    "\n请把这些记忆当作背景线索自然使用；如果今天数据不相关，不要强行提及。",
                    "",
                )
                return prompt_text.strip()
            else:
                return f"记忆检索失败，状态码：{res.status_code}，响应：{res.text}"
        except Exception as e:
            return f"记忆检索失败，网络请求异常: {str(e)}"


if __name__ == "__main__":
    # 以 stdio 模式启动，供 AstrBot 客户端调用
    mcp.run(transport="stdio")

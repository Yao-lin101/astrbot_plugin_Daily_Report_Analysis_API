import os
import json
import httpx
from datetime import datetime, timezone, timedelta
from dateutil.parser import parse
from mcp.server.fastmcp import FastMCP

# 初始化 MCP 服务器
mcp = FastMCP("StillAlive-Status-MCP")

# 1. 环境变量加载逻辑
BASE_URL = os.environ.get("STILLALIVE_BASE_URL", "https://alive.ineed.asia/").rstrip("/")

# 解析角色配置
CHARACTERS_RAW = os.environ.get("STILLALIVE_CHARACTERS", "[]").strip()
CHARACTERS = []

if CHARACTERS_RAW:
    # 逻辑：如果是路径则检查并创建，否则解析字符串
    # 判断是否看起来像个路径（包含斜杠或.json结尾）
    is_path = "/" in CHARACTERS_RAW or CHARACTERS_RAW.endswith(".json")
    
    if is_path:
        try:
            # 如果文件不存在，自动创建初始模板
            if not os.path.exists(CHARACTERS_RAW):
                with open(CHARACTERS_RAW, "w", encoding="utf-8") as f:
                    json.dump([], f, indent=2)
            
            with open(CHARACTERS_RAW, "r", encoding="utf-8-sig") as f:
                CHARACTERS = json.load(f)
        except Exception:
            CHARACTERS = []
    else:
        try:
            CHARACTERS = json.loads(CHARACTERS_RAW)
        except Exception:
            CHARACTERS = []

# 建立快速查找索引
DISPLAY_TO_CHAR = {c["display_code"]: c for c in CHARACTERS if "display_code" in c}
USER_ID_TO_CHAR = {c["user_id"]: c for c in CHARACTERS if "user_id" in c}

def get_character_info_prompt() -> str:
    """生成用于工具描述的角色信息提示，帮助 LLM 智能识别"""
    if not CHARACTERS:
        return ""
    info_parts = []
    for c in CHARACTERS:
        aliases = ", ".join(c.get("alias", []))
        part = f"- 昵称: {c.get('name')}"
        if aliases:
            part += f" (别名: {aliases})"
        part += f", 展示码: {c.get('display_code')}"
        if "user_id" in c:
            part += f", 用户ID: {c.get('user_id')}"
        info_parts.append(part)
    return "\n当前配置的可直接查询角色列表：\n" + "\n".join(info_parts)

# 动态生成描述前缀
CHAR_PROMPT = get_character_info_prompt()

# 通用请求头处理
def get_headers(display_code: str = None):
    headers = {}
    if display_code and display_code in DISPLAY_TO_CHAR:
        key = DISPLAY_TO_CHAR[display_code].get("key")
        if key:
            headers["X-Character-Key"] = key
    return headers

@mcp.tool(
    description=(
        "【角色列表工具】获取当前 StillAlive 平台上活跃的角色列表（过滤掉 24 小时内无活跃记录的角色）。"
        "当用户询问“有哪些人”、“谁在线”或你无法确定用户指的是哪个角色时调用此工具。"
        "输出格式：1、昵称：展示码："
    )
)
async def get_character_list() -> str:
    url = f"{BASE_URL}/api/v1/survivors/"
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(url)
            if res.status_code != 200:
                return f"获取列表失败，状态码：{res.status_code}"
            
            data = res.json()
            results = data.get("results", [])
            now = datetime.now(timezone.utc)
            
            active_chars = []
            for item in results:
                last_updated_str = item.get("last_updated")
                if not last_updated_str:
                    continue
                
                try:
                    last_updated = parse(last_updated_str)
                    # 统一转为 UTC 进行比较
                    if last_updated.tzinfo is None:
                        last_updated = last_updated.replace(tzinfo=timezone.utc)
                    
                    delta = now - last_updated
                    if delta.total_seconds() <= 86400:  # 24小时
                        active_chars.append(item)
                except Exception:
                    continue
            
            if not active_chars:
                return "当前 24 小时内没有活跃角色。"
            
            output_lines = []
            for i, c in enumerate(active_chars, 1):
                output_lines.append(f"{i}、{c.get('name')}：展示码：{c.get('display_code')}")
            
            return "\n".join(output_lines)
            
        except Exception as e:
            return f"获取列表请求异常: {str(e)}"

@mcp.tool(
    description=(
        "【核心状态工具】获取指定角色的真实实时数据。包含步数、电量、当前 App、位置及今日日报 Markdown。"
        "LLM 应当优先通过上下文或以下配置判断 display_code。"
        f"{CHAR_PROMPT}"
    )
)
async def get_character_status(display_code: str) -> str:
    """
    Args:
        display_code: 目标角色的展示码，例如 "e.e." 或 "iybNOo"
    """
    if not BASE_URL:
        return "获取状态失败：未配置 STILLALIVE_BASE_URL。"

    # 强制使用北京时间 (UTC+8) 获取日期
    tz_beijing = timezone(timedelta(hours=8))
    now_beijing = datetime.now(tz_beijing)
    today_str = now_beijing.strftime("%Y-%m-%d")
    
    headers = get_headers(display_code)

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            # 使用传入的 display_code 构造路径
            config_res = await client.get(f"{BASE_URL}/api/v1/d/{display_code}/", headers=headers)
            status_res = await client.get(f"{BASE_URL}/api/v1/d/{display_code}/status/", headers=headers)
            report_res = await client.get(
                f"{BASE_URL}/api/v1/d/{display_code}/reports/detail/?date={today_str}",
                headers=headers,
            )

            if config_res.status_code != 200 or status_res.status_code != 200:
                return f"获取状态失败，API 返回异常。状态码: {config_res.status_code}/{status_res.status_code}"

            config_data = config_res.json()
            status_data = status_res.json()
            report_data = report_res.json() if report_res.status_code == 200 else {}
        except Exception as e:
            return f"获取状态失败，网络请求异常: {str(e)}"

    # 解析逻辑
    report_content = report_data.get("markdown", "今日暂无日报记录。")
    vital_config_dict = config_data.get("status_config", {}).get("vital_signs", {})
    config_map = {item["key"]: item for item in vital_config_dict.values()}

    realtime_lines = []
    status_categories = status_data.get("status_data", {})

    for category_name, category_info in status_categories.items():
        cat_data = category_info.get("data", {})
        for k, v in cat_data.items():
            if k in config_map:
                cfg = config_map[k]
                desc = cfg.get("description", cfg.get("label", k))
                suffix = cfg.get("suffix", "")
                realtime_lines.append(f"{desc}：{v}{suffix}")
            else:
                realtime_lines.append(f"{k}：{v}")

    mac_info = status_categories.get("mac", {})
    vital_info = status_categories.get("vital_signs", {})

    mac_time_str = mac_info.get("updated_at", "1970-01-01T00:00:00Z")
    phone_time_str = vital_info.get("updated_at", "1970-01-01T00:00:00Z")

    try:
        mac_time = parse(mac_time_str)
        phone_time = parse(phone_time_str)
        # 统一时区处理
        if mac_time.tzinfo is None: mac_time = mac_time.replace(tzinfo=timezone.utc)
        if phone_time.tzinfo is None: phone_time = phone_time.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)

        if mac_time > phone_time:
            latest_time, app_name = mac_time, mac_info.get("data", {}).get("mac", "未知App")
            latest_str = f"mac：{app_name}"
        else:
            latest_time, app_name = phone_time, vital_info.get("data", {}).get("phone", "未知App")
            latest_str = f"phone：{app_name}"

        delta_seconds = (now - latest_time).total_seconds()
        if delta_seconds < 60: time_ago = "刚刚"
        elif delta_seconds < 3600: time_ago = f"{int(delta_seconds // 60)} 分钟前"
        else: time_ago = f"{int(delta_seconds // 3600)} 小时前"

        latest_formatted = f"{latest_str} （数据时间：{latest_time.strftime('%H:%M')}，距今 {time_ago}）"
    except Exception as e:
        latest_formatted = f"解析时间失败：{str(e)}"

    realtime_text = "\n".join(realtime_lines) if realtime_lines else "暂无实时数据。"

    return f"""角色在 {today_str} 的日报内容：
{report_content}

实时数据：
{realtime_text}

最新状态：
{latest_formatted}"""

@mcp.tool(
    description=(
        "【长期记忆检索】通过语义搜索指定角色的长时记忆档案、过去发生的事件或话题偏好。"
        "此工具完全基于角色密钥进行身份识别。LLM 应当根据角色昵称从以下配置中选择对应的 display_code 以获取其密钥进行查询。"
        f"{CHAR_PROMPT}"
    )
)
async def search_historical_memory(display_code: str, query: str) -> str:
    """
    Args:
        display_code: 目标角色的展示码
        query: 搜索关键词
    """
    headers = get_headers(display_code)
    async with httpx.AsyncClient() as client:
        url = f"{BASE_URL}/api/v1/bot/status/"
        try:
            res = await client.get(url, headers=headers, params={"memory": "long", "q": query})
            if res.status_code == 200:
                data = res.json()
                prompt_text = data.get("prompt", "暂无匹配的长期记忆。")
                # 剔除首尾冗余的指导语
                replacements = ["## 可用的长期重要记忆\n", "## 可能相关的长期重要记忆\n", 
                               "\n可以在这些内容中寻找话题。", "\n请把这些记忆当作背景线索自然使用；如果今天数据不相关，不要强行提及。"]
                for r in replacements:
                    prompt_text = prompt_text.replace(r, "")
                return prompt_text.strip()
            return f"记忆检索失败，状态码：{res.status_code}"
        except Exception as e:
            return f"记忆检索失败，网络请求异常: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="stdio")

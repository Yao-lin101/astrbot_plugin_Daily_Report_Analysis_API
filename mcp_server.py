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

    # 统一使用 UTC 时间进行计算
    now_utc = datetime.now(timezone.utc)
    tz_beijing = timezone(timedelta(hours=8))
    today_str = now_utc.astimezone(tz_beijing).strftime("%Y-%m-%d")
    
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
                return (f"获取状态失败，API 返回异常。\n"
                        f"Config Status: {config_res.status_code}, Status API: {status_res.status_code}")

            config_data = config_res.json()
            status_data = status_res.json()
            report_data = report_res.json() if report_res.status_code == 200 else {}
        except Exception as e:
            return f"获取状态失败，网络请求异常: {str(e)}"

    # 解析逻辑
    report_content = report_data.get("markdown", "今日暂无日报记录。")
    vital_config_dict = config_data.get("status_config", {}).get("vital_signs", {})
    config_map = {item["key"]: item for item in vital_config_dict.values()}

    # 辅助：确保时间对象是 UTC 觉醒的
    def ensure_utc(dt_obj_or_str):
        if isinstance(dt_obj_or_str, str):
            dt = parse(dt_obj_or_str)
        else:
            dt = dt_obj_or_str
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    # ========== 解析 2: 动态解析所有数据块 ==========
    sections = []
    status_categories = status_data.get("status_data", {})
    
    for cat_key, cat_info in status_categories.items():
        cat_data = cat_info.get("data", {})
        cat_time_str = cat_info.get("updated_at")
        
        if not cat_data:
            continue
            
        # 计算该块的距今时间
        time_display = ""
        if cat_time_str:
            try:
                cat_time = ensure_utc(cat_time_str)
                delta = now_utc - cat_time
                delta_seconds = delta.total_seconds()
                
                # 剔除超过 24 小时（1天）的过时数据块
                if delta_seconds > 86400:
                    continue
                
                if delta_seconds < 60: t_ago = "刚刚"
                elif delta_seconds < 3600: t_ago = f"{int(delta_seconds // 60)} 分钟前"
                else: t_ago = f"{int(delta_seconds // 3600)} 小时前"
                time_display = f"({t_ago})"
            except:
                pass
        
        # 格式化该块的数据
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

    # ========== 解析 3: 全局最新活跃汇总 ==========
    mac_info = status_categories.get("mac", {})
    vital_info = status_categories.get("vital_signs", {})
    
        # 统一时区处理辅助逻辑
        def ensure_utc(dt_str):
            dt = parse(dt_str)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)

        m_time = ensure_utc(mac_info.get("updated_at", "1970-01-01T00:00:00Z"))
        v_time = ensure_utc(vital_info.get("updated_at", "1970-01-01T00:00:00Z"))
        
        if m_time > v_time:
            latest_time, app_name = m_time, mac_info.get("data", {}).get("mac", "未知")
            latest_str = f"Mac 正在使用：{app_name}"
        else:
            latest_time, app_name = v_time, vital_info.get("data", {}).get("phone", "未知")
            latest_str = f"手机正在使用：{app_name}"

        # 转换到北京时间显示
        latest_formatted = f"{latest_str} （最后活跃：{latest_time.astimezone(tz_beijing).strftime('%H:%M')}）"
    except:
        latest_formatted = "无法解析最新活跃时间"

    return f"""角色在 {today_str} 的日报内容：
{report_content}

实时数据：
{realtime_text}

概览：
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

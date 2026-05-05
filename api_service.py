import aiohttp

from astrbot.api import logger


class APIService:
    def __init__(self, base_url: str, character_key: str):
        self.base_url = base_url.rstrip("/")
        self.character_key = character_key

    async def send_data(self, endpoint: str, data: dict):
        """发送数据到目标API"""
        url = f"{self.base_url}{endpoint}"
        headers = {
            "X-Character-Key": self.character_key,
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, json=data, headers=headers) as response:
                    if response.status == 200:
                        logger.info(f"APIService: {data.get('type')} 发送成功")
                        return True
                    else:
                        logger.error(f"APIService: 发送失败，状态码：{response.status}")
                        return False
        except Exception as e:
            logger.error(f"APIService: 发送请求时出错：{str(e)}")
            return False

    async def fetch_report(self, date: str):
        """获取日报详情"""
        # 路径: /api/v1/d/e.e./reports/detail/?date=...
        url = f"{self.base_url}/api/v1/d/e.e./reports/detail/?date={date}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    # 无论状态码是多少，只要有内容就尝试解析 JSON
                    try:
                        res_data = await response.json()
                        if response.status == 200:
                            return res_data
                        else:
                            # 即使不是 200，也将错误信息返回给上层处理
                            return res_data
                    except Exception:
                        logger.error(
                            f"APIService: 获取日报失败，状态码：{response.status}"
                        )
                        return None
        except Exception as e:
            logger.error(f"APIService: 获取日报时出错：{str(e)}")
            return None

    async def fetch_status(self, memory: str = "short", q: str = ""):
        """获取角色的实时活动状态"""
        url = f"{self.base_url}/api/v1/bot/status/"
        params = {"memory": memory}
        if q:
            params["q"] = q

        headers = {
            "X-Character-Key": self.character_key,
        }
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, headers=headers) as response:
                    try:
                        res_data = await response.json()
                        if response.status == 200:
                            return res_data
                        else:
                            return res_data
                    except Exception:
                        logger.error(
                            f"APIService: 获取状态失败，状态码：{response.status}"
                        )
                        return None
        except Exception as e:
            logger.error(f"APIService: 获取状态请求时出错：{str(e)}")
            return None

import sqlite3
from datetime import datetime

from astrbot.api import logger


class Storage:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _get_conn(self):
        return sqlite3.connect(self.db_path)

    def _init_db(self):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # 1. 群消息表
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT,
                    msg_id_in_group INTEGER,
                    sender_id TEXT,
                    sender_name TEXT,
                    content TEXT,
                    timestamp REAL,
                    platform_msg_id TEXT,
                    is_specific_user INTEGER DEFAULT 0 -- 0: 否, 1: 是 (对于机器人，表示是否回复给特定用户)
                )
            """)
            # 2. 群元数据表 (存储进度和计数器)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS group_meta (
                    group_id TEXT PRIMARY KEY,
                    group_name TEXT,
                    last_summarized_id INTEGER DEFAULT 0,
                    message_id_counter INTEGER DEFAULT 0,
                    user_nickname TEXT,
                    bot_nickname TEXT
                )
            """)
            # 3. 私聊消息表 (流式记录身份)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS private_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    role TEXT, -- 'user' 或 'bot'
                    content TEXT,
                    timestamp REAL
                )
            """)
            # 4. 插件全局元数据表 (用于存储主动消息时间等)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plugin_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)

            # --- 热更新检查：补齐缺失字段 ---
            cursor.execute("PRAGMA table_info(group_meta)")
            columns = [column[1] for column in cursor.fetchall()]
            if "user_nickname" not in columns:
                try:
                    cursor.execute(
                        "ALTER TABLE group_meta ADD COLUMN user_nickname TEXT"
                    )
                    cursor.execute(
                        "ALTER TABLE group_meta ADD COLUMN bot_nickname TEXT"
                    )
                    logger.info("DailyReportAnalysisAPI: 数据库 group_meta 已升级。")
                except Exception:
                    pass

            cursor.execute("PRAGMA table_info(private_messages)")
            p_columns = [column[1] for column in cursor.fetchall()]
            if p_columns and "role" not in p_columns:
                try:
                    # 私聊表结构变动较大，直接重建
                    cursor.execute("DROP TABLE private_messages")
                    cursor.execute("""
                        CREATE TABLE IF NOT EXISTS private_messages (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            user_id TEXT,
                            role TEXT,
                            content TEXT,
                            timestamp REAL
                        )
                    """)
                    logger.info(
                        "DailyReportAnalysisAPI: 数据库 private_messages 已重构。"
                    )
                except Exception:
                    pass

            cursor.execute("PRAGMA table_info(group_messages)")
            g_columns = [column[1] for column in cursor.fetchall()]
            if "is_specific_user" not in g_columns:
                try:
                    cursor.execute(
                        "ALTER TABLE group_messages ADD COLUMN is_specific_user INTEGER DEFAULT 0"
                    )
                    logger.info(
                        "DailyReportAnalysisAPI: 数据库 group_messages 已升级。"
                    )
                except Exception:
                    pass

            conn.commit()

    # --- 群组相关操作 ---

    def get_group_meta(self, group_id):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT group_name, last_summarized_id, message_id_counter, user_nickname, bot_nickname FROM group_meta WHERE group_id = ?",
                (group_id,),
            )
            return cursor.fetchone()

    def update_group_meta(
        self,
        group_id,
        group_name=None,
        last_summarized_id=None,
        message_id_counter=None,
        user_nickname=None,
        bot_nickname=None,
    ):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # 先确保存在
            cursor.execute(
                "INSERT OR IGNORE INTO group_meta (group_id, group_name) VALUES (?, ?)",
                (group_id, group_name or "未知群聊"),
            )

            if group_name:
                cursor.execute(
                    "UPDATE group_meta SET group_name = ? WHERE group_id = ?",
                    (group_name, group_id),
                )
            if last_summarized_id is not None:
                cursor.execute(
                    "UPDATE group_meta SET last_summarized_id = ? WHERE group_id = ?",
                    (last_summarized_id, group_id),
                )
            if message_id_counter is not None:
                cursor.execute(
                    "UPDATE group_meta SET message_id_counter = ? WHERE group_id = ?",
                    (message_id_counter, group_id),
                )
            if user_nickname:
                cursor.execute(
                    "UPDATE group_meta SET user_nickname = ? WHERE group_id = ?",
                    (user_nickname, group_id),
                )
            if bot_nickname:
                cursor.execute(
                    "UPDATE group_meta SET bot_nickname = ? WHERE group_id = ?",
                    (bot_nickname, group_id),
                )
            conn.commit()

    def add_group_message(
        self,
        group_id,
        msg_id,
        sender_id,
        sender_name,
        content,
        timestamp,
        platform_msg_id,
        is_specific_user=False,
    ):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO group_messages (group_id, msg_id_in_group, sender_id, sender_name, content, timestamp, platform_msg_id, is_specific_user)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
                (
                    group_id,
                    msg_id,
                    sender_id,
                    sender_name,
                    content,
                    timestamp,
                    str(platform_msg_id),
                    1 if is_specific_user else 0,
                ),
            )
            conn.commit()

    def get_pending_messages(self, group_id, last_id, limit=100):
        """获取尚未总结的消息"""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT msg_id_in_group as id, sender_id, sender_name, content, timestamp
                FROM group_messages
                WHERE group_id = ? AND msg_id_in_group > ?
                ORDER BY msg_id_in_group ASC
                LIMIT ?
            """,
                (group_id, last_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def clean_old_messages(self, days=7):
        """清理旧消息，防止数据库过大"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            limit_ts = datetime.now().timestamp() - (days * 86400)
            cursor.execute(
                "DELETE FROM group_messages WHERE timestamp < ?", (limit_ts,)
            )
            conn.commit()

    # --- 私聊相关操作 ---

    def add_private_message(self, user_id, role, content, timestamp):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO private_messages (user_id, role, content, timestamp)
                VALUES (?, ?, ?, ?)
            """,
                (user_id, role, content, timestamp),
            )
            conn.commit()

    def get_pending_private_messages(self, user_id, last_id, limit=50):
        """获取尚未总结的私聊消息流"""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, role, content, timestamp
                FROM private_messages
                WHERE user_id = ? AND id > ?
                ORDER BY id ASC
                LIMIT ?
            """,
                (user_id, last_id, limit),
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_recent_private_messages(self, user_id, limit=20):
        """获取最近的私聊记录流 (用于 AI 观察上下文，不涉及进度)"""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT role, content, timestamp
                FROM private_messages
                WHERE user_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            """,
                (user_id, limit),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            rows.reverse()
            return rows

    def clear_private_messages(self, user_id):
        """清空指定用户的私聊历史 (通常在总结完成后调用)"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM private_messages WHERE user_id = ?", (user_id,))
            conn.commit()

    def get_recent_combined_messages(self, user_id, limit=20):
        """获取最近的合并记录流 (包含私聊和特定用户在群聊中的互动)"""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # 合并私聊和群聊中涉及特定用户的消息
            # 对于群聊，我们需要统一格式：【用户】name: content 或 【你】name: content
            # 注意：group_messages 里的 content 已经是格式化好的了，如 "【用户】e.e.: ..."
            query = """
                SELECT role, content, timestamp FROM (
                    SELECT role, content, timestamp FROM private_messages WHERE user_id = ?
                    UNION ALL
                    SELECT (CASE WHEN sender_id = 'bot' THEN 'bot' ELSE 'user' END) as role, content, timestamp 
                    FROM group_messages 
                    WHERE is_specific_user = 1
                )
                ORDER BY timestamp DESC
                LIMIT ?
            """
            cursor.execute(query, (user_id, limit))
            rows = [dict(row) for row in cursor.fetchall()]
            rows.reverse()
            return rows

    # --- 全局配置相关操作 ---

    def get_plugin_meta(self, key, default=None):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM plugin_meta WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row[0] if row else default

    def update_plugin_meta(self, key, value):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO plugin_meta (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
            conn.commit()

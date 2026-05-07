import sqlite3
import json
import os
from datetime import datetime

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
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS group_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id TEXT,
                    msg_id_in_group INTEGER,
                    sender_id TEXT,
                    sender_name TEXT,
                    content TEXT,
                    timestamp REAL,
                    platform_msg_id TEXT
                )
            ''')
            # 2. 群元数据表 (存储进度和计数器)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS group_meta (
                    group_id TEXT PRIMARY KEY,
                    group_name TEXT,
                    last_summarized_id INTEGER DEFAULT 0,
                    message_id_counter INTEGER DEFAULT 0,
                    user_nickname TEXT,
                    bot_nickname TEXT
                )
            ''')
            # 3. 私聊消息表
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS private_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    sender_id TEXT,
                    timestamp REAL,
                    content TEXT,
                    bot_reply TEXT
                )
            ''')
            # 4. 插件全局元数据表 (用于存储主动消息时间等)
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS plugin_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            ''')
            
            # --- 热更新检查：补齐缺失字段 ---
            cursor.execute("PRAGMA table_info(group_meta)")
            columns = [column[1] for column in cursor.fetchall()]
            if "user_nickname" not in columns:
                try:
                    cursor.execute('ALTER TABLE group_meta ADD COLUMN user_nickname TEXT')
                    cursor.execute('ALTER TABLE group_meta ADD COLUMN bot_nickname TEXT')
                    logger.info("DailyReportAnalysisAPI: 数据库已成功升级，增加了昵称字段。")
                except:
                    pass

            conn.commit()

    # --- 群组相关操作 ---

    def get_group_meta(self, group_id):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT group_name, last_summarized_id, message_id_counter, user_nickname, bot_nickname FROM group_meta WHERE group_id = ?', (group_id,))
            return cursor.fetchone()

    def update_group_meta(self, group_id, group_name=None, last_summarized_id=None, message_id_counter=None, user_nickname=None, bot_nickname=None):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            # 先确保存在
            cursor.execute('INSERT OR IGNORE INTO group_meta (group_id, group_name) VALUES (?, ?)', (group_id, group_name or "未知群聊"))
            
            if group_name:
                cursor.execute('UPDATE group_meta SET group_name = ? WHERE group_id = ?', (group_name, group_id))
            if last_summarized_id is not None:
                cursor.execute('UPDATE group_meta SET last_summarized_id = ? WHERE group_id = ?', (last_summarized_id, group_id))
            if message_id_counter is not None:
                cursor.execute('UPDATE group_meta SET message_id_counter = ? WHERE group_id = ?', (message_id_counter, group_id))
            if user_nickname:
                cursor.execute('UPDATE group_meta SET user_nickname = ? WHERE group_id = ?', (user_nickname, group_id))
            if bot_nickname:
                cursor.execute('UPDATE group_meta SET bot_nickname = ? WHERE group_id = ?', (bot_nickname, group_id))
            conn.commit()

    def add_group_message(self, group_id, msg_id, sender_id, sender_name, content, timestamp, platform_msg_id):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO group_messages (group_id, msg_id_in_group, sender_id, sender_name, content, timestamp, platform_msg_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (group_id, msg_id, sender_id, sender_name, content, timestamp, str(platform_msg_id)))
            conn.commit()

    def get_pending_messages(self, group_id, last_id, limit=100):
        """获取尚未总结的消息"""
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT msg_id_in_group as id, sender_id, sender_name, content, timestamp 
                FROM group_messages 
                WHERE group_id = ? AND msg_id_in_group > ?
                ORDER BY msg_id_in_group ASC
                LIMIT ?
            ''', (group_id, last_id, limit))
            return [dict(row) for row in cursor.fetchall()]

    def clean_old_messages(self, days=7):
        """清理旧消息，防止数据库过大"""
        with self._get_conn() as conn:
            cursor = conn.cursor()
            limit_ts = datetime.now().timestamp() - (days * 86400)
            cursor.execute('DELETE FROM group_messages WHERE timestamp < ?', (limit_ts,))
            conn.commit()

    # --- 私聊相关操作 ---

    def add_private_message(self, sender_id, content, timestamp, bot_reply=""):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('''
                INSERT INTO private_messages (sender_id, content, timestamp, bot_reply)
                VALUES (?, ?, ?, ?)
            ''', (sender_id, content, timestamp, bot_reply))
            conn.commit()

    def get_recent_private_messages(self, sender_id, limit=20):
        with self._get_conn() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute('''
                SELECT content as 用户, bot_reply as 你的回复, timestamp
                FROM private_messages 
                WHERE sender_id = ?
                ORDER BY timestamp DESC
                LIMIT ?
            ''', (sender_id, limit))
            # 注意：LLM 习惯按时间顺序看
            rows = [dict(row) for row in cursor.fetchall()]
            rows.reverse()
            return rows

    # --- 全局配置相关操作 ---

    def get_plugin_meta(self, key, default=None):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('SELECT value FROM plugin_meta WHERE key = ?', (key,))
            row = cursor.fetchone()
            return row[0] if row else default

    def update_plugin_meta(self, key, value):
        with self._get_conn() as conn:
            cursor = conn.cursor()
            cursor.execute('INSERT OR REPLACE INTO plugin_meta (key, value) VALUES (?, ?)', (key, str(value)))
            conn.commit()

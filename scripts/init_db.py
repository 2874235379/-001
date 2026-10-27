"""初始化数据库：建库 + 建表（读取 sql/schema.sql）。

用法：
    python scripts/init_db.py
    python scripts/init_db.py --drop          # 危险：先删这三张表再重建
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402

DB_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")


def split_statements(sql_text: str):
    """按分号拆分 SQL（去掉 -- 行注释）。schema.sql 里没有含分号的字符串，够用。"""
    lines = []
    for line in sql_text.splitlines():
        if line.strip().startswith("--"):
            continue
        lines.append(line)
    body = "\n".join(lines)
    return [chunk.strip() for chunk in body.split(";") if chunk.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="FacePass 数据库初始化")
    parser.add_argument("--drop", action="store_true", help="删除已有三张表后重建（会清空数据）")
    args = parser.parse_args()

    settings = load_settings()
    db_name = settings.db_name
    if not DB_NAME_PATTERN.match(db_name):
        print(f"[错误] DB_NAME={db_name!r} 不合法，只允许字母/数字/下划线。")
        return 2

    schema_file = ROOT / "sql" / "schema.sql"
    if not schema_file.is_file():
        print(f"[错误] 找不到 {schema_file}")
        return 2

    print(f"连接 MySQL {settings.db_host}:{settings.db_port}（用户 {settings.db_user}）…")
    try:
        conn = pymysql.connect(
            host=settings.db_host,
            port=settings.db_port,
            user=settings.db_user,
            password=settings.db_password,
            charset=settings.db_charset,
            cursorclass=DictCursor,
            autocommit=True,
            connect_timeout=5,
        )
    except pymysql.MySQLError as exc:
        print(f"[错误] 无法连接 MySQL：{exc}")
        print("      请确认小皮面板中的 MySQL 已启动，并检查 .env 中的账号密码。")
        return 1

    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{db_name}` "
                "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            print(f"[OK] 数据库 `{db_name}` 已就绪（utf8mb4）")
        conn.select_db(db_name)

        if args.drop:
            with conn.cursor() as cur:
                cur.execute("SET FOREIGN_KEY_CHECKS = 0")
                for table in ("attendance_records", "face_encodings", "employees"):
                    cur.execute(f"DROP TABLE IF EXISTS `{table}`")
                    print(f"[WARN] 已删除表 {table}")
                cur.execute("SET FOREIGN_KEY_CHECKS = 1")

        statements = split_statements(schema_file.read_text(encoding="utf-8"))
        executed = 0
        with conn.cursor() as cur:
            for statement in statements:
                head = statement.lstrip().upper()
                if head.startswith("CREATE DATABASE") or head.startswith("USE "):
                    continue
                cur.execute(statement)
                executed += 1
        print(f"[OK] 已执行 {executed} 条建表 / 设置语句")

        with conn.cursor() as cur:
            cur.execute(
                "SELECT TABLE_NAME AS name, TABLE_ROWS AS rows_estimate "
                "FROM information_schema.TABLES WHERE TABLE_SCHEMA = %s ORDER BY TABLE_NAME",
                (db_name,),
            )
            rows = cur.fetchall() or []
            cur.execute("SELECT VERSION() AS v")
            version = (cur.fetchone() or {}).get("v", "")

        print(f"[OK] MySQL {version}，当前表：")
        for row in rows:
            print(f"     - {row['name']}（约 {row['rows_estimate']} 行）")
        print("\n初始化完成。下一步：")
        print("  1) 下载 dlib 模型到 models/（见 README）")
        print("  2) python scripts/check_env.py 自检")
        print("  3) python -m app.main 启动服务")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())

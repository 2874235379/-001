"""数据库集成测试（需要本机 MySQL 已启动且已执行 scripts/init_db.py）。

运行：python tests/test_db.py
测试数据全部以 __TEST__ 开头，结束后自动清理。
"""
from __future__ import annotations

import sys
from datetime import date, datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings, load_settings  # noqa: E402
from app.db import DatabaseError, MySQLPool  # noqa: E402

PASSED = []
FAILED = []
EMP_NO = "__TEST__01"
TEST_DATE = date(2020, 1, 6)


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def cleanup(pool: MySQLPool):
    with pool.transaction() as cur:
        cur.execute("DELETE FROM employees WHERE emp_no LIKE '__TEST__%%'")


def fake_vector(seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return rng.normal(0, 0.1, 128)


def main() -> int:
    settings = load_settings()
    pool = MySQLPool(settings)
    print("=" * 62)
    print("FacePass 数据库集成测试")
    print("=" * 62)
    print(f"目标：{settings.db_dsn}")

    health = pool.health()
    if not health["ok"]:
        print(f"\n[跳过] 数据库未就绪：{health['message']}")
        print(f"       {health.get('hint', '')}")
        pool.close()
        return 0

    check("MySQL 连接正常", health["ok"])
    check("三张表齐全", health["tables_ok"], str(health.get("missing_tables")))

    cleanup(pool)
    try:
        employee = pool.save_employee_with_encodings(
            EMP_NO, "测试员工", "质量保障", [fake_vector(1), fake_vector(2), fake_vector(3)]
        )
        check("员工写入成功", employee["samples"] == 3, str(employee))

        gallery = pool.fetch_gallery(force=True)
        rows = [g for g in gallery if g["emp_no"] == EMP_NO]
        check("特征库缓存读取 3 个样本", len(rows) == 3, f"rows={len(rows)}")
        check("特征向量维度为 128", all(r["vector"].shape == (128,) for r in rows))

        replaced = pool.save_employee_with_encodings(
            EMP_NO, "测试员工", "质量保障", [fake_vector(9)], replace=True
        )
        gallery = pool.fetch_gallery(force=True)
        rows = [g for g in gallery if g["emp_no"] == EMP_NO]
        check("重新录入会覆盖旧样本", replaced["samples"] == 1 and len(rows) == 1, f"rows={len(rows)}")

        emp = rows[0]
        first = pool.record_attendance(
            emp, datetime(2020, 1, 6, 8, 30), settings, 0.92, 0.31
        )
        check("08:30 首次打卡 -> 上班 / 正常", not first["duplicate"] and first["decision"]["status"] == "正常", str(first))
        check("记录时间正确", first["record"]["punch_time"] == datetime(2020, 1, 6, 8, 30))

        dup = pool.record_attendance(emp, datetime(2020, 1, 6, 8, 31), settings, 0.9, 0.3)
        check("2 分钟内重复识别 -> duplicate", dup["duplicate"] is True, str(dup))

        second = pool.record_attendance(emp, datetime(2020, 1, 6, 17, 0), settings, 0.88, 0.33)
        check(
            "17:00 二次打卡 -> 下班 / 早退",
            (not second["duplicate"]) and second["decision"]["punch_type"] == "下班"
            and second["decision"]["status"] == "早退",
            str(second),
        )

        records = pool.records_for_date(TEST_DATE)
        test_records = [r for r in records if r["emp_no"] == EMP_NO]
        check("当天共写入 2 条记录", len(test_records) == 2, f"count={len(test_records)}")
        check(
            "活体分数已落库",
            all(0 <= r["liveness_score"] <= 1 for r in test_records),
            str([r["liveness_score"] for r in test_records]),
        )

        overview = pool.overview(TEST_DATE)
        check("概览统计到该员工", overview["today_count"] == 1 and overview["employee_count"] >= 1, str(overview))

        # 错误密码 -> 明确的中文提示
        bad_settings = Settings(**{**settings.__dict__, "db_password": "definitely-wrong"})
        bad_pool = MySQLPool(bad_settings)
        bad_health = bad_pool.health()
        check(
            "密码错误时给出明确提示",
            not bad_health["ok"] and "密码" in bad_health["message"],
            str(bad_health),
        )
        bad_pool.close()
    except DatabaseError as exc:
        FAILED.append("数据库操作异常")
        print(f"\n[FAIL] {exc.message}\n       {exc.hint}")
    finally:
        cleanup(pool)
        pool.close()

    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("全部通过（测试数据已清理）")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())

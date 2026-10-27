"""UI 预览服务（仅用于没有 dlib 时调试前端动效）。

它用假引擎按固定节奏模拟「睁眼 -> 眨眼 -> 比对成功」，不参与任何真实识别。
真实打卡请使用：python -m app.main

运行：python tests/demo_server.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.attendance import decide_punch  # noqa: E402
from app.config import load_settings  # noqa: E402
from app.face_service import EngineStatus, FaceAnalysis  # noqa: E402
from app.main import create_app  # noqa: E402


class DemoEngine:
    EARS = [0.32, 0.31, 0.30, 0.10, 0.09, 0.33]

    def __init__(self):
        self.calls = 0

    def status(self, refresh=False):
        return EngineStatus(
            ready=True,
            dlib_available=True,
            detector="hog",
            message="预览模式（假引擎）",
            hint="此模式仅用于查看交互动效，不会进行真实人脸识别。",
            models={},
        )

    def analyze(self, rgb, embed="auto"):
        ear = self.EARS[self.calls % len(self.EARS)]
        self.calls += 1
        return FaceAnalysis(
            face_found=True,
            ear=ear,
            bbox=(0.24, 0.2, 0.4, 0.46),
            encoding=np.zeros(128) if ear > 0.28 else None,
            face_count=1,
        )

    def extract_single_encoding(self, rgb):
        return np.zeros(128)


class DemoDB:
    def __init__(self):
        self.records = []

    def health(self):
        return {
            "ok": True,
            "message": "预览模式（未连接数据库）",
            "hint": "",
            "version": "5.7.26",
            "database": "demo",
            "tables_ok": True,
            "missing_tables": [],
        }

    def fetch_gallery(self, force=False):
        return [
            {
                "employee_id": 1,
                "emp_no": "E001",
                "name": "张三",
                "department": "研发部",
                "vector": np.zeros(128),
            }
        ]

    def record_attendance(self, employee, now, settings, liveness_score, distance):
        last = self.records[-1] if self.records else None
        if last and (now - last["time"]).total_seconds() < settings.duplicate_window_seconds:
            return {
                "duplicate": True,
                "record": {
                    "id": last["id"],
                    "punch_time": last["time"],
                    "punch_type": last["punch_type"],
                    "status": last["status"],
                },
                "decision": None,
            }
        existing = {r["punch_type"] for r in self.records if r["time"].date() == now.date()}
        decision = decide_punch(
            existing,
            now,
            work_start=settings.work_start,
            work_end=settings.work_end,
            grace_minutes=settings.late_grace_minutes,
            auto_punch_type=settings.auto_punch_type,
            punch_split=settings.punch_split,
        )
        self.records.append(
            {
                "id": len(self.records) + 1,
                "time": now,
                "punch_type": decision.punch_type,
                "status": decision.status,
            }
        )
        return {
            "duplicate": False,
            "record": {
                "id": len(self.records),
                "punch_time": now,
                "punch_type": decision.punch_type,
                "status": decision.status,
            },
            "decision": decision.to_dict(),
        }

    def list_employees(self):
        return [
            {
                "id": 1,
                "emp_no": "E001",
                "name": "张三",
                "department": "研发部",
                "active": 1,
                "samples": 3,
                "created_at": "2026-10-25 09:00:00",
            }
        ]

    def overview(self, today):
        rows = [
            {
                "id": r["id"],
                "emp_no": "E001",
                "employee_name": "张三",
                "department": "研发部",
                "punch_type": r["punch_type"],
                "punch_time": r["time"].strftime("%Y-%m-%d %H:%M:%S"),
                "time": r["time"].strftime("%H:%M:%S"),
                "status": r["status"],
                "liveness_score": 0.95,
                "match_distance": 0.31,
            }
            for r in reversed(self.records)
        ]
        return {
            "employee_count": 1,
            "today_count": len({r["emp_no"] for r in rows}),
            "records": rows,
        }

    def records_for_date(self, day, limit=100):
        return []

    def close(self):
        return None


def main() -> int:
    settings = load_settings()
    settings.app_port = 8011
    settings.duplicate_window_seconds = 0  # 预览模式：每轮都走到成功态
    app = create_app(settings=settings, engine=DemoEngine(), db=DemoDB())
    print("UI 预览：http://127.0.0.1:8011 （Ctrl+C 退出）")
    uvicorn.run(app, host="127.0.0.1", port=8011, log_level="warning")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    sys.exit(main())

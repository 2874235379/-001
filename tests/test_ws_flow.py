"""WebSocket 端到端测试（不依赖 dlib / 真实数据库）。

用注入的 FakeEngine / FakeDB 驱动完整链路：
    前端帧 -> 后端解码 -> 活体状态机 -> 128 维比对 -> 防重复 -> 考勤判定 -> JSON 结果

运行：python tests/test_ws_flow.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import time
from datetime import time as dtime
from pathlib import Path

import numpy as np
import uvicorn
import websockets
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.attendance import decide_punch  # noqa: E402
from app.config import Settings  # noqa: E402
from app.face_service import EngineStatus, FaceAnalysis  # noqa: E402
from app.main import create_app  # noqa: E402

PORT = 8012
PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def jpeg_bytes() -> bytes:
    image = Image.new("RGB", (320, 240), (38, 44, 56))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


class FakeEngine:
    def __init__(self):
        self.ears = [0.32, 0.31, 0.30, 0.10, 0.09, 0.32]
        self.vector = np.zeros(128)
        self.calls = 0

    def status(self, refresh=False):
        return EngineStatus(
            ready=True,
            dlib_available=True,
            detector="hog",
            message="fake engine ready",
            models={},
        )

    def analyze(self, rgb, embed="auto"):
        index = min(self.calls, len(self.ears) - 1)
        self.calls += 1
        ear = self.ears[index]
        encoding = self.vector.copy() if ear > 0.28 else None
        return FaceAnalysis(
            face_found=True,
            ear=ear,
            bbox=(0.2, 0.18, 0.44, 0.5),
            encoding=encoding,
            face_count=1,
        )

    def extract_single_encoding(self, rgb):
        return self.vector.copy()

    def require_ready(self):
        return None


class FakeDB:
    def __init__(self):
        self.records = []
        self.gallery = [
            {
                "employee_id": 1,
                "emp_no": "E001",
                "name": "张三",
                "department": "研发部",
                "vector": np.zeros(128),
            }
        ]

    def health(self):
        return {
            "ok": True,
            "message": "ok",
            "hint": "",
            "version": "5.7.26",
            "database": "demo",
            "tables_ok": True,
            "missing_tables": [],
        }

    def fetch_gallery(self, force=False):
        return self.gallery

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
        record = {
            "id": len(self.records) + 1,
            "time": now,
            "punch_type": decision.punch_type,
            "status": decision.status,
        }
        self.records.append(record)
        return {
            "duplicate": False,
            "record": {
                "id": record["id"],
                "punch_time": now,
                "punch_type": decision.punch_type,
                "status": decision.status,
            },
            "decision": decision.to_dict(),
        }

    def overview(self, today):
        return {"employee_count": 1, "today_count": len(self.records), "records": []}

    def records_for_date(self, day, limit=100):
        return []

    def close(self):
        return None


def start_server(app):
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(120):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("测试服务器启动超时")


async def recv(ws, timeout=6):
    raw = await asyncio.wait_for(ws.recv(), timeout)
    return json.loads(raw)


async def drive_until_result(ws, max_frames=12, max_seconds=6):
    deadline = time.monotonic() + max_seconds
    states = []
    while time.monotonic() < deadline:
        for _ in range(max_frames):
            await ws.send(jpeg_bytes())
            msg = await recv(ws)
            if msg["type"] == "state":
                states.append(msg.get("state"))
            elif msg["type"] == "result":
                return msg, states
            elif msg["type"] == "error":
                return msg, states
    raise AssertionError("未在预期时间内收到结果")


async def scenario(ws, engine):
    hello = await recv(ws)
    check("收到 hello 帧", hello.get("type") == "hello")
    check("引擎状态为就绪", hello.get("engine", {}).get("ready") is True)

    await ws.send(json.dumps({"type": "reset"}))
    reset_ack = await recv(ws)
    check("reset 有回执", reset_ack.get("type") == "reset_ok")

    result, states = await drive_until_result(ws)
    check("完整走完扫描 -> 眨眼 -> 比对", "blink" in states or "scanning" in states, str(states))
    check("返回 success", result.get("code") == "success", json.dumps(result, ensure_ascii=False))
    if result.get("code") == "success":
        check("姓名正确", result["employee"]["name"] == "张三")
        check("打卡类型为上班", result["punch_type"] == "上班")
        check("状态为正常或迟到", result["status"] in ("正常", "迟到"))
        check("活体分数 > 0", result["liveness_score"] > 0)
        check("匹配距离为 0", result["distance"] == 0.0)

    await ws.send(json.dumps({"type": "reset"}))
    await recv(ws)
    engine.calls = 0
    duplicate, _ = await drive_until_result(ws)
    check("短时间内二次识别 -> duplicate", duplicate.get("code") == "duplicate", str(duplicate))
    check("重复时给出上次时间", bool(duplicate.get("last", {}).get("time")))

    await ws.send(json.dumps({"type": "reset"}))
    await recv(ws)
    await ws.send(jpeg_bytes())
    msg = await recv(ws)
    check("首帧就有状态反馈", msg.get("type") in ("state", "result"))


def main() -> int:
    print("=" * 62)
    print("FacePass WebSocket 端到端测试（FakeEngine / FakeDB）")
    print("=" * 62)

    settings = Settings(
        app_port=PORT,
        work_start=dtime(9, 0),
        work_end=dtime(18, 0),
        liveness_timeout_seconds=3.0,
        duplicate_window_seconds=120,
    )
    engine = FakeEngine()
    db = FakeDB()
    app = create_app(settings=settings, engine=engine, db=db)
    server = start_server(app)

    try:
        async def main_scenario():
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT}/ws/checkin", max_size=4 * 1024 * 1024
            ) as ws:
                await scenario(ws, engine)

        asyncio.run(main_scenario())

        # 陌生人：特征与库中距离极大 -> 未识别
        async def unknown_scenario():
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT}/ws/checkin", max_size=4 * 1024 * 1024
            ) as ws:
                await recv(ws)
                engine.calls = 0
                engine.vector = np.full(128, 5.0)
                result, _ = await drive_until_result(ws)
                check("陌生人 -> unknown", result.get("code") == "unknown", str(result))
                check("unknown 返回最近距离", isinstance(result.get("distance"), (int, float)))

        asyncio.run(unknown_scenario())

        # 一直睁眼 -> 活体超时
        settings_fast = Settings(app_port=PORT + 1, liveness_timeout_seconds=1.0)
        engine2 = FakeEngine()
        engine2.ears = [0.32]
        app2 = create_app(settings=settings_fast, engine=engine2, db=FakeDB())
        server2 = uvicorn.Server(uvicorn.Config(app2, host="127.0.0.1", port=PORT + 1, log_level="error"))
        threading.Thread(target=server2.run, daemon=True).start()
        for _ in range(120):
            if server2.started:
                break
            time.sleep(0.05)

        async def liveness_scenario():
            async with websockets.connect(
                f"ws://127.0.0.1:{PORT + 1}/ws/checkin", max_size=4 * 1024 * 1024
            ) as ws:
                await recv(ws)
                result, _ = await drive_until_result(ws, max_frames=200, max_seconds=6)
                check(
                    "不眨眼 -> liveness_failed",
                    result.get("code") == "liveness_failed",
                    str(result),
                )
                check(
                    "文案明确要求眨眼",
                    "眨眼" in (result.get("message") or ""),
                    result.get("message", ""),
                )

        asyncio.run(liveness_scenario())
        server2.should_exit = True
    finally:
        server.should_exit = True
        time.sleep(0.4)

    print("\n" + "=" * 62)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())

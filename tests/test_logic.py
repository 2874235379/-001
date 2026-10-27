"""纯逻辑单测（不需要 dlib / MySQL）。

运行：python tests/test_logic.py
"""
from __future__ import annotations

import sys
from datetime import datetime, time as dtime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.attendance import decide_punch  # noqa: E402
from app.checkin import (  # noqa: E402
    PHASE_AWAIT_OPEN,
    PHASE_BLINK,
    PHASE_DONE,
    PHASE_NO_FACE,
    PHASE_READY,
    PHASE_SCANNING,
    PHASE_TIMEOUT,
    REASON_NO_BLINK,
    REASON_NO_FACE,
    CheckinSession,
)
from app.db import blob_to_vector, vector_to_blob  # noqa: E402
from app.face_service import ear_from_landmarks, eye_aspect_ratio, match_face  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def landmarks(open_eye=True):
    pts = np.zeros((68, 2), dtype=np.float64)
    dy = 1.0 if open_eye else 0.1
    base = [(0, 0), (1, dy), (2, dy), (3, 0), (2, -dy), (1, -dy)]
    for offset, (x, y) in zip(range(36, 42), base):
        pts[offset] = (x, y)
    for offset, (x, y) in zip(range(42, 48), base):
        pts[offset] = (x + 10, y)
    return pts


class FakeClock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


def feed(session, clock, ear, step=0.15):
    clock.advance(step)
    return session.observe(True, ear)


def test_ear():
    print("\n[EAR 计算]")
    open_ear = eye_aspect_ratio(landmarks(True)[36:42])
    closed_ear = eye_aspect_ratio(landmarks(False)[36:42])
    check("睁眼 EAR > 0.25", open_ear > 0.25, f"ear={open_ear:.3f}")
    check("闭眼 EAR < 0.25", closed_ear < 0.25, f"ear={closed_ear:.3f}")
    check("双眼平均与单眼一致", abs(ear_from_landmarks(landmarks(True)) - open_ear) < 1e-9)
    check("水平距离为 0 时安全返回 0", eye_aspect_ratio(np.zeros((6, 2))) == 0.0)


def test_session_happy_path():
    print("\n[活体状态机 · 正常眨眼]")
    clock = FakeClock()
    session = CheckinSession(ear_threshold=0.25, consecutive_frames=2, timeout_seconds=9, clock=clock)
    check("无人脸 -> no_face", session.observe(False, None).phase == PHASE_NO_FACE)

    check("第 1 帧先校准 -> scanning", feed(session, clock, 0.32).phase == PHASE_SCANNING)
    feed(session, clock, 0.30)
    check("校准未完成前不判眨眼", not session.calibrated)
    check("第 3 帧完成基线校准", feed(session, clock, 0.31).phase == PHASE_SCANNING and session.calibrated)
    check("阈值不超过 0.25", session.threshold <= 0.25, f"threshold={session.threshold:.3f}")

    obs = feed(session, clock, 0.12)
    check("闭眼第 1 帧不判眨眼", obs.phase == PHASE_SCANNING and not obs.blink)
    obs = feed(session, clock, 0.09)
    check("连续 2 帧闭眼 -> blink", obs.phase == PHASE_BLINK and obs.blink)
    check("仍闭眼 -> await_open", feed(session, clock, 0.11).phase == PHASE_AWAIT_OPEN)
    check("重新睁眼 -> ready", feed(session, clock, 0.31).phase == PHASE_READY)
    check("活体分数在 0~1 之间", 0.5 < session.liveness_score <= 1.0, f"score={session.liveness_score:.2f}")

    session.finish()
    check("结束态不再重复出结果", session.observe(True, 0.3).phase == PHASE_DONE)


def test_session_narrow_eyes():
    print("\n[活体状态机 · 窄眼型自适应]")
    clock = FakeClock()
    session = CheckinSession(ear_threshold=0.25, consecutive_frames=2, timeout_seconds=9, clock=clock)
    for _ in range(3):
        feed(session, clock, 0.19)
    check("窄眼型也能完成校准", session.calibrated and session.baseline is not None)
    check("自适应阈值低于 0.25", session.threshold < 0.25, f"threshold={session.threshold:.3f}")

    obs = feed(session, clock, 0.19)
    check("睁眼 0.19 不被误判为闭眼", not obs.blink and obs.phase == PHASE_SCANNING)
    feed(session, clock, 0.08)
    check("真正闭眼仍能触发眨眼", feed(session, clock, 0.07).phase == PHASE_BLINK)
    check("重新睁眼 -> ready", feed(session, clock, 0.19).phase == PHASE_READY)
    check("窄眼型活体分数有效", session.liveness_score > 0.5, f"score={session.liveness_score:.2f}")


def test_session_timeouts():
    print("\n[活体状态机 · 超时与中断]")
    clock = FakeClock()
    session = CheckinSession(timeout_seconds=5, clock=clock)
    feed(session, clock, 0.32)
    clock.advance(6)
    obs = session.observe(True, 0.32)
    check("不眨眼超时 -> timeout(no_blink)", obs.phase == PHASE_TIMEOUT and obs.reason == REASON_NO_BLINK)

    clock2 = FakeClock()
    session2 = CheckinSession(timeout_seconds=5, clock=clock2)
    session2.observe(False, None)
    clock2.advance(6)
    obs = session2.observe(False, None)
    check("始终无人脸 -> timeout(no_face)", obs.phase == PHASE_TIMEOUT and obs.reason == REASON_NO_FACE)

    clock3 = FakeClock()
    session3 = CheckinSession(timeout_seconds=5, clock=clock3)
    for ear in (0.32, 0.31, 0.30):
        feed(session3, clock3, ear)
    feed(session3, clock3, 0.10)
    feed(session3, clock3, 0.09)
    clock3.advance(8)
    obs = session3.observe(False, None)
    check("眨眼后人脸丢失 -> 中断超时", obs.phase == PHASE_TIMEOUT)


def test_attendance():
    print("\n[考勤状态判定]")
    base = dict(work_start=dtime(9, 0), work_end=dtime(18, 0), grace_minutes=0)

    d = decide_punch([], datetime(2026, 10, 25, 8, 30), **base)
    check("08:30 首次 -> 上班/正常", d.punch_type == "上班" and d.status == "正常", str(d))

    d = decide_punch([], datetime(2026, 10, 25, 9, 5), **base)
    check("09:05 首次 -> 上班/迟到", d.punch_type == "上班" and d.status == "迟到", str(d))

    d = decide_punch(
        [],
        datetime(2026, 10, 25, 9, 5),
        work_start=dtime(9, 0),
        work_end=dtime(18, 0),
        grace_minutes=10,
    )
    check("宽限 10 分钟时 09:05 不算迟到", d.status == "正常", str(d))

    d = decide_punch(["上班"], datetime(2026, 10, 25, 13, 0), **base)
    check("当天已有上班 -> 下班/早退", d.punch_type == "下班" and d.status == "早退", str(d))

    d = decide_punch(["上班"], datetime(2026, 10, 25, 18, 30), **base)
    check("18:30 下班 -> 下班/正常", d.punch_type == "下班" and d.status == "正常", str(d))

    d = decide_punch(["上班", "下班"], datetime(2026, 10, 25, 19, 30), **base)
    check("第三次打卡 -> 仍记下班", d.punch_type == "下班", str(d))

    d = decide_punch(
        [],
        datetime(2026, 10, 25, 13, 0),
        work_start=dtime(9, 0),
        work_end=dtime(18, 0),
        auto_punch_type=False,
        punch_split=dtime(12, 0),
    )
    check("固定时间段模式：13:00 -> 下班", d.punch_type == "下班", str(d))


def test_matching():
    print("\n[128 维特征比对]")
    gallery = [
        {"employee_id": 1, "emp_no": "E001", "name": "张三", "department": "研发", "vector": np.zeros(128)},
        {"employee_id": 2, "emp_no": "E002", "name": "李四", "department": "设计", "vector": np.ones(128)},
    ]
    outcome = match_face(np.zeros(128), gallery, 0.45)
    check("同人距离 0 -> 命中", outcome is not None and outcome.accepted and outcome.distance == 0.0)

    near = np.full(128, 0.005)
    outcome = match_face(near, gallery, 0.45)
    check("轻微噪声（0.057）-> 命中 E001", outcome.accepted and outcome.employee["emp_no"] == "E001")

    far = np.full(128, 0.05)
    outcome = match_face(far, gallery, 0.45)
    check("距离 0.566 超阈值 -> 不通过", outcome is not None and not outcome.accepted)

    vector = np.zeros(128)
    vector[0] = 0.44
    check("距离 0.44 < 0.45 -> 命中", match_face(vector, gallery[:1], 0.45).accepted)

    vector[0] = 0.46
    check("距离 0.46 > 0.45 -> 拒绝", not match_face(vector, gallery[:1], 0.45).accepted)

    multi = gallery + [
        {"employee_id": 1, "emp_no": "E001", "name": "张三", "department": "研发", "vector": np.full(128, 9.0)},
    ]
    outcome = match_face(np.zeros(128), multi, 0.45)
    check("同一员工多样本取最小距离", outcome.distance == 0.0 and outcome.employee["employee_id"] == 1)

    check("空库返回 None", match_face(np.zeros(128), [], 0.45) is None)


def test_blob():
    print("\n[特征向量 BLOB 编解码]")
    vector = np.linspace(-0.5, 0.5, 128)
    blob = vector_to_blob(vector)
    check("float32 128 维 = 512 字节", len(blob) == 512, f"len={len(blob)}")
    restored = blob_to_vector(blob, 128)
    check("编解码误差 < 1e-6", np.max(np.abs(restored - vector)) < 1e-6)


def main():
    print("=" * 62)
    print("FacePass 纯逻辑测试")
    print("=" * 62)
    test_ear()
    test_session_happy_path()
    test_session_narrow_eyes()
    test_session_timeouts()
    test_attendance()
    test_matching()
    test_blob()
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

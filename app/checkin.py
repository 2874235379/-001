"""活体检测状态机（纯逻辑，不依赖 dlib / 数据库，方便单测）。

判定链路：
    检测到人脸 -> 记录睁眼 EAR 基线 -> 连续 N 帧 EAR < 阈值（默认 0.25）-> 判定眨眼
    -> 等眼睛重新睁开 -> 允许提取特征并比对

任何一步超时都会返回 timeout，由上层翻译成「请眨眼，活体检测失败」之类的文案，
保证前端永远不会停在无反馈状态。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

PHASE_NO_FACE = "no_face"
PHASE_SCANNING = "scanning"
PHASE_BLINK = "blink"
PHASE_AWAIT_OPEN = "await_open"
PHASE_READY = "ready"
PHASE_TIMEOUT = "timeout"
PHASE_DONE = "done"

REASON_NO_BLINK = "no_blink"
REASON_NO_FACE = "no_face"
REASON_FACE_LOST = "face_lost"


@dataclass
class Observation:
    phase: str
    blink: bool
    progress: float
    ear: Optional[float]
    hint: str
    reason: str = ""
    face_found: bool = False


class CheckinSession:
    """一次打卡会话（从开始扫描到出结果）。"""

    def __init__(
        self,
        *,
        ear_threshold: float = 0.25,
        consecutive_frames: int = 2,
        timeout_seconds: float = 9.0,
        clock=time.monotonic,
    ):
        self.ear_threshold = float(ear_threshold)
        self.consecutive_frames = max(1, int(consecutive_frames))
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._clock = clock
        self.reset()

    def reset(self) -> None:
        now = self._clock()
        self.started_at = now
        self.last_face_at: Optional[float] = None
        self.baseline = None
        self.calibration = []
        self.calibrated = False
        self.closed_run = 0
        self.blink_seen = False
        self.blink_at: Optional[float] = None
        self.min_ear = 1.0
        self.frames = 0
        self.done = False

    # ------------------------------------------------------------------ 属性
    @property
    def threshold(self) -> float:
        """实际的闭眼阈值。

        规范默认 0.25，但不同人眼型差异很大（实测有人睁眼 EAR 只有 0.19）。
        前几帧会先校准个人睁眼基线，再取 min(0.25, 基线 × 0.72)，
        既保留 0.25 的上限，也避免把眼型偏小的人一直判成「闭眼」。
        """
        if not self.calibrated or not self.baseline:
            return self.ear_threshold
        return min(self.ear_threshold, max(0.14, self.baseline * 0.72))

    @property
    def liveness_score(self) -> float:
        """0~1 的活体分数：眨眼时 EAR 相对基线下降越多，分数越高。"""
        if not self.blink_seen or not self.baseline:
            return 0.0
        drop = (self.baseline - self.min_ear) / max(self.baseline, 0.05)
        return max(0.0, min(1.0, drop / 0.55))

    @property
    def elapsed(self) -> float:
        return max(0.0, self._clock() - self.started_at)

    # ------------------------------------------------------------------ 核心
    def observe(self, face_found: bool, ear: Optional[float]) -> Observation:
        if self.done:
            return Observation(PHASE_DONE, self.blink_seen, 1.0, ear, "", "", face_found)

        self.frames += 1
        elapsed = self.elapsed
        progress = min(1.0, elapsed / self.timeout_seconds)

        if not face_found or ear is None:
            self.closed_run = 0
            if self.blink_seen:
                if elapsed > self.timeout_seconds + 1.5:
                    return self._timeout(progress, ear, REASON_FACE_LOST)
                return Observation(
                    PHASE_AWAIT_OPEN,
                    True,
                    progress,
                    ear,
                    "请重新正对摄像头，保持面部在框内",
                    face_found=False,
                )
            if elapsed >= self.timeout_seconds:
                return self._timeout(progress, ear, REASON_NO_FACE)
            return Observation(
                PHASE_NO_FACE,
                False,
                progress,
                ear,
                "请将面部移入识别框",
                face_found=False,
            )

        self.last_face_at = self._clock()

        if not self.blink_seen:
            # 1) 先用前几帧校准个人睁眼基线（不参与眨眼判定）
            if not self.calibrated:
                self.calibration.append(float(ear))
                if len(self.calibration) >= 3 and max(self.calibration) > 0.15:
                    self.baseline = max(self.calibration)
                    self.calibrated = True
                elif len(self.calibration) >= 8:
                    # 8 帧都没见过明显睁眼，按最大观测值兜底，后续靠超时拦截
                    self.baseline = max(self.calibration) or self.ear_threshold
                    self.calibrated = True
                if elapsed >= self.timeout_seconds:
                    return self._timeout(progress, ear, REASON_NO_BLINK)
                return Observation(
                    PHASE_SCANNING,
                    False,
                    progress,
                    ear,
                    "正在校准…请保持正对摄像头",
                    face_found=True,
                )

            threshold = self.threshold

            # 2) 睁眼时缓慢更新基线（只升不降太多，避免越眨越低）
            if ear >= threshold * 1.3:
                self.baseline = 0.9 * float(self.baseline or ear) + 0.1 * float(ear)
                self.closed_run = 0
            else:
                self.closed_run += 1
                self.min_ear = min(self.min_ear, float(ear))
                if self.closed_run >= self.consecutive_frames:
                    self.blink_seen = True
                    self.blink_at = self._clock()
                    return Observation(
                        PHASE_BLINK,
                        True,
                        progress,
                        ear,
                        "已检测到眨眼，正在比对…",
                        face_found=True,
                    )

            if elapsed >= self.timeout_seconds:
                return self._timeout(progress, ear, REASON_NO_BLINK)
            hint = "保持正对摄像头，自然眨一次眼"
            if ear < threshold and self.closed_run < self.consecutive_frames:
                hint = "检测到闭眼…"
            return Observation(
                PHASE_SCANNING, False, progress, ear, hint, face_found=True
            )

        # 已确认眨眼，等眼睛睁开后再取特征（阈值只比闭眼高 10%）
        if ear > self.threshold * 1.1:
            return Observation(
                PHASE_READY, True, min(1.0, progress), ear, "正在比对身份…", face_found=True
            )
        if elapsed > self.timeout_seconds + 2.0:
            return self._timeout(progress, ear, REASON_FACE_LOST)
        return Observation(
            PHASE_AWAIT_OPEN, True, progress, ear, "请睁开眼睛…", face_found=True
        )

    def _timeout(self, progress: float, ear: Optional[float], reason: str) -> Observation:
        hints = {
            REASON_NO_BLINK: "请眨眼，活体检测失败",
            REASON_NO_FACE: "未能检测到人脸，请正对摄像头后重试",
            REASON_FACE_LOST: "活体检测中断，请保持面部在画面中重试",
        }
        return Observation(
            PHASE_TIMEOUT,
            self.blink_seen,
            max(progress, 1.0),
            ear,
            hints.get(reason, "活体检测失败"),
            reason=reason,
        )

    def finish(self) -> None:
        self.done = True

    def snapshot(self) -> dict:
        """调试信息（写日志用，不回传给前端）。"""
        return {
            "frames": self.frames,
            "elapsed": round(self.elapsed, 2),
            "blink_seen": self.blink_seen,
            "min_ear": round(self.min_ear, 4),
            "baseline": round(self.baseline, 4) if self.baseline else None,
            "threshold": round(self.threshold, 4),
            "liveness_score": round(self.liveness_score, 4),
        }

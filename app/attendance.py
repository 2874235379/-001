"""考勤规则（纯函数，无副作用，便于单元测试）。

规则说明
--------
打卡类型：
  * AUTO_PUNCH_TYPE=true（默认）：当天第一条记录 = 上班，之后 = 下班。
    这样即使中午才到公司，第一条也会被判定为「上班 / 迟到」，而不是误判成早退。
  * AUTO_PUNCH_TYPE=false：按 PUNCH_SPLIT（默认 12:00）划分上/下班。

状态：
  * 上班：晚于 WORK_START + LATE_GRACE_MINUTES -> 迟到，否则正常。
  * 下班：早于 WORK_END -> 早退，否则正常。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from typing import Iterable, Optional

PUNCH_IN = "上班"
PUNCH_OUT = "下班"

STATUS_NORMAL = "正常"
STATUS_LATE = "迟到"
STATUS_EARLY = "早退"


@dataclass(frozen=True)
class PunchDecision:
    punch_type: str
    status: str
    note: str

    def to_dict(self) -> dict:
        return {"punch_type": self.punch_type, "status": self.status, "note": self.note}


def decide_punch(
    existing_types: Iterable[str],
    now: datetime,
    *,
    work_start: dtime,
    work_end: dtime,
    grace_minutes: int = 0,
    auto_punch_type: bool = True,
    punch_split: Optional[dtime] = None,
) -> PunchDecision:
    """根据当天已有打卡类型与当前时间，判定本次打卡类型和状态。"""
    types = {t for t in existing_types if t}

    if auto_punch_type:
        punch_type = PUNCH_IN if PUNCH_IN not in types else PUNCH_OUT
    else:
        split = punch_split or dtime(12, 0)
        punch_type = PUNCH_IN if now.time() < split else PUNCH_OUT

    if punch_type == PUNCH_IN:
        deadline = datetime.combine(now.date(), work_start) + timedelta(minutes=max(0, grace_minutes))
        if now > deadline:
            minutes = int((now - datetime.combine(now.date(), work_start)).total_seconds() // 60)
            note = f"上班时间 {work_start.strftime('%H:%M')}，实际 {now.strftime('%H:%M')}，迟到 {minutes} 分钟"
            if grace_minutes:
                note += f"（宽限 {grace_minutes} 分钟）"
            return PunchDecision(PUNCH_IN, STATUS_LATE, note)
        note = f"上班时间 {work_start.strftime('%H:%M')}，打卡 {now.strftime('%H:%M')}"
        if grace_minutes:
            note += f"（宽限 {grace_minutes} 分钟）"
        return PunchDecision(PUNCH_IN, STATUS_NORMAL, note)

    end_dt = datetime.combine(now.date(), work_end)
    if now < end_dt:
        minutes = int((end_dt - now).total_seconds() // 60)
        return PunchDecision(
            PUNCH_OUT,
            STATUS_EARLY,
            f"下班时间 {work_end.strftime('%H:%M')}，实际 {now.strftime('%H:%M')}，提前 {minutes} 分钟",
        )
    return PunchDecision(
        PUNCH_OUT,
        STATUS_NORMAL,
        f"下班时间 {work_end.strftime('%H:%M')}，打卡 {now.strftime('%H:%M')}",
    )

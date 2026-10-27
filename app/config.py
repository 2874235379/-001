"""集中配置。

所有可变项都通过环境变量或项目根目录的 .env 文件注入，代码里不写死任何
本机路径 / 密码。读取顺序：真实环境变量 > .env > 内置默认值。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import time as dtime
from pathlib import Path
from typing import List, Optional

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_FILE = BASE_DIR / ".env"


class ConfigError(RuntimeError):
    """配置错误：启动阶段直接抛出，附带可执行的修复建议。"""


def _load_env_file(path: Path) -> None:
    """极简 .env 解析（不引入 python-dotenv 依赖）。"""
    if not path.is_file():
        return
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        content = path.read_text(encoding="gbk", errors="replace")

    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith(";"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        # 真实环境变量优先，.env 只做兜底
        os.environ.setdefault(key, value)


def _env_str(key: str, default: str) -> str:
    value = os.environ.get(key)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(key: str, default: int) -> int:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw.strip())
    except ValueError:
        return default


def _env_bool(key: str, default: bool) -> bool:
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_opt_path(key: str):
    """可选路径：空值返回 None。"""
    raw = os.environ.get(key, "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _env_path(key: str, default: str) -> Path:
    raw = _env_str(key, default)
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


def _env_time(key: str, default: str, warnings: List[str]) -> dtime:
    raw = _env_str(key, default)
    try:
        hour, minute = raw.split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, TypeError):
        warnings.append(f"{key}={raw!r} 不是合法的 HH:MM 时间，已回退为 {default}")
        hour, minute = default.split(":")
        return dtime(int(hour), int(minute))


@dataclass
class Settings:
    """运行时配置。字段全部有默认值，便于测试时局部覆盖。"""

    base_dir: Path = BASE_DIR
    static_dir: Path = BASE_DIR / "app" / "static"
    models_dir: Path = BASE_DIR / "models"

    app_host: str = "127.0.0.1"
    app_port: int = 8010

    # 局域网 HTTPS（手机浏览器调用摄像头必须 https）
    https_port: int = 8443
    ssl_certfile: Optional[Path] = None
    ssl_keyfile: Optional[Path] = None

    db_host: str = "127.0.0.1"
    db_port: int = 3306
    db_user: str = "root"
    db_password: str = "root"
    db_name: str = "face_attendance"
    db_charset: str = "utf8mb4"
    db_pool_size: int = 4

    # --- dlib ---
    shape_predictor_path: Path = BASE_DIR / "models" / "shape_predictor_68_face_landmarks.dat"
    face_recognition_model_path: Path = (
        BASE_DIR / "models" / "dlib_face_recognition_resnet_model_v1.dat"
    )
    cnn_detector_path: Optional[Path] = None
    face_detector: str = "hog"  # hog | cnn

    # --- 算法阈值 ---
    match_threshold: float = 0.45
    ear_threshold: float = 0.25
    ear_consecutive_frames: int = 2
    liveness_timeout_seconds: float = 9.0

    # --- 帧参数 ---
    frame_max_width: int = 480
    frame_max_bytes: int = 3 * 1024 * 1024

    # --- 考勤规则 ---
    work_start: dtime = dtime(9, 0)
    work_end: dtime = dtime(18, 0)
    late_grace_minutes: int = 0
    duplicate_window_seconds: int = 120
    auto_punch_type: bool = True
    punch_split: dtime = dtime(12, 0)

    warnings: List[str] = field(default_factory=list)

    # ---- 派生属性 ----
    @property
    def db_dsn(self) -> str:
        """用于日志展示的 DSN（不含密码）。"""
        return f"mysql://{self.db_user}@{self.db_host}:{self.db_port}/{self.db_name}"

    @property
    def ssl_enabled(self) -> bool:
        return bool(self.ssl_certfile and self.ssl_keyfile)

    @property
    def work_start_text(self) -> str:
        return self.work_start.strftime("%H:%M")

    @property
    def work_end_text(self) -> str:
        return self.work_end.strftime("%H:%M")

    def public_dict(self) -> dict:
        """可以安全暴露给前端的配置（不含密码 / 路径）。"""
        return {
            "work_start": self.work_start_text,
            "work_end": self.work_end_text,
            "late_grace_minutes": self.late_grace_minutes,
            "duplicate_window_seconds": self.duplicate_window_seconds,
            "match_threshold": self.match_threshold,
            "ear_threshold": self.ear_threshold,
            "liveness_timeout_seconds": self.liveness_timeout_seconds,
            "frame_max_width": self.frame_max_width,
            "db_name": self.db_name,
            "https_port": self.https_port if self.ssl_enabled else None,
        }


def load_settings(env_file: Optional[Path] = None) -> Settings:
    """读取 .env + 环境变量，产出 Settings。"""
    _load_env_file(env_file or ENV_FILE)

    warnings: List[str] = []
    models_dir = _env_path("MODELS_DIR", "models")

    shape_path = _env_path(
        "DLIB_SHAPE_PREDICTOR", str(models_dir / "shape_predictor_68_face_landmarks.dat")
    )
    recog_path = _env_path(
        "DLIB_FACE_RECOGNITION_MODEL",
        str(models_dir / "dlib_face_recognition_resnet_model_v1.dat"),
    )

    cnn_raw = _env_str("DLIB_CNN_DETECTOR", "")
    cnn_path: Optional[Path] = None
    if cnn_raw:
        cnn_path = Path(cnn_raw).expanduser()
        if not cnn_path.is_absolute():
            cnn_path = BASE_DIR / cnn_path

    detector = _env_str("FACE_DETECTOR", "hog").lower()
    if detector not in {"hog", "cnn"}:
        warnings.append(f"FACE_DETECTOR={detector!r} 不支持，已回退为 hog")
        detector = "hog"

    settings = Settings(
        static_dir=_env_path("STATIC_DIR", "app/static"),
        models_dir=models_dir,
        app_host=_env_str("APP_HOST", "127.0.0.1"),
        app_port=_env_int("APP_PORT", 8010),
        https_port=_env_int("HTTPS_PORT", 8443),
        ssl_certfile=_env_opt_path("SSL_CERTFILE"),
        ssl_keyfile=_env_opt_path("SSL_KEYFILE"),
        db_host=_env_str("DB_HOST", "127.0.0.1"),
        db_port=_env_int("DB_PORT", 3306),
        db_user=_env_str("DB_USER", "root"),
        db_password=os.environ.get("DB_PASSWORD", "root"),
        db_name=_env_str("DB_NAME", "face_attendance"),
        db_charset=_env_str("DB_CHARSET", "utf8mb4"),
        db_pool_size=max(1, _env_int("DB_POOL_SIZE", 4)),
        shape_predictor_path=shape_path,
        face_recognition_model_path=recog_path,
        cnn_detector_path=cnn_path,
        face_detector=detector,
        match_threshold=_env_float("MATCH_THRESHOLD", 0.45),
        ear_threshold=_env_float("EAR_THRESHOLD", 0.25),
        ear_consecutive_frames=max(1, _env_int("EAR_CONSECUTIVE_FRAMES", 2)),
        liveness_timeout_seconds=_env_float("LIVENESS_TIMEOUT_SECONDS", 9.0),
        frame_max_width=_env_int("FRAME_MAX_WIDTH", 480),
        frame_max_bytes=_env_int("FRAME_MAX_BYTES", 3 * 1024 * 1024),
        late_grace_minutes=_env_int("LATE_GRACE_MINUTES", 0),
        duplicate_window_seconds=_env_int("DUPLICATE_WINDOW_SECONDS", 120),
        auto_punch_type=_env_bool("AUTO_PUNCH_TYPE", True),
        warnings=warnings,
    )
    settings.work_start = _env_time("WORK_START", "09:00", warnings)
    settings.work_end = _env_time("WORK_END", "18:00", warnings)
    settings.punch_split = _env_time("PUNCH_SPLIT", "12:00", warnings)

    if settings.match_threshold <= 0 or settings.match_threshold > 1.2:
        warnings.append("MATCH_THRESHOLD 超出合理范围（0~1.2），已回退为 0.45")
        settings.match_threshold = 0.45
    if settings.ear_threshold <= 0.05 or settings.ear_threshold >= 0.5:
        warnings.append("EAR_THRESHOLD 超出合理范围（0.05~0.5），已回退为 0.25")
        settings.ear_threshold = 0.25
    if settings.work_end <= settings.work_start:
        warnings.append("WORK_END 早于 WORK_START，早退判定可能不符合预期")

    return settings

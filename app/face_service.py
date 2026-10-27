"""dlib 封装：人脸检测 / 68 点关键点 / 128 维特征 / EAR 活体判定。

关键设计：
* dlib 采用「延迟导入 + 延迟加载」，缺少 dlib 或模型文件时服务依然能启动，
  并通过 FaceEngine.status() 给出精确到路径的中文修复提示；
* 所有推理串行化（dlib 对象非严格线程安全），由调用方放到线程池里执行；
* 检测在缩略图上做（默认 480px 宽），关键点和特征在原始分辨率上做，
  兼顾速度与 128 维特征质量。
"""
from __future__ import annotations

import hashlib
import logging
import shutil
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from PIL import Image

from .config import Settings

logger = logging.getLogger("facepass.engine")

try:  # dlib 属于可选二进制依赖，导入失败不能拖垮整个服务
    import dlib  # type: ignore

    _DLIB_IMPORT_ERROR: Optional[str] = None
except Exception as exc:  # pragma: no cover - 取决于本机环境
    dlib = None  # type: ignore
    _DLIB_IMPORT_ERROR = f"{type(exc).__name__}: {exc}"

LEFT_EYE = (36, 37, 38, 39, 40, 41)
RIGHT_EYE = (42, 43, 44, 45, 46, 47)

MODEL_DOWNLOADS = {
    "shape_predictor_68_face_landmarks.dat": "http://dlib.net/files/shape_predictor_68_face_landmarks.dat.bz2",
    "dlib_face_recognition_resnet_model_v1.dat": "http://dlib.net/files/dlib_face_recognition_resnet_model_v1.dat.bz2",
    "mmod_human_face_detector.dat": "http://dlib.net/files/mmod_human_face_detector.dat.bz2",
}

DLIB_INSTALL_HINT = (
    "在项目虚拟环境中执行：pip install dlib。"
    "注意 PyPI 只提供源码包：Windows 需要 CMake + Visual Studio Build Tools（C++ 工作负载）；"
    "更省事的替代方案是 conda install -c conda-forge dlib，"
    "或下载与 Python 版本匹配的社区预编译 wheel 后 pip install 该 .whl 文件。"
)


class FaceEngineUnavailable(RuntimeError):
    def __init__(self, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.code = code
        self.message = message
        self.hint = hint

    def to_dict(self) -> dict:
        return {"ok": False, "code": self.code, "message": self.message, "hint": self.hint}


class EnrollmentError(RuntimeError):
    def __init__(self, message: str, hint: str = ""):
        super().__init__(message)
        self.message = message
        self.hint = hint


def ascii_safe_model_path(path: Path) -> Tuple[Path, Optional[Path]]:
    """dlib 在 Windows 上用窄字符 fopen，中文路径会导致「Unable to open file」。

    如果路径含非 ASCII 字符，就把它复制到系统临时目录后加载。
    返回 (可加载路径, 缓存路径或 None)。
    """
    path = Path(path)
    try:
        str(path).encode("ascii")
        return path, None
    except UnicodeEncodeError:
        pass

    cache_dir = Path(tempfile.gettempdir()) / "facepass-models"
    cache_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha1(str(path).encode("utf-8")).hexdigest()[:10]
    target = cache_dir / f"{path.stem}-{digest}{path.suffix}"
    try:
        need_copy = (
            not target.is_file() or target.stat().st_size != path.stat().st_size
        )
        if need_copy:
            shutil.copy2(path, target)
    except OSError:
        return path, None
    return target, target


@dataclass
class FaceAnalysis:
    face_found: bool
    ear: Optional[float] = None
    bbox: Optional[Tuple[float, float, float, float]] = None  # 归一化 x, y, w, h
    encoding: Optional[np.ndarray] = None
    face_count: int = 0


@dataclass
class MatchOutcome:
    """一次比对的结果：无论是否通过阈值，都带回最接近的员工与距离。"""

    employee: dict
    distance: float
    accepted: bool

    def to_dict(self) -> dict:
        return {
            "employee": {
                "employee_id": self.employee["employee_id"],
                "emp_no": self.employee["emp_no"],
                "name": self.employee["name"],
                "department": self.employee.get("department", ""),
            },
            "distance": round(float(self.distance), 4),
            "accepted": self.accepted,
        }


@dataclass
class EngineStatus:
    ready: bool
    dlib_available: bool
    detector: str = "hog"
    message: str = ""
    hint: str = ""
    models: Dict[str, dict] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ready": self.ready,
            "dlib_available": self.dlib_available,
            "detector": self.detector,
            "message": self.message,
            "hint": self.hint,
            "models": self.models,
            "warnings": self.warnings,
        }


# --------------------------------------------------------------------------- 纯数学
def eye_aspect_ratio(points: np.ndarray) -> float:
    """EAR = (|p2-p6| + |p3-p5|) / (2 * |p1-p4|)，points 为 6x2。"""
    p = np.asarray(points, dtype=np.float64)
    if p.shape != (6, 2):
        raise ValueError("眼睛关键点必须是 6x2 数组")
    vertical = np.linalg.norm(p[1] - p[5]) + np.linalg.norm(p[2] - p[4])
    horizontal = np.linalg.norm(p[0] - p[3])
    if horizontal < 1e-6:
        return 0.0
    return float(vertical / (2.0 * horizontal))


def ear_from_landmarks(landmarks: np.ndarray) -> float:
    """68 点 -> 双眼 EAR 平均值。"""
    pts = np.asarray(landmarks, dtype=np.float64)
    if pts.shape[0] < 68:
        raise ValueError("关键点数量不足 68")
    left = eye_aspect_ratio(pts[list(LEFT_EYE)])
    right = eye_aspect_ratio(pts[list(RIGHT_EYE)])
    return float((left + right) / 2.0)


def match_face(
    encoding: np.ndarray,
    gallery: List[dict],
    threshold: float,
) -> Optional[MatchOutcome]:
    """欧氏距离比对。同一员工取多张样本中的最小距离，返回阈值内最近的一位。"""
    if encoding is None or len(gallery) == 0:
        return None

    vector = np.asarray(encoding, dtype=np.float64).reshape(-1)
    matrix = np.stack([np.asarray(item["vector"], dtype=np.float64).reshape(-1) for item in gallery])
    distances = np.linalg.norm(matrix - vector, axis=1)

    best_by_employee: Dict[int, Tuple[float, dict]] = {}
    for item, distance in zip(gallery, distances):
        employee_id = int(item["employee_id"])
        current = best_by_employee.get(employee_id)
        if current is None or float(distance) < current[0]:
            best_by_employee[employee_id] = (float(distance), item)

    if not best_by_employee:
        return None
    distance, employee = min(best_by_employee.values(), key=lambda pair: pair[0])
    return MatchOutcome(employee=employee, distance=distance, accepted=distance < threshold)


# 兼容旧命名（threshold 内才返回，供调用方直接判断）
def best_match(encoding: np.ndarray, gallery: List[dict], threshold: float) -> Optional[MatchOutcome]:
    outcome = match_face(encoding, gallery, threshold)
    return outcome if outcome and outcome.accepted else None


# --------------------------------------------------------------------------- 引擎
class FaceEngine:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.RLock()
        self._load_lock = threading.Lock()
        self._loaded = False
        self._load_error: Optional[FaceEngineUnavailable] = None
        self._detector = None
        self._predictor = None
        self._recognizer = None
        self._status_cache: Optional[EngineStatus] = None

    # ---------------------------------------------------------------- 状态
    def status(self, refresh: bool = False) -> EngineStatus:
        if self._status_cache is not None and not refresh:
            return self._status_cache

        models = {}
        for name, path in (
            ("shape_predictor", self.settings.shape_predictor_path),
            ("face_recognition", self.settings.face_recognition_model_path),
        ):
            models[name] = self._inspect_model(path)
        if self.settings.face_detector == "cnn":
            models["cnn_detector"] = self._inspect_model(
                self.settings.cnn_detector_path or Path("models/mmod_human_face_detector.dat")
            )

        warnings = list(self.settings.warnings)

        if dlib is None:
            status = EngineStatus(
                ready=False,
                dlib_available=False,
                detector=self.settings.face_detector,
                message="未安装 dlib，人脸识别引擎不可用",
                hint=(_DLIB_IMPORT_ERROR or "") + " | " + DLIB_INSTALL_HINT,
                models=models,
                warnings=warnings,
            )
            self._status_cache = status
            return status

        missing = [item for item in models.values() if not item["exists"]]
        if missing:
            paths = "、".join(item["path"] for item in missing)
            hint_parts = [f"缺少模型文件：{paths}。请从 dlib 官网下载 .bz2 并解压到上述路径。"]
            for item in missing:
                download = MODEL_DOWNLOADS.get(Path(item["path"]).name)
                if download:
                    hint_parts.append(f"{Path(item['path']).name} -> {download}")
            packaged = [item["path"] for item in missing if item.get("bz2_present")]
            if packaged:
                hint_parts.append("检测到同名 .bz2 压缩包，请先解压后再启动。")
            hint_parts.append("也可以运行 python scripts/check_env.py 查看完整自检结果。")
            status = EngineStatus(
                ready=False,
                dlib_available=True,
                detector=self.settings.face_detector,
                message="缺少 dlib 模型文件，无法提取 128 维特征",
                hint=" ".join(hint_parts),
                models=models,
                warnings=warnings,
            )
            self._status_cache = status
            return status

        if not self._loaded:
            try:
                self._load()
            except FaceEngineUnavailable as exc:
                self._load_error = exc
                status = EngineStatus(
                    ready=False,
                    dlib_available=True,
                    detector=self.settings.face_detector,
                    message=exc.message,
                    hint=exc.hint,
                    models=models,
                    warnings=warnings,
                )
                self._status_cache = status
                return status

        status = EngineStatus(
            ready=True,
            dlib_available=True,
            detector=self.settings.face_detector,
            message="人脸识别引擎就绪（dlib ResNet 128 维）",
            hint="",
            models=models,
            warnings=warnings,
        )
        self._status_cache = status
        return status

    def _inspect_model(self, path: Path) -> dict:
        path = Path(path)
        exists = path.is_file()
        size = path.stat().st_size if exists else 0
        bz2 = Path(str(path) + ".bz2").is_file()
        try:
            str(path).encode("ascii")
            ascii_safe = True
        except UnicodeEncodeError:
            ascii_safe = False
        return {
            "path": str(path),
            "exists": exists,
            "size": size,
            "size_text": f"{size / 1024 / 1024:.1f} MB" if size else "",
            "bz2_present": bz2,
            "ascii_safe": ascii_safe,
        }

    def _load(self) -> None:
        with self._load_lock:
            if self._loaded:
                return
            assert dlib is not None
            try:
                if self.settings.face_detector == "cnn":
                    cnn_path = self.settings.cnn_detector_path
                    if not cnn_path or not Path(cnn_path).is_file():
                        raise FaceEngineUnavailable(
                            "face_engine_unavailable",
                            "已选择 CNN 检测器，但未找到 mmod_human_face_detector.dat",
                            f"请把模型放到 {cnn_path}，或在 .env 中改回 FACE_DETECTOR=hog。",
                        )
                    cnn_loadable, cnn_cache = ascii_safe_model_path(Path(cnn_path))
                    self._detector = dlib.cnn_face_detection_model_v1(str(cnn_loadable))
                    if cnn_cache:
                        logger.info("CNN 模型路径含中文，已缓存到 %s", cnn_cache)
                else:
                    self._detector = dlib.get_frontal_face_detector()

                shape_loadable, shape_cache = ascii_safe_model_path(
                    self.settings.shape_predictor_path
                )
                recog_loadable, recog_cache = ascii_safe_model_path(
                    self.settings.face_recognition_model_path
                )
                if shape_cache:
                    logger.info("68 点模型路径含中文，已缓存到 %s", shape_cache)
                if recog_cache:
                    logger.info("特征模型路径含中文，已缓存到 %s", recog_cache)

                self._predictor = dlib.shape_predictor(str(shape_loadable))
                self._recognizer = dlib.face_recognition_model_v1(str(recog_loadable))
                self._loaded = True
                self._load_error = None
            except FaceEngineUnavailable:
                raise
            except RuntimeError as exc:
                raise FaceEngineUnavailable(
                    "face_engine_unavailable",
                    "dlib 模型文件无法加载（可能已损坏或与 dlib 版本不匹配）",
                    f"{exc}. 请重新下载官方模型文件，并确认解压后大小约为 95MB / 22MB。",
                ) from exc

    def require_ready(self) -> None:
        status = self.status()
        if not status.ready:
            raise FaceEngineUnavailable(
                "face_engine_unavailable", status.message, status.hint
            )

    # ---------------------------------------------------------------- 推理
    def analyze(self, rgb: np.ndarray, embed: str = "auto") -> FaceAnalysis:
        """单帧分析。

        embed: True 强制提取特征；False 不提取；"auto" 仅在眼睛睁开时提取。
        """
        self.require_ready()
        image = np.ascontiguousarray(rgb)
        if image.ndim != 3 or image.shape[2] != 3:
            raise ValueError("输入图像必须是 HxWx3 的 RGB 数组")
        height, width = image.shape[:2]

        scale = min(1.0, float(self.settings.frame_max_width) / float(max(1, width)))
        if scale < 1.0:
            small = np.asarray(
                Image.fromarray(image).resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.BILINEAR,
                )
            )
        else:
            small = image

        with self._lock:
            rects = self._detect(small)
            if not rects:
                return FaceAnalysis(face_found=False, face_count=0)
            rect = max(rects, key=lambda r: r.width() * r.height())
            full_rect = dlib.rectangle(
                int(round(rect.left() / scale)),
                int(round(rect.top() / scale)),
                int(round(rect.right() / scale)),
                int(round(rect.bottom() / scale)),
            )
            shape = self._predictor(image, full_rect)

            points = np.array(
                [[shape.part(i).x, shape.part(i).y] for i in range(68)], dtype=np.float64
            )
            ear = ear_from_landmarks(points)

            encoding = None
            want_embed = embed is True or (
                embed == "auto" and ear > self.settings.ear_threshold * 1.05
            )
            if want_embed:
                encoding = self._descriptor(image, shape)

        left = max(0.0, full_rect.left() / width)
        top = max(0.0, full_rect.top() / height)
        box_w = min(1.0 - left, full_rect.width() / width)
        box_h = min(1.0 - top, full_rect.height() / height)

        return FaceAnalysis(
            face_found=True,
            ear=ear,
            bbox=(round(left, 4), round(top, 4), round(box_w, 4), round(box_h, 4)),
            encoding=encoding,
            face_count=len(rects),
        )

    def _detect(self, image: np.ndarray):
        assert self._detector is not None
        rects = self._detector(image, 0)
        if self.settings.face_detector == "cnn":
            return [r.rect for r in rects]
        return list(rects)

    def _descriptor(self, image: np.ndarray, shape) -> Optional[np.ndarray]:
        assert self._recognizer is not None
        try:
            chip = dlib.get_face_chip(image, shape, size=150, padding=0.25)
            vector = np.asarray(self._recognizer.compute_face_descriptor(chip), dtype=np.float64)
        except Exception:
            return None
        if vector.shape[0] != 128:
            return None
        return vector

    # ---------------------------------------------------------------- 录入
    def extract_single_encoding(self, rgb: np.ndarray) -> np.ndarray:
        """录入用：要求画面中恰好一张人脸，返回 128 维特征。"""
        self.require_ready()
        image = np.ascontiguousarray(rgb)
        height, width = image.shape[:2]
        scale = min(1.0, float(self.settings.frame_max_width) / float(max(1, width)))
        small = (
            np.asarray(
                Image.fromarray(image).resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.BILINEAR,
                )
            )
            if scale < 1.0
            else image
        )

        with self._lock:
            rects = self._detect(small)
            if not rects:
                raise EnrollmentError(
                    "画面中没有检测到人脸",
                    "请正对摄像头、保证光线充足，并让面部占据画面 1/3 以上。",
                )
            if len(rects) > 1:
                raise EnrollmentError(
                    "画面中检测到多张人脸",
                    "录入时请确保镜头中只有本人，避免同事路过或照片入镜。",
                )
            rect = rects[0]
            full_rect = dlib.rectangle(
                int(round(rect.left() / scale)),
                int(round(rect.top() / scale)),
                int(round(rect.right() / scale)),
                int(round(rect.bottom() / scale)),
            )
            shape = self._predictor(image, full_rect)
            vector = self._descriptor(image, shape)

        if vector is None:
            raise EnrollmentError(
                "人脸特征提取失败",
                "请调整光线与角度后重试，避免强逆光、侧脸或遮挡。",
            )
        return vector

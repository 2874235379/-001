"""真实 dlib 引擎测试（需要已安装 dlib + 两个模型文件）。

用法：
    python tests/test_face_real.py --image a.jpg b.jpg

验证点：
* HOG 检测到人脸
* 68 点关键点算出的 EAR 在合理范围（睁眼 > 0.25）
* 同一张图轻微缩放 / 调亮后，欧氏距离 < 0.45
* 不同人的照片，欧氏距离 > 0.45
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.db import blob_to_vector, vector_to_blob  # noqa: E402
from app.checkin import CheckinSession  # noqa: E402
from app.face_service import FaceEngine, match_face  # noqa: E402

PASSED = []
FAILED = []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def load_rgb(path: Path, width=640) -> np.ndarray:
    image = Image.open(path).convert("RGB")
    if image.width > width:
        ratio = width / image.width
        image = image.resize((width, int(image.height * ratio)), Image.BILINEAR)
    return np.asarray(image, dtype=np.uint8)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", nargs="+", required=True, help="两张不同人的正脸照片")
    args = parser.parse_args()
    paths = [Path(p) for p in args.image]
    if len(paths) < 2:
        print("[错误] 至少需要两张不同人的照片")
        return 2

    settings = load_settings()
    engine = FaceEngine(settings)
    status = engine.status(refresh=True)
    print("=" * 62)
    print("FacePass 真实 dlib 引擎测试")
    print("=" * 62)
    print(f"dlib 就绪：{status.ready} | {status.message}")
    if not status.ready:
        print(f"提示：{status.hint}")
        return 0

    encodings = []
    for path in paths[:2]:
        rgb = load_rgb(path)
        analysis = engine.analyze(rgb, embed=True)
        print(f"\n[{path.name}] {rgb.shape[1]}x{rgb.shape[0]}")
        check("检测到人脸", analysis.face_found, str(analysis.face_count))
        if not analysis.face_found:
            continue
        check("EAR 为合理睁眼值（> 0.12）", analysis.ear is not None and analysis.ear > 0.12, f"ear={analysis.ear}")
        if analysis.ear is not None:
            session = CheckinSession(ear_threshold=settings.ear_threshold)
            obs = None
            for _ in range(4):
                obs = session.observe(True, analysis.ear)
            check(
                "自适应校准后不会把该用户误判为闭眼",
                session.calibrated and session.threshold < analysis.ear and not obs.blink,
                f"ear={analysis.ear:.3f} threshold={session.threshold:.3f}",
            )
        check("提取到 128 维特征", analysis.encoding is not None and analysis.encoding.shape == (128,))
        if analysis.encoding is not None:
            encodings.append(analysis.encoding)
            print(f"       ear={analysis.ear:.4f} bbox={analysis.bbox}")

    if len(encodings) == 2:
        distance = float(np.linalg.norm(encodings[0] - encodings[1]))
        check("两个不同人距离 > 0.45", distance > 0.45, f"distance={distance:.3f}")
        print(f"       不同人距离 = {distance:.3f}")

    if encodings:
        # 同图扰动：缩到 480 + 调亮 8%
        image = Image.open(paths[0]).convert("RGB")
        image = image.resize((480, int(image.height * 480 / image.width)), Image.BILINEAR)
        image = ImageEnhance.Brightness(image).enhance(1.08)
        rgb = np.asarray(image, dtype=np.uint8)
        again = engine.analyze(rgb, embed=True)
        check("同一人扰动后仍检测到人脸", again.face_found)
        if again.encoding is not None:
            distance = float(np.linalg.norm(encodings[0] - again.encoding))
            check("同一人扰动后距离 < 0.45", distance < 0.45, f"distance={distance:.3f}")
            print(f"       同人扰动距离 = {distance:.3f}")

        # BLOB 往返
        restored = blob_to_vector(vector_to_blob(encodings[0]), 128)
        check("特征 BLOB 往返误差 < 1e-6", np.max(np.abs(restored - encodings[0])) < 1e-6)

        # 用真实特征跑一次 match_face
        gallery = [
            {
                "employee_id": 1,
                "emp_no": "E001",
                "name": "A",
                "department": "",
                "vector": encodings[0],
            },
            {
                "employee_id": 2,
                "emp_no": "E002",
                "name": "B",
                "department": "",
                "vector": encodings[1] if len(encodings) > 1 else encodings[0] + 1.0,
            },
        ]
        outcome = match_face(encodings[0], gallery, settings.match_threshold)
        check("真实特征命中本人", outcome is not None and outcome.accepted and outcome.employee["emp_no"] == "E001")

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

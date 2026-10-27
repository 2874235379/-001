"""命令行录入员工人脸（无需打开浏览器）。

用法：
    python scripts/enroll.py --emp-no E001 --name 张三 --dept 研发 --image a.jpg b.jpg c.jpg
    python scripts/enroll.py --list
    python scripts/enroll.py --emp-no E001 --name 张三 --dept 研发 --image a.jpg --append
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image, ImageOps  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.db import DatabaseError, MySQLPool  # noqa: E402
from app.face_service import EnrollmentError, FaceEngine, FaceEngineUnavailable  # noqa: E402


def load_image(path: Path, max_width: int) -> np.ndarray:
    if not path.is_file():
        raise EnrollmentError(f"找不到图片：{path}", "请检查 --image 后面的路径是否正确。")
    image = Image.open(path)
    image = ImageOps.exif_transpose(image).convert("RGB")
    if image.width > max_width:
        ratio = max_width / image.width
        image = image.resize((max_width, int(image.height * ratio)), Image.BILINEAR)
    return np.asarray(image, dtype=np.uint8)


def main() -> int:
    parser = argparse.ArgumentParser(description="录入员工人脸特征")
    parser.add_argument("--emp-no", help="工号，例如 E001")
    parser.add_argument("--name", help="姓名")
    parser.add_argument("--dept", default="", help="部门")
    parser.add_argument("--image", nargs="*", default=[], help="一张或多张清晰正脸照片")
    parser.add_argument("--append", action="store_true", help="追加样本而不是覆盖旧样本")
    parser.add_argument("--list", action="store_true", help="列出已录入员工")
    args = parser.parse_args()

    settings = load_settings()
    pool = MySQLPool(settings)

    try:
        if args.list:
            rows = pool.list_employees()
            if not rows:
                print("还没有员工，先录入一位吧。")
                return 0
            print(f"{'工号':<12}{'姓名':<10}{'部门':<14}{'样本数':<8}创建时间")
            for row in rows:
                print(
                    f"{row['emp_no']:<12}{row['name']:<10}"
                    f"{(row['department'] or '-'):<14}{row['samples']:<8}{row['created_at']}"
                )
            return 0

        if not (args.emp_no and args.name):
            parser.print_help()
            print("\n[错误] --emp-no 和 --name 必填")
            return 2
        if not args.image:
            print("[错误] 至少通过 --image 提供一张照片")
            return 2

        engine = FaceEngine(settings)
        status = engine.status(refresh=True)
        if not status.ready:
            print(f"[错误] {status.message}")
            if status.hint:
                print(f"       {status.hint}")
            return 1

        vectors = []
        for raw_path in args.image:
            path = Path(raw_path)
            print(f"处理 {path.name} …", end=" ", flush=True)
            try:
                rgb = load_image(path, settings.frame_max_width)
                vector = engine.extract_single_encoding(rgb)
                vectors.append(vector)
                print("OK")
            except EnrollmentError as exc:
                print("失败")
                print(f"  - {exc.message}")
                if exc.hint:
                    print(f"    {exc.hint}")

        if not vectors:
            print("\n[错误] 没有可用样本，录入中止。")
            return 1

        employee = pool.save_employee_with_encodings(
            args.emp_no.strip(),
            args.name.strip(),
            args.dept.strip(),
            vectors,
            "dlib_resnet_v1",
            replace=not args.append,
        )
        print(
            f"\n[OK] 已录入 {employee['name']}（{employee['emp_no']} / "
            f"{employee['department'] or '未填部门'}），共 {employee['samples']} 个样本。"
        )
        print("接下来启动服务：python -m app.main")
        return 0

    except DatabaseError as exc:
        print(f"[数据库错误] {exc.message}")
        if exc.hint:
            print(f"            {exc.hint}")
        return 1
    except FaceEngineUnavailable as exc:
        print(f"[引擎错误] {exc.message}")
        if exc.hint:
            print(f"          {exc.hint}")
        return 1
    finally:
        pool.close()


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())

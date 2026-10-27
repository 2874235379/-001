"""环境自检：一条命令看清「还差什么」。

用法：
    python scripts/check_env.py
退出码：0 = 全部就绪；1 = 存在未就绪项
"""
from __future__ import annotations

import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_settings  # noqa: E402
from app.db import MySQLPool  # noqa: E402
from app.face_service import FaceEngine  # noqa: E402

OK = "[ OK ]"
BAD = "[FAIL]"
WARN = "[WARN]"


def line(status: str, title: str, detail: str = "") -> None:
    print(f"{status} {title}" + (f"  {detail}" if detail else ""))


def main() -> int:
    settings = load_settings()
    failures = 0

    print("=" * 74)
    print("FacePass 环境自检")
    print("=" * 74)
    line(OK, f"Python {platform.python_version()}", platform.platform())
    if sys.version_info >= (3, 12):
        line(
            WARN,
            "Python 版本提示",
            "PyPI 上 dlib 只有源码包；若 import dlib 失败，请安装 CMake + VS Build Tools，或改用 conda-forge / 社区预编译 wheel。",
        )

    # ---------------- 依赖包 ----------------
    packages = {
        "fastapi": "fastapi",
        "uvicorn": "uvicorn",
        "pymysql": "pymysql",
        "numpy": "numpy",
        "PIL": "pillow",
    }
    for module, label in packages.items():
        try:
            __import__(module)
            line(OK, f"依赖 {label}")
        except Exception as exc:
            failures += 1
            line(BAD, f"依赖 {label}", f"{type(exc).__name__}: {exc}")
    try:
        import multipart  # noqa: F401

        line(OK, "依赖 python-multipart")
    except Exception:
        failures += 1
        line(BAD, "依赖 python-multipart", "pip install python-multipart")

    # ---------------- 数据库 ----------------
    pool = MySQLPool(settings)
    db_status = pool.health()
    if db_status["ok"]:
        line(OK, "MySQL 连接", f"{settings.db_dsn} / MySQL {db_status.get('version', '')}")
    else:
        failures += 1
        line(BAD, "MySQL 连接", db_status["message"])
        if db_status.get("hint"):
            line("", "      修复建议", db_status["hint"])
    pool.close()

    # ---------------- 识别引擎 ----------------
    engine = FaceEngine(settings)
    engine_status = engine.status(refresh=True)
    if engine_status.ready:
        line(OK, "识别引擎", f"dlib {engine_status.detector.upper()} + ResNet 128 维")
    else:
        failures += 1
        line(BAD, "识别引擎", engine_status.message)
        if engine_status.hint:
            line("", "      修复建议", engine_status.hint)

    print("-" * 74)
    print("模型文件：")
    for name, item in engine_status.models.items():
        status = OK if item["exists"] else BAD
        detail = item["path"]
        if item["exists"]:
            detail += f"  ({item['size_text']})"
        elif item.get("bz2_present"):
            detail += "  （检测到 .bz2，请先解压）"
        print(f"  {status} {name}: {detail}")

    if settings.warnings:
        print("-" * 74)
        for warning in settings.warnings:
            print(f"{WARN} 配置：{warning}")

    print("=" * 74)
    if failures:
        print(f"共 {failures} 项未就绪，请按上面的提示修复后重新自检。")
        print("提示：MySQL 可用 `python scripts/init_db.py` 一键建库建表。")
        return 1
    print("全部就绪，可以运行：python -m app.main")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())

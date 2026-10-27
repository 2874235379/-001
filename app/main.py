"""FacePass · 人脸考勤打卡系统 —— FastAPI 入口。

职责：
* 托管前端静态文件（同源部署，无需 nginx / 打包工具）
* WebSocket 实时帧识别（前端发 JPEG，后端回状态 / 结果）
* 员工录入、健康检查、今日记录等 REST 接口
"""
from __future__ import annotations

import io
import json
import logging
import platform
import re
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import date, datetime
from typing import List, Optional

import numpy as np
from fastapi import FastAPI, File, Form, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from starlette.concurrency import run_in_threadpool

from .checkin import (
    PHASE_AWAIT_OPEN,
    PHASE_BLINK,
    PHASE_DONE,
    PHASE_NO_FACE,
    PHASE_READY,
    PHASE_SCANNING,
    PHASE_TIMEOUT,
    CheckinSession,
    Observation,
)
from .config import Settings, load_settings
from .db import DatabaseError, MySQLPool
from .face_service import (
    EnrollmentError,
    FaceAnalysis,
    FaceEngine,
    FaceEngineUnavailable,
    match_face,
)
from .netinfo import lan_ipv4_addresses

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("facepass")

EMP_NO_PATTERN = re.compile(r"^[A-Za-z0-9_-]{2,32}$")
MAX_ENROLL_FILES = 5
MAX_ENROLL_FILE_BYTES = 8 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status_code: int, code: str, message: str, hint: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.hint = hint


class BadFrame(Exception):
    pass


def error_payload(code: str, message: str, hint: str = "", **extra) -> dict:
    payload = {"ok": False, "code": code, "message": message, "hint": hint}
    payload.update(extra)
    return payload


# --------------------------------------------------------------------------- 工具
def decode_frame(data: bytes, settings: Settings) -> np.ndarray:
    try:
        image = Image.open(io.BytesIO(data))
        image = ImageOps.exif_transpose(image).convert("RGB")
    except Exception as exc:  # 损坏帧 / 非图片
        raise BadFrame(f"JPEG 解码失败：{type(exc).__name__}") from exc

    if image.width <= 0 or image.height <= 0:
        raise BadFrame("空图像")
    if image.width > settings.frame_max_width:
        ratio = settings.frame_max_width / float(image.width)
        image = image.resize(
            (settings.frame_max_width, max(1, int(round(image.height * ratio)))),
            Image.BILINEAR,
        )
    return np.asarray(image, dtype=np.uint8)


def _fmt_time(value) -> str:
    if isinstance(value, datetime):
        return value.strftime("%H:%M:%S")
    if isinstance(value, str) and " " in value:
        return value.split(" ", 1)[1]
    return str(value)


def start_https_listener(app: FastAPI, settings: Settings):
    """在独立线程里再起一个 HTTPS 监听，给手机 / 局域网设备用。

    HTTP 8010 保留给本机开发，HTTPS 8443 给需要调用摄像头的移动端。
    lifespan 关闭，避免重复执行启动逻辑（共用同一个 app.state）。
    """
    if not settings.ssl_enabled:
        return None
    cert, key = settings.ssl_certfile, settings.ssl_keyfile
    if cert is None or key is None or not cert.is_file() or not key.is_file():
        logger.warning("已配置 SSL 证书但文件不存在，跳过 HTTPS：%s | %s", cert, key)
        logger.warning("可运行 python scripts/gen_cert.py 生成自签证书")
        return None
    if settings.https_port == settings.app_port:
        logger.warning("HTTPS_PORT 与 APP_PORT 相同（%s），跳过 HTTPS 监听", settings.https_port)
        return None

    import uvicorn

    config = uvicorn.Config(
        app,
        host=settings.app_host,
        port=settings.https_port,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        log_level="info",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="facepass-https", daemon=True)
    thread.start()
    logger.info("HTTPS 监听已启动：https://%s:%s", settings.app_host, settings.https_port)
    return server


def _sha_preview(value: str, keep: int = 3) -> str:
    if not value:
        return ""
    return value[:keep] + "***"


# --------------------------------------------------------------------------- 应用
def create_app(
    settings: Optional[Settings] = None,
    engine: Optional[FaceEngine] = None,
    db: Optional[MySQLPool] = None,
) -> FastAPI:
    settings = settings or load_settings()
    engine = engine or FaceEngine(settings)
    db = db or MySQLPool(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("=" * 68)
        logger.info("FacePass 人脸打卡系统启动中…")
        logger.info("Python %s | %s", platform.python_version(), platform.platform())
        logger.info("数据库 %s（密码 %s）", settings.db_dsn, _sha_preview(settings.db_password))

        db_status = await run_in_threadpool(db.health)
        if db_status["ok"]:
            logger.info("数据库  [OK] MySQL %s，字符集 utf8mb4", db_status.get("version", ""))
        else:
            logger.warning("数据库  [未就绪] %s", db_status["message"])
            if db_status.get("hint"):
                logger.warning("         修复建议：%s", db_status["hint"])

        engine_status = await run_in_threadpool(engine.status, True)
        if engine_status.ready:
            logger.info("识别引擎 [OK] dlib %s 检测器，128 维特征已加载", engine_status.detector.upper())
        else:
            logger.warning("识别引擎 [未就绪] %s", engine_status.message)
            if engine_status.hint:
                logger.warning("         修复建议：%s", engine_status.hint)

        if not settings.static_dir.is_dir():
            logger.error("静态目录不存在：%s", settings.static_dir)

        if settings.app_host in ("0.0.0.0", "::"):
            ips = lan_ipv4_addresses()
            if ips:
                for ip in ips:
                    logger.info("本机访问   http://127.0.0.1:%s", settings.app_port)
                    logger.info("局域网访问 http://%s:%s", ip, settings.app_port)
                    if settings.ssl_enabled:
                        logger.info("手机访问   https://%s:%s（摄像头需要 https）", ip, settings.https_port)
            else:
                logger.info("监听所有网卡：http://0.0.0.0:%s", settings.app_port)
            logger.warning("当前已对局域网开放，且系统没有内置登录认证；请勿直接暴露到公网。")
        else:
            logger.info("访问地址 http://%s:%s", settings.app_host, settings.app_port)

        if settings.ssl_enabled and (
            settings.ssl_certfile is None
            or settings.ssl_keyfile is None
            or not settings.ssl_certfile.is_file()
            or not settings.ssl_keyfile.is_file()
        ):
            logger.warning("SSL 证书配置不完整或文件不存在，手机将无法调用摄像头。")
            logger.warning("运行 python scripts/gen_cert.py 生成证书后重启即可。")
        logger.info("=" * 68)
        yield
        db.close()
        logger.info("已关闭数据库连接池，服务退出")

    app = FastAPI(
        title="FacePass 人脸打卡系统",
        version="1.0.0",
        description="dlib 128 维特征 + EAR 眨眼活体检测 + Apple 风格交互动效",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.db = db
    app.state.started_at = time.time()

    # ---------------------------------------------------------------- 异常处理
    @app.exception_handler(ApiError)
    async def handle_api_error(request: Request, exc: ApiError):
        return JSONResponse(
            status_code=exc.status_code,
            content=error_payload(exc.code, exc.message, exc.hint),
        )

    @app.exception_handler(DatabaseError)
    async def handle_db_error(request: Request, exc: DatabaseError):
        logger.error("数据库异常：%s | %s", exc.message, exc.hint)
        return JSONResponse(status_code=503, content=error_payload("db_error", exc.message, exc.hint))

    @app.exception_handler(FaceEngineUnavailable)
    async def handle_engine_error(request: Request, exc: FaceEngineUnavailable):
        return JSONResponse(
            status_code=503,
            content=error_payload(exc.code, exc.message, exc.hint),
        )

    # ---------------------------------------------------------------- 基础接口
    @app.get("/api/health")
    async def health():
        db_status = await run_in_threadpool(db.health)
        engine_status = await run_in_threadpool(engine.status, True)
        engine_payload = engine_status.to_dict()
        ok = bool(db_status["ok"] and engine_payload["ready"])
        return {
            "ok": ok,
            "ready": {
                "database": db_status["ok"],
                "engine": engine_payload["ready"],
            },
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "python": platform.python_version(),
            "db": db_status,
            "engine": engine_payload,
            "models_dir": str(settings.models_dir),
            "config": settings.public_dict(),
            "warnings": settings.warnings,
            "uptime_seconds": int(time.time() - app.state.started_at),
        }

    @app.get("/api/config")
    async def public_config():
        return {"ok": True, "config": settings.public_dict()}

    @app.get("/api/overview")
    async def overview():
        today = date.today()
        data = await run_in_threadpool(db.overview, today)
        return {
            "ok": True,
            "date": today.isoformat(),
            "work_start": settings.work_start_text,
            "work_end": settings.work_end_text,
            "duplicate_window_seconds": settings.duplicate_window_seconds,
            "server_time": datetime.now().strftime("%H:%M:%S"),
            **data,
        }

    @app.get("/api/records")
    async def records(date_str: str = ""):
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else date.today()
        except ValueError:
            raise ApiError(400, "bad_date", "日期格式应为 YYYY-MM-DD", "例如 2026-10-25")
        rows = await run_in_threadpool(db.records_for_date, target, 200)
        return {"ok": True, "date": target.isoformat(), "records": rows}

    @app.get("/api/employees")
    async def employees():
        rows = await run_in_threadpool(db.list_employees)
        return {"ok": True, "employees": rows}

    @app.post("/api/employees/enroll")
    async def enroll(
        emp_no: str = Form(...),
        name: str = Form(...),
        department: str = Form(""),
        files: List[UploadFile] = File(...),
    ):
        emp_no = emp_no.strip()
        name = name.strip()
        department = department.strip()

        if not EMP_NO_PATTERN.match(emp_no):
            raise ApiError(
                400,
                "bad_emp_no",
                "工号格式不合法",
                "工号只用字母、数字、下划线或短横线，长度 2~32，例如 E001。",
            )
        if not 1 <= len(name) <= 32:
            raise ApiError(400, "bad_name", "姓名长度应为 1~32 个字符", "")
        if len(department) > 64:
            raise ApiError(400, "bad_department", "部门名称过长（最多 64 个字符）", "")
        if not files:
            raise ApiError(400, "no_file", "请至少上传 1 张人脸照片", "建议 3 张不同角度的清晰正脸。")
        if len(files) > MAX_ENROLL_FILES:
            raise ApiError(400, "too_many_files", f"最多一次上传 {MAX_ENROLL_FILES} 张照片", "")

        engine_status = await run_in_threadpool(engine.status, True)
        if not engine_status.ready:
            raise ApiError(503, "face_engine_unavailable", engine_status.message, engine_status.hint)

        vectors = []
        failures = []
        for index, upload in enumerate(files, start=1):
            raw = await upload.read()
            if not raw:
                failures.append(f"第 {index} 张：空文件")
                continue
            if len(raw) > MAX_ENROLL_FILE_BYTES:
                failures.append(f"第 {index} 张：文件超过 8MB")
                continue
            try:
                rgb = await run_in_threadpool(decode_frame, raw, settings)
                vector = await run_in_threadpool(engine.extract_single_encoding, rgb)
                vectors.append(vector)
            except (BadFrame, EnrollmentError) as exc:
                message = exc.message if isinstance(exc, EnrollmentError) else str(exc)
                failures.append(f"第 {index} 张：{message}")

        if not vectors:
            raise ApiError(
                400,
                "enroll_failed",
                "没有一张照片能提取到有效人脸特征",
                "；".join(failures)
                or "请确保光线充足、正对镜头、画面中只有本人，避免逆光与遮挡。",
            )

        employee = await run_in_threadpool(
            db.save_employee_with_encodings,
            emp_no,
            name,
            department,
            vectors,
            "dlib_resnet_v1",
            True,
        )
        logger.info("员工录入成功：%s %s（%d 个样本）", emp_no, name, len(vectors))
        return {
            "ok": True,
            "employee": employee,
            "samples": len(vectors),
            "failures": failures,
            "message": f"已录入 {name}（{len(vectors)} 个人脸样本）",
        }

    # ---------------------------------------------------------------- WebSocket
    @app.websocket("/ws/checkin")
    async def ws_checkin(ws: WebSocket):
        await ws.accept()
        session = CheckinSession(
            ear_threshold=settings.ear_threshold,
            consecutive_frames=settings.ear_consecutive_frames,
            timeout_seconds=settings.liveness_timeout_seconds,
        )
        logger.info("WebSocket 已连接：%s", ws.client)

        async def send(payload: dict) -> bool:
            try:
                await ws.send_text(json.dumps(payload, ensure_ascii=False))
                return True
            except Exception:
                return False

        status = await run_in_threadpool(engine.status)
        await send(
            {
                "type": "hello",
                "server": "FacePass 1.0.0",
                "config": settings.public_dict(),
                "engine": status.to_dict(),
                "db_name": settings.db_name,
            }
        )
        if not status.ready:
            await send(error_payload("face_engine_unavailable", status.message, status.hint))

        last_error_at = 0.0

        try:
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    break

                text = message.get("text")
                if text:
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "reset":
                        session.reset()
                        await send({"type": "reset_ok"})
                    elif payload.get("type") == "ping":
                        await send({"type": "pong"})
                    continue

                data = message.get("bytes") or b""
                if not data:
                    continue

                now = time.monotonic()
                if len(data) > settings.frame_max_bytes:
                    if now - last_error_at > 1.0:
                        last_error_at = now
                        await send(
                            error_payload(
                                "frame_too_large",
                                "画面数据过大",
                                f"单帧上限 {settings.frame_max_bytes // 1024} KB，请降低分辨率后重试。",
                            )
                        )
                    continue

                status = await run_in_threadpool(engine.status)
                if not status.ready:
                    if now - last_error_at > 2.0:
                        last_error_at = now
                        await send(
                            error_payload("face_engine_unavailable", status.message, status.hint)
                        )
                    continue

                try:
                    rgb = await run_in_threadpool(decode_frame, data, settings)
                    analysis: FaceAnalysis = await run_in_threadpool(engine.analyze, rgb, "auto")
                except BadFrame as exc:
                    if now - last_error_at > 1.0:
                        last_error_at = now
                        await send(error_payload("bad_frame", "画面解析失败", str(exc)))
                    continue
                except FaceEngineUnavailable as exc:
                    if now - last_error_at > 1.0:
                        last_error_at = now
                        await send(error_payload(exc.code, exc.message, exc.hint))
                    continue
                except Exception as exc:  # 防御：任何推理异常都要有反馈
                    logger.exception("推理异常")
                    if now - last_error_at > 1.0:
                        last_error_at = now
                        await send(
                            error_payload(
                                "engine_error",
                                "识别过程出现异常",
                                f"{type(exc).__name__}: {exc}",
                            )
                        )
                    continue

                observation = session.observe(analysis.face_found, analysis.ear)

                if observation.phase == PHASE_DONE:
                    continue

                if observation.phase == PHASE_TIMEOUT:
                    await send(
                        {
                            "type": "result",
                            "ok": False,
                            "code": "liveness_failed",
                            "message": observation.hint,
                            "hint": "请正对摄像头，自然眨眼一次。照片、屏幕翻拍无法通过活体检测。",
                            "reason": observation.reason,
                        }
                    )
                    logger.info("活体检测未通过：%s", session.snapshot())
                    session.finish()
                    continue

                if observation.phase == PHASE_READY:
                    if analysis.encoding is None:
                        await send(
                            {
                                "type": "state",
                                "state": PHASE_AWAIT_OPEN,
                                "hint": "正在读取面部特征…",
                                "face": True,
                                "blink": True,
                                "progress": round(observation.progress, 3),
                                "bbox": list(analysis.bbox) if analysis.bbox else None,
                            }
                        )
                        continue

                    try:
                        gallery = await run_in_threadpool(db.fetch_gallery)
                    except DatabaseError as exc:
                        await send(error_payload("db_error", exc.message, exc.hint))
                        session.finish()
                        continue

                    outcome = match_face(analysis.encoding, gallery, settings.match_threshold)
                    if outcome is None or not outcome.accepted:
                        nearest = round(outcome.distance, 3) if outcome else None
                        await send(
                            {
                                "type": "result",
                                "ok": False,
                                "code": "unknown",
                                "message": "未识别到员工",
                                "hint": (
                                    "请点击右上角「录入员工」先登记人脸；"
                                    "若已登记，请正对镜头、摘掉口罩或调整光线。"
                                ),
                                "distance": nearest,
                                "threshold": settings.match_threshold,
                            }
                        )
                        logger.info(
                            "比对未通过：最近距离=%s 阈值=%s %s",
                            nearest,
                            settings.match_threshold,
                            session.snapshot(),
                        )
                        session.finish()
                        continue

                    employee = outcome.employee
                    now_dt = datetime.now()
                    try:
                        result = await run_in_threadpool(
                            db.record_attendance,
                            employee,
                            now_dt,
                            settings,
                            session.liveness_score,
                            outcome.distance,
                        )
                    except DatabaseError as exc:
                        await send(error_payload("db_error", exc.message, exc.hint))
                        session.finish()
                        continue

                    employee_payload = {
                        "employee_id": employee["employee_id"],
                        "emp_no": employee["emp_no"],
                        "name": employee["name"],
                        "department": employee.get("department", ""),
                    }

                    if result["duplicate"]:
                        last = result["record"]
                        await send(
                            {
                                "type": "result",
                                "ok": True,
                                "code": "duplicate",
                                "message": f"已打过卡（{_fmt_time(last.get('punch_time'))}）",
                                "hint": f"{settings.duplicate_window_seconds} 秒内重复识别不会写入新记录。",
                                "employee": employee_payload,
                                "last": {
                                    "time": _fmt_time(last.get("punch_time")),
                                    "punch_type": last.get("punch_type"),
                                    "status": last.get("status"),
                                },
                                "time": _fmt_time(now_dt),
                            }
                        )
                        logger.info(
                            "重复打卡：%s %s（%s）",
                            employee["emp_no"],
                            employee["name"],
                            _fmt_time(last.get("punch_time")),
                        )
                    else:
                        decision = result["decision"] or {}
                        record = result["record"]
                        payload = {
                            "type": "result",
                            "ok": True,
                            "code": "success",
                            "message": f"打卡成功，欢迎 {employee['name']}",
                            "employee": employee_payload,
                            "punch_type": record.get("punch_type"),
                            "status": record.get("status"),
                            "time": _fmt_time(record.get("punch_time")),
                            "liveness_score": round(session.liveness_score, 4),
                            "distance": round(outcome.distance, 4),
                            "note": decision.get("note", ""),
                        }
                        await send(payload)
                        logger.info(
                            "打卡成功：%s %s | %s %s | 活体 %.2f | 距离 %.3f",
                            employee["emp_no"],
                            employee["name"],
                            record.get("punch_type"),
                            record.get("status"),
                            session.liveness_score,
                            outcome.distance,
                        )
                    session.finish()
                    continue

                # scanning / no_face / blink / await_open
                await send(
                    {
                        "type": "state",
                        "state": observation.phase,
                        "hint": observation.hint,
                        "face": observation.face_found,
                        "blink": observation.blink,
                        "progress": round(observation.progress, 3),
                        "ear": round(observation.ear, 4) if observation.ear is not None else None,
                        "bbox": list(analysis.bbox) if analysis.bbox else None,
                    }
                )

        except WebSocketDisconnect:
            logger.info("WebSocket 断开：%s", ws.client)
        except Exception:
            logger.exception("WebSocket 处理异常")
            await send(error_payload("ws_error", "连接异常，请刷新页面重试", ""))
        finally:
            logger.info("会话结束：%s", session.snapshot())

    # ---------------------------------------------------------------- 静态资源
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        return Response(status_code=204)

    index_file = settings.static_dir / "index.html"
    if settings.static_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(settings.static_dir), html=True), name="static")
    else:
        @app.get("/", include_in_schema=False)
        async def missing_static():
            return JSONResponse(
                status_code=500,
                content=error_payload(
                    "static_missing",
                    "前端静态目录不存在",
                    f"期望目录：{settings.static_dir}，请确认项目文件完整。",
                ),
            )

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    config: Settings = app.state.settings
    if sys.platform == "win32":
        try:  # 避免 Windows 控制台 GBK 编码导致中文日志报错
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass
    start_https_listener(app, config)
    uvicorn.run(app, host=config.app_host, port=config.app_port, log_level="info")

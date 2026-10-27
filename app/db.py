"""MySQL 连接池 + 数据访问层（pymysql，兼容 MySQL 5.7 / utf8mb4）。

设计要点：
* 手写轻量连接池（BoundedSemaphore 控制总量），避免引入额外依赖；
* 所有数据库异常统一翻译成 DatabaseError，message/hint 直接可以展示给用户；
* 特征向量以 float32 小端序 BLOB 存储，读取时还原成 numpy 数组；
* 打卡写入在事务内用 SELECT ... FOR UPDATE 串行化，杜绝并发重复写入。
"""
from __future__ import annotations

import functools
import queue
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pymysql
from pymysql.cursors import DictCursor

from .attendance import decide_punch
from .config import Settings

REQUIRED_TABLES = ("employees", "face_encodings", "attendance_records")

GALLERY_TTL_SECONDS = 15.0


class DatabaseError(RuntimeError):
    """携带面向用户的中文说明与修复建议。"""

    def __init__(self, message: str, hint: str = "", code: Optional[int] = None):
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.code = code

    def to_dict(self) -> dict:
        return {"ok": False, "message": self.message, "hint": self.hint, "code": self.code}


def vector_to_blob(vector: Sequence[float]) -> bytes:
    """128 维 float64 -> 512 字节 float32 小端序。"""
    arr = np.asarray(vector, dtype="<f4").reshape(-1)
    return arr.tobytes()


def blob_to_vector(blob: bytes, dims: int = 128) -> np.ndarray:
    arr = np.frombuffer(blob, dtype="<f4", count=dims)
    if arr.size != dims:
        raise ValueError(f"特征向量长度异常：期望 {dims}，实际 {arr.size}")
    return arr.astype(np.float64)


def _translate_error(exc: Exception, settings: Settings) -> DatabaseError:
    code = None
    if exc.args and isinstance(exc.args[0], int):
        code = exc.args[0]

    if code == 1045:
        return DatabaseError(
            "MySQL 账号或密码错误，无法登录",
            "请检查 .env 中的 DB_USER / DB_PASSWORD（小皮面板默认 root/root）。",
            code,
        )
    if code == 1049:
        return DatabaseError(
            f"数据库 {settings.db_name} 不存在",
            "请在 phpStudy 中新建同名数据库，或运行 python scripts/init_db.py 自动创建。",
            code,
        )
    if code in (2003, 2002):
        return DatabaseError(
            f"无法连接 MySQL（{settings.db_host}:{settings.db_port}）",
            "请确认小皮面板（phpStudy）中的 MySQL 已启动，且 3306 端口未被占用。",
            code,
        )
    if code == 1146:
        return DatabaseError(
            "数据表不存在，尚未初始化",
            "请执行 sql/schema.sql，或运行 python scripts/init_db.py 自动建库建表。",
            code,
        )
    if code == 1044:
        return DatabaseError(
            "当前 MySQL 账号没有访问该数据库的权限",
            "请在小皮面板中给该账号授权，或改用 root 账号。",
            code,
        )
    if code == 1054:
        return DatabaseError(
            "数据表结构与代码不匹配（字段缺失）",
            "请重新执行 sql/schema.sql（或删除旧表后重建）。",
            code,
        )
    return DatabaseError(f"数据库操作失败：{exc}", "请查看运行终端中的完整报错堆栈。", code)


def _wrap_errors(fn):
    @functools.wraps(fn)
    def inner(self, *args, **kwargs):
        try:
            return fn(self, *args, **kwargs)
        except DatabaseError:
            raise
        except pymysql.MySQLError as exc:
            raise _translate_error(exc, self.settings) from exc

    return inner


class MySQLPool:
    """线程安全的 MySQL 连接池。"""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._max_size = max(1, int(settings.db_pool_size))
        self._idle: "queue.LifoQueue[pymysql.connections.Connection]" = queue.LifoQueue(
            maxsize=self._max_size
        )
        self._permits = threading.BoundedSemaphore(self._max_size)
        self._state_lock = threading.Lock()
        self._closed = False
        self._gallery_cache: Optional[List[dict]] = None
        self._gallery_cached_at = 0.0

    # ------------------------------------------------------------------ 连接池
    def _connect(self) -> pymysql.connections.Connection:
        try:
            conn = pymysql.connect(
                host=self.settings.db_host,
                port=self.settings.db_port,
                user=self.settings.db_user,
                password=self.settings.db_password,
                database=self.settings.db_name,
                charset=self.settings.db_charset,
                cursorclass=DictCursor,
                autocommit=True,
                connect_timeout=5,
                read_timeout=15,
                write_timeout=15,
                init_command="SET NAMES utf8mb4 COLLATE utf8mb4_unicode_ci",
            )
            return conn
        except pymysql.MySQLError as exc:
            raise _translate_error(exc, self.settings) from exc

    def _acquire(self) -> pymysql.connections.Connection:
        if self._closed:
            raise DatabaseError("数据库连接池已关闭", "请重启服务。")

        conn = None
        try:
            conn = self._idle.get_nowait()
        except queue.Empty:
            conn = None

        if conn is not None:
            try:
                conn.ping(reconnect=True)
                return conn
            except pymysql.MySQLError:
                self._discard(conn, release_permit=True)

        if not self._permits.acquire(timeout=5):
            raise DatabaseError(
                "数据库连接池繁忙，等待超时",
                "请稍后重试，或在 .env 中调大 DB_POOL_SIZE。",
            )
        try:
            return self._connect()
        except Exception:
            self._permits.release()
            raise

    def _release(self, conn: pymysql.connections.Connection, healthy: bool = True) -> None:
        if healthy and not self._closed:
            try:
                self._idle.put_nowait(conn)
                return
            except queue.Full:
                pass
        self._discard(conn, release_permit=True)

    def _discard(self, conn: pymysql.connections.Connection, release_permit: bool) -> None:
        try:
            conn.close()
        except Exception:
            pass
        if release_permit:
            try:
                self._permits.release()
            except ValueError:
                pass

    @contextmanager
    def connection(self):
        conn = self._acquire()
        healthy = True
        try:
            yield conn
        except Exception:
            healthy = False
            raise
        finally:
            self._release(conn, healthy)

    @contextmanager
    def transaction(self):
        conn = self._acquire()
        healthy = True
        try:
            conn.begin()
            with conn.cursor() as cur:
                yield cur
            conn.commit()
        except Exception:
            healthy = False
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._release(conn, healthy)

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                conn = self._idle.get_nowait()
            except queue.Empty:
                break
            self._discard(conn, release_permit=True)

    # ------------------------------------------------------------------ 健康检查
    @_wrap_errors
    def health(self) -> dict:
        info = {
            "ok": True,
            "message": "数据库连接正常",
            "hint": "",
            "version": "",
            "database": self.settings.db_name,
            "tables_ok": False,
            "missing_tables": [],
        }
        try:
            with self.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT VERSION() AS version")
                    row = cur.fetchone() or {}
                    info["version"] = str(row.get("version", ""))
                    cur.execute(
                        "SELECT TABLE_NAME AS name FROM information_schema.TABLES "
                        "WHERE TABLE_SCHEMA = %s",
                        (self.settings.db_name,),
                    )
                    names = {str(r["name"]) for r in (cur.fetchall() or [])}
        except DatabaseError as exc:
            return {
                "ok": False,
                "message": exc.message,
                "hint": exc.hint,
                "version": "",
                "database": self.settings.db_name,
                "tables_ok": False,
                "missing_tables": list(REQUIRED_TABLES),
            }

        missing = [t for t in REQUIRED_TABLES if t not in names]
        info["missing_tables"] = missing
        info["tables_ok"] = not missing
        if missing:
            info["ok"] = False
            info["message"] = "数据表不完整：" + "、".join(missing)
            info["hint"] = "运行 python scripts/init_db.py 或手动执行 sql/schema.sql。"
        return info

    # ------------------------------------------------------------------ 员工 / 特征
    @_wrap_errors
    def list_employees(self) -> List[dict]:
        sql = (
            "SELECT e.id, e.emp_no, e.name, e.department, e.active, e.created_at, "
            "       COUNT(f.id) AS samples "
            "FROM employees e "
            "LEFT JOIN face_encodings f ON f.employee_id = e.id "
            "GROUP BY e.id "
            "ORDER BY e.id ASC"
        )
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() or []
        result = []
        for row in rows:
            item = dict(row)
            if isinstance(item.get("created_at"), datetime):
                item["created_at"] = item["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            result.append(item)
        return result

    @_wrap_errors
    def fetch_gallery(self, force: bool = False) -> List[dict]:
        """读取全部人脸特征（带 15s 缓存），返回 [{employee..., vector: np.ndarray}]。"""
        now = time.monotonic()
        if (
            not force
            and self._gallery_cache is not None
            and now - self._gallery_cached_at < GALLERY_TTL_SECONDS
        ):
            return self._gallery_cache

        sql = (
            "SELECT e.id AS employee_id, e.emp_no, e.name, e.department, "
            "       f.encoding, f.dims "
            "FROM face_encodings f "
            "JOIN employees e ON e.id = f.employee_id "
            "WHERE e.active = 1 "
            "ORDER BY e.id ASC, f.sample_index ASC"
        )
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
                rows = cur.fetchall() or []

        gallery: List[dict] = []
        for row in rows:
            try:
                dims = int(row.get("dims") or 128)
                vector = blob_to_vector(row["encoding"], dims)
            except (ValueError, TypeError):
                continue
            gallery.append(
                {
                    "employee_id": int(row["employee_id"]),
                    "emp_no": row["emp_no"],
                    "name": row["name"],
                    "department": row.get("department") or "",
                    "vector": vector,
                }
            )
        self._gallery_cache = gallery
        self._gallery_cached_at = now
        return gallery

    def invalidate_gallery(self) -> None:
        self._gallery_cache = None
        self._gallery_cached_at = 0.0

    @_wrap_errors
    def save_employee_with_encodings(
        self,
        emp_no: str,
        name: str,
        department: str,
        vectors: Iterable[np.ndarray],
        model: str = "dlib_resnet_v1",
        replace: bool = True,
    ) -> dict:
        vectors = [np.asarray(v, dtype=np.float64).reshape(-1) for v in vectors]
        vectors = [v for v in vectors if v.size == 128]
        if not vectors:
            raise DatabaseError("没有可保存的有效人脸特征", "请重新采集，确保画面中只有一张清晰正脸。")

        with self.transaction() as cur:
            cur.execute(
                "INSERT INTO employees (emp_no, name, department) VALUES (%s, %s, %s) "
                "ON DUPLICATE KEY UPDATE name = VALUES(name), department = VALUES(department)",
                (emp_no, name, department),
            )
            cur.execute("SELECT id FROM employees WHERE emp_no = %s", (emp_no,))
            row = cur.fetchone()
            if not row:
                raise DatabaseError("员工写入失败", "请检查 employees 表结构与索引。")
            employee_id = int(row["id"])

            if replace:
                cur.execute("DELETE FROM face_encodings WHERE employee_id = %s", (employee_id,))
            cur.execute(
                "SELECT COALESCE(MAX(sample_index), -1) AS max_index "
                "FROM face_encodings WHERE employee_id = %s",
                (employee_id,),
            )
            row = cur.fetchone() or {}
            start = int(row.get("max_index", -1)) + 1

            for offset, vector in enumerate(vectors):
                cur.execute(
                    "INSERT INTO face_encodings (employee_id, encoding, dims, model, sample_index) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (employee_id, vector_to_blob(vector), 128, model, start + offset),
                )

        self.invalidate_gallery()
        return {
            "id": employee_id,
            "emp_no": emp_no,
            "name": name,
            "department": department,
            "samples": len(vectors),
        }

    # ------------------------------------------------------------------ 打卡
    @_wrap_errors
    def record_attendance(
        self,
        employee: dict,
        now: datetime,
        settings: Settings,
        liveness_score: float,
        match_distance: float,
    ) -> dict:
        """事务内完成：防重复 -> 判定状态 -> 写记录。

        返回 {"duplicate": bool, "record": {...}, "decision": {...}}
        """
        employee_id = int(employee["employee_id"])
        work_date = now.date()
        window_start = now - timedelta(seconds=max(0, settings.duplicate_window_seconds))

        with self.transaction() as cur:
            # 锁住员工行，串行化同一员工的并发打卡
            cur.execute("SELECT id FROM employees WHERE id = %s FOR UPDATE", (employee_id,))
            if not cur.fetchone():
                raise DatabaseError("员工不存在或已被删除", "请重新录入该员工的人脸信息。")

            cur.execute(
                "SELECT id, punch_time, punch_type, status FROM attendance_records "
                "WHERE employee_id = %s AND punch_time >= %s AND punch_time <= %s "
                "ORDER BY punch_time DESC LIMIT 1",
                (employee_id, window_start, now),
            )
            recent = cur.fetchone()
            if recent:
                return {
                    "duplicate": True,
                    "record": {
                        "id": int(recent["id"]),
                        "punch_time": recent["punch_time"],
                        "punch_type": recent["punch_type"],
                        "status": recent["status"],
                    },
                    "decision": None,
                }

            cur.execute(
                "SELECT punch_type FROM attendance_records "
                "WHERE employee_id = %s AND work_date = %s",
                (employee_id, work_date),
            )
            existing = {str(r["punch_type"]) for r in (cur.fetchall() or [])}

            decision = decide_punch(
                existing,
                now,
                work_start=settings.work_start,
                work_end=settings.work_end,
                grace_minutes=settings.late_grace_minutes,
                auto_punch_type=settings.auto_punch_type,
                punch_split=settings.punch_split,
            )

            cur.execute(
                "INSERT INTO attendance_records "
                "(employee_id, emp_no, employee_name, department, work_date, punch_type, "
                " punch_time, status, liveness_score, match_distance, device) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    employee_id,
                    employee.get("emp_no", ""),
                    employee.get("name", ""),
                    employee.get("department", ""),
                    work_date,
                    decision.punch_type,
                    now,
                    decision.status,
                    round(float(liveness_score), 4),
                    round(float(match_distance), 4),
                    "web",
                ),
            )
            record_id = int(cur.lastrowid)

        self.invalidate_gallery()
        return {
            "duplicate": False,
            "record": {
                "id": record_id,
                "punch_time": now,
                "punch_type": decision.punch_type,
                "status": decision.status,
            },
            "decision": decision.to_dict(),
        }

    @_wrap_errors
    def records_for_date(self, work_date, limit: int = 100) -> List[dict]:
        sql = (
            "SELECT id, emp_no, employee_name, department, punch_type, punch_time, "
            "       status, liveness_score, match_distance "
            "FROM attendance_records WHERE work_date = %s "
            "ORDER BY punch_time DESC LIMIT %s"
        )
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (work_date, int(limit)))
                rows = cur.fetchall() or []
        return [self._serialize_record(r) for r in rows]

    @staticmethod
    def _serialize_record(row: dict) -> dict:
        item = dict(row)
        punch_time = item.get("punch_time")
        if isinstance(punch_time, datetime):
            item["punch_time"] = punch_time.strftime("%Y-%m-%d %H:%M:%S")
            item["time"] = punch_time.strftime("%H:%M:%S")
        if item.get("liveness_score") is not None:
            item["liveness_score"] = round(float(item["liveness_score"]), 4)
        if item.get("match_distance") is not None:
            item["match_distance"] = round(float(item["match_distance"]), 4)
        return item

    @_wrap_errors
    def overview(self, today) -> dict:
        with self.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) AS c FROM employees WHERE active = 1")
                employee_count = int((cur.fetchone() or {}).get("c", 0))
                cur.execute(
                    "SELECT COUNT(DISTINCT employee_id) AS c FROM attendance_records "
                    "WHERE work_date = %s",
                    (today,),
                )
                today_count = int((cur.fetchone() or {}).get("c", 0))
        records = self.records_for_date(today, limit=30)
        return {
            "employee_count": employee_count,
            "today_count": today_count,
            "records": records,
        }

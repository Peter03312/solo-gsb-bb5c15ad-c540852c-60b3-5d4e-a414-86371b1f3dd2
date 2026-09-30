"""FastAPI 应用：路跑分段计时裁决服务。

接口总览（前缀 /api/v1）：
    POST   /races                  创建赛事（配置创建后固定）
    GET    /races/{race_id}        查询赛事
    POST   /races/{race_id}/seals  封存赛事（幂等，返回封存后成绩快照）
    POST   /scans                  提交扫描（乱序可，幂等/冲突判定）
    DELETE /scans/{scan_code}      撤销扫描（仅封存前）
    GET    /races/{race_id}/results 查询暂定/封存成绩
    GET    /healthz                健康检查
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse

from .db import connect, wait_for_database
from .schemas import RaceCreate, ScanCreate
from .timing import adjudicate


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时等待数据库就绪（compose 已配 healthcheck，这里再兜底）。
    wait_for_database()
    yield


app = FastAPI(title="路跑分段计时裁决服务", version="1.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def utc(dt: datetime) -> datetime:
    """把无时区时间按 UTC 处理，统一为带时区时刻。"""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def get_conn():
    """每请求一个连接，正常结束提交、异常回滚。"""
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def row_to_scan(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "scan_id": row["scan_code"],
        "race_id": row["race_id"],
        "runner_id": row["runner_id"],
        "point_code": row["point_code"],
        "occurred_at": row["occurred_at"],
        "revoked": row["revoked"],
    }


def fetch_race_config(conn, race_id: uuid.UUID) -> tuple[dict, list[dict]]:
    race = conn.execute("SELECT * FROM races WHERE id = %s", (race_id,)).fetchone()
    if race is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "赛事不存在")
    points = conn.execute(
        """
        SELECT code, name, position, lower_seconds, upper_seconds
          FROM points
         WHERE race_id = %s
         ORDER BY position
        """,
        (race_id,),
    ).fetchall()
    return race, points


def compute_results(conn, race_id: uuid.UUID) -> list[dict[str, Any]]:
    """对赛事下所有选手重算暂定成绩（不写库）。"""
    race, points = fetch_race_config(conn, race_id)
    start = utc(race["start_time"]).timestamp()
    cutoff = utc(race["cutoff_time"]).timestamp()
    point_codes = [p["code"] for p in points]
    bounds = [(float(p["lower_seconds"]), float(p["upper_seconds"])) for p in points]

    runners = conn.execute(
        "SELECT * FROM runners WHERE race_id = %s ORDER BY bib", (race_id,)
    ).fetchall()

    results: list[dict[str, Any]] = []
    for runner in runners:
        scan_rows = conn.execute(
            """
            SELECT * FROM scans
             WHERE race_id = %s AND runner_id = %s AND revoked = FALSE
            """,
            (race_id, runner["id"]),
        ).fetchall()
        candidates = [
            (str(s["id"]), s["point_code"], utc(s["occurred_at"]).timestamp())
            for s in scan_rows
        ]
        path, used_ids = adjudicate(point_codes, bounds, candidates, start, cutoff)

        used_scans: list[dict[str, Any]] = []
        finish_time = None
        total_seconds = None
        if path is not None:
            order = {sid: i for i, sid in enumerate(used_ids)}
            chosen = [s for s in scan_rows if str(s["id"]) in order]
            chosen.sort(key=lambda s: order[str(s["id"])])
            used_scans = [row_to_scan(s) for s in chosen]
            finish_time = datetime.fromtimestamp(path.time, tz=timezone.utc)
            total_seconds = round(path.time - start, 3)

        results.append(
            {
                "race_id": race_id,
                "runner_id": runner["id"],
                "bib": runner["bib"],
                "name": runner["name"],
                "chip_code": runner["chip_code"],
                "finished": path is not None,
                "finish_time": finish_time,
                "total_seconds": total_seconds,
                "scans": used_scans,
            }
        )
    return results


# --------------------------------------------------------------------------- #
# 唯一约束冲突：重复芯片 -> 400 拒收；扫描编号冲突 -> 409
# --------------------------------------------------------------------------- #
@app.exception_handler(psycopg.errors.UniqueViolation)
async def unique_violation_handler(request: Request,
                                   exc: psycopg.errors.UniqueViolation):
    constraint = (exc.diag.constraint_name or "").lower()
    # 并发创建赛事时重复绑定芯片属于“重复芯片拒收”（400）；
    # 并发提交同一扫描编号由活动编号唯一索引兜底，按冲突处理（409）。
    if "chip" in constraint:
        code, detail = status.HTTP_400_BAD_REQUEST, "芯片码重复绑定，拒收"
    else:
        code, detail = status.HTTP_409_CONFLICT, "扫描编号冲突（可能为并发重复提交）"
    return JSONResponse(status_code=code, content={"detail": detail})


# --------------------------------------------------------------------------- #
# 健康检查
# --------------------------------------------------------------------------- #
@app.get("/healthz", tags=["system"])
def healthz(conn=Depends(get_conn)):
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 赛事
# --------------------------------------------------------------------------- #
@app.post("/api/v1/races", status_code=status.HTTP_201_CREATED, tags=["races"])
def create_race(payload: RaceCreate, conn=Depends(get_conn)):
    start = utc(payload.start_time)
    cutoff = utc(payload.cutoff_time)
    if cutoff <= start:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "截止时间必须晚于起跑时间")

    # 理论最早完赛 = 起跑 + 各段下限之和；不得晚于截止时间。
    min_total = sum(p.lower_seconds for p in payload.points)
    if start.timestamp() + min_total > cutoff.timestamp():
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "区间不可行：各段下限之和已使最早完赛晚于截止时间",
        )

    # 芯片码全局唯一（跨赛事也不能重复绑定），提前给出明确错误。
    codes = [c.chip_code for c in payload.chips]
    dup = conn.execute(
        "SELECT chip_code FROM runners WHERE chip_code = ANY(%s)", (codes,)
    ).fetchall()
    if dup:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"芯片码已被其他赛事绑定: {[r['chip_code'] for r in dup]}",
        )

    race_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO races (id, name, start_time, cutoff_time)
        VALUES (%s, %s, %s, %s)
        """,
        (race_id, payload.name, start, cutoff),
    )
    for position, p in enumerate(payload.points, start=1):
        conn.execute(
            """
            INSERT INTO points (race_id, position, code, name,
                                lower_seconds, upper_seconds)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (race_id, position, p.code, p.name, p.lower_seconds, p.upper_seconds),
        )
    for c in payload.chips:
        conn.execute(
            """
            INSERT INTO runners (id, race_id, bib, name, chip_code)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (uuid.uuid4(), race_id, c.bib, c.name, c.chip_code),
        )

    return {
        "id": race_id,
        "name": payload.name,
        "start_time": start,
        "cutoff_time": cutoff,
        "sealed": False,
        "sealed_at": None,
    }


@app.get("/api/v1/races/{race_id}", tags=["races"])
def get_race(race_id: uuid.UUID, conn=Depends(get_conn)):
    race, _ = fetch_race_config(conn, race_id)
    return {
        "id": race["id"],
        "name": race["name"],
        "start_time": race["start_time"],
        "cutoff_time": race["cutoff_time"],
        "sealed": race["sealed"],
        "sealed_at": race["sealed_at"],
    }


@app.post("/api/v1/races/{race_id}/seals", tags=["races"])
def seal_race(race_id: uuid.UUID, conn=Depends(get_conn)):
    """封存赛事：固化全部选手成绩快照；重复封存幂等返回已有快照。"""
    race, _ = fetch_race_config(conn, race_id)
    if race["sealed"]:
        return {
            "race_id": race_id,
            "sealed": True,
            "sealed_at": race["sealed_at"],
            "results": _read_snapshots(conn, race_id),
        }

    # 行锁串行化封存，与并发提交/撤销互斥。
    conn.execute("SELECT id FROM races WHERE id = %s FOR UPDATE", (race_id,))
    results = compute_results(conn, race_id)

    for r in results:
        conn.execute(
            """
            INSERT INTO results (race_id, runner_id, finished, finish_time,
                                 total_seconds, scan_ids, scan_codes)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                race_id,
                r["runner_id"],
                r["finished"],
                r["finish_time"],
                r["total_seconds"],
                [s["id"] for s in r["scans"]],
                [s["scan_id"] for s in r["scans"]],
            ),
        )
    sealed_at = datetime.now(timezone.utc)
    conn.execute(
        "UPDATE races SET sealed = TRUE, sealed_at = %s WHERE id = %s",
        (sealed_at, race_id),
    )
    return {"race_id": race_id, "sealed": True, "sealed_at": sealed_at,
            "results": results}


def _read_snapshots(conn, race_id: uuid.UUID) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT rs.*, r.bib, r.name, r.chip_code
          FROM results rs
          JOIN runners r ON r.id = rs.runner_id
         WHERE rs.race_id = %s
         ORDER BY r.bib
        """,
        (race_id,),
    ).fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        scans: list[dict[str, Any]] = []
        if row["scan_ids"]:
            scan_rows = conn.execute(
                "SELECT * FROM scans WHERE id = ANY(%s)",
                (row["scan_ids"],),
            ).fetchall()
            order = {sid: i for i, sid in enumerate(row["scan_ids"])}
            scan_rows.sort(key=lambda s: order[s["id"]])
            scans = [row_to_scan(s) for s in scan_rows]
        output.append(
            {
                "race_id": race_id,
                "runner_id": row["runner_id"],
                "bib": row["bib"],
                "name": row["name"],
                "chip_code": row["chip_code"],
                "finished": row["finished"],
                "finish_time": row["finish_time"],
                "total_seconds": float(row["total_seconds"])
                if row["total_seconds"] is not None else None,
                "scans": scans,
            }
        )
    return output


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
@app.post("/api/v1/scans", status_code=status.HTTP_201_CREATED, tags=["scans"])
def submit_scan(payload: ScanCreate, conn=Depends(get_conn)):
    runner = conn.execute(
        "SELECT * FROM runners WHERE chip_code = %s", (payload.chip_code,)
    ).fetchone()
    if runner is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知芯片，拒收")
    race_id = runner["race_id"]

    point = conn.execute(
        "SELECT code FROM points WHERE race_id = %s AND code = %s",
        (race_id, payload.point_code),
    ).fetchone()
    if point is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知点位，拒收")

    # 先做幂等判定：同编号同内容重传不改变任何状态，
    # 即使赛事已封存也照常返回（200）。扫描编号全局唯一。
    occurred = utc(payload.occurred_at)
    existing = conn.execute(
        """
        SELECT * FROM scans
         WHERE scan_code = %s AND revoked = FALSE
        """,
        (payload.scan_id,),
    ).fetchone()
    if existing is not None:
        if existing["race_id"] != race_id:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "扫描编号已被其他赛事使用，冲突拒收",
            )
        same = (
            existing["runner_id"] == runner["id"]
            and existing["point_code"] == payload.point_code
            and utc(existing["occurred_at"]) == occurred
        )
        if same:
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=row_to_scan(existing)
            )
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "扫描编号已存在但内容不同（芯片/点位/时间），冲突拒收",
        )

    # 锁定赛事行：与封存互斥；封存后拒绝一切新增。
    race = conn.execute(
        "SELECT sealed FROM races WHERE id = %s FOR UPDATE", (race_id,)
    ).fetchone()
    if race["sealed"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "赛事已封存，拒绝提交")

    new_id = uuid.uuid4()
    conn.execute(
        """
        INSERT INTO scans (id, scan_code, race_id, runner_id, point_code,
                           occurred_at)
        VALUES (%s, %s, %s, %s, %s, %s)
        """,
        (new_id, payload.scan_id, race_id, runner["id"],
         payload.point_code, occurred),
    )
    row = conn.execute("SELECT * FROM scans WHERE id = %s", (new_id,)).fetchone()
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=row_to_scan(row))


@app.delete("/api/v1/scans/{scan_code}", tags=["scans"])
def revoke_scan(
    scan_code: str,
    chip_code: str | None = None,
    race_id: uuid.UUID | None = None,
    conn=Depends(get_conn),
):
    """撤销扫描（封存前），暂定成绩随之重算。

    scan_code 在全局活动扫描中唯一；用查询参数 chip_code（经芯片定位赛事）
    或 race_id 指定赛事，二者至少给一个。
    """
    if chip_code is None and race_id is None:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            "请通过 chip_code 或 race_id 指定扫描所属赛事",
        )

    if race_id is None:
        runner = conn.execute(
            "SELECT race_id FROM runners WHERE chip_code = %s", (chip_code,)
        ).fetchone()
        if runner is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "未知芯片")
        race_id = runner["race_id"]

    race = conn.execute(
        "SELECT sealed FROM races WHERE id = %s FOR UPDATE", (race_id,)
    ).fetchone()
    if race is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "赛事不存在")
    if race["sealed"]:
        raise HTTPException(status.HTTP_409_CONFLICT, "赛事已封存，拒绝撤销")

    row = conn.execute(
        """
        UPDATE scans SET revoked = TRUE
         WHERE race_id = %s AND scan_code = %s AND revoked = FALSE
        RETURNING *
        """,
        (race_id, scan_code),
    ).fetchone()
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "扫描不存在或已撤销")
    return {"revoked": True, "scan": row_to_scan(row)}


# --------------------------------------------------------------------------- #
# 成绩
# --------------------------------------------------------------------------- #
@app.get("/api/v1/races/{race_id}/results", tags=["results"])
def get_results(race_id: uuid.UUID, conn=Depends(get_conn)):
    """成绩查询：封存后返回固定快照，封存前实时重算暂定成绩。"""
    race, _ = fetch_race_config(conn, race_id)
    if race["sealed"]:
        return {
            "race_id": race_id,
            "sealed": True,
            "sealed_at": race["sealed_at"],
            "results": _read_snapshots(conn, race_id),
        }
    return {
        "race_id": race_id,
        "sealed": False,
        "sealed_at": None,
        "results": compute_results(conn, race_id),
    }

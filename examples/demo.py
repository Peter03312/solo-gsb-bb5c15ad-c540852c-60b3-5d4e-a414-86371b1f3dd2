#!/usr/bin/env python3
"""端到端调用示例：走通完整赛事裁决生命周期。

仅依赖标准库。用法：
    python examples/demo.py [API_BASE_URL]
默认 http://127.0.0.1:8000

场景：
  08:00 起跑，CP1/CP2/FINISH 三段各允许 290~320 秒，截止 09:00。
  - 选手 A：CP1 有两条候选扫描（08:05:00 与 08:05:10），CP2/FIN 各只有
    一条（08:10:05、08:15:05）。两条路径同成绩（08:15:05 完赛），
    演示“同成绩取扫描编号序列字典序最小”，选到编号更小的首点扫描；
    撤销该首点扫描后，暂定成绩自动重算为另一条路径。
  - 选手 B：只有首点扫描，未完赛。
  - 重传：同编号同内容幂等；异内容返回 409；未知芯片/点位返回 400。
  - 封存：固化快照，之后提交/撤销返回 409，成绩不再变化。
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
API = f"{BASE}/api/v1"


def call(method: str, path: str, body: dict | None = None,
         allow_status: set[int] | None = None) -> tuple[int, dict]:
    url = f"{API}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if allow_status and exc.code in allow_status:
            return exc.code, json.loads(exc.read() or b"{}")
        raise


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n=== {title} [{status}] ===")
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def find_result(results: list[dict], chip: str) -> dict:
    return next(r for r in results if r["chip_code"] == chip)


def main() -> None:
    # 1) 创建赛事 ---------------------------------------------------------- #
    race_body = {
        "name": "2026 示例十公里",
        "start_time": "2026-09-30T08:00:00+08:00",
        "cutoff_time": "2026-09-30T09:00:00+08:00",
        "points": [
            {"code": "CP1", "name": "检查点1", "lower_seconds": 290, "upper_seconds": 320},
            {"code": "CP2", "name": "检查点2", "lower_seconds": 290, "upper_seconds": 320},
            {"code": "FIN", "name": "终点",    "lower_seconds": 290, "upper_seconds": 320},
        ],
        "chips": [
            {"bib": "A001", "name": "选手甲", "chip_code": "CHIP-A"},
            {"bib": "B001", "name": "选手乙", "chip_code": "CHIP-B"},
        ],
    }
    status, race = call("POST", "/races", race_body)
    show("创建赛事", status, race)
    race_id = race["id"]

    # 2) 提交扫描（故意乱序：先到的晚提交） -------------------------------- #
    scans = [
        # 选手 A：路径 1（首点编号字典序较小），完赛 08:15:05
        ("S-A1-CP1", "CHIP-A", "CP1", "2026-09-30T08:05:00+08:00"),
        ("S-A2-CP1", "CHIP-A", "CP1", "2026-09-30T08:05:10+08:00"),
        ("S-A-CP2", "CHIP-A", "CP2", "2026-09-30T08:10:05+08:00"),
        ("S-A-FIN", "CHIP-A", "FIN", "2026-09-30T08:15:05+08:00"),
        # 选手 B：只有首点
        ("S-B1-CP1", "CHIP-B", "CP1", "2026-09-30T08:07:00+08:00"),
    ]
    # 打乱提交顺序，演示“可乱序提交”。
    for scan_id, chip, point, at in [scans[4], scans[2], scans[0], scans[3],
                                     scans[1]]:
        st, out = call("POST", "/scans",
                       {"scan_id": scan_id, "chip_code": chip,
                        "point_code": point, "occurred_at": at})
        print(f"提交 {scan_id}: {st}")

    # 3) 幂等与冲突 -------------------------------------------------------- #
    st, out = call("POST", "/scans",
                   {"scan_id": "S-A1-CP1", "chip_code": "CHIP-A",
                    "point_code": "CP1", "occurred_at": "2026-09-30T08:05:00+08:00"})
    print(f"\n同编号同内容重传 -> {st}（200 表示幂等命中）")

    st, out = call("POST", "/scans",
                   {"scan_id": "S-A1-CP1", "chip_code": "CHIP-A",
                    "point_code": "CP1", "occurred_at": "2026-09-30T08:05:01+08:00"},
                   allow_status={409})
    print(f"同编号异内容重传 -> {st}: {out['detail']}")

    st, out = call("POST", "/scans",
                   {"scan_id": "X", "chip_code": "NO-SUCH-CHIP",
                    "point_code": "CP1", "occurred_at": "2026-09-30T08:05:00+08:00"},
                   allow_status={400})
    print(f"未知芯片 -> {st}: {out['detail']}")

    st, out = call("POST", "/scans",
                   {"scan_id": "X", "chip_code": "CHIP-A",
                    "point_code": "MARS", "occurred_at": "2026-09-30T08:05:00+08:00"},
                   allow_status={400})
    print(f"未知点位 -> {st}: {out['detail']}")

    # 4) 查询暂定成绩：平局取字典序最小（首点选 S-A1-CP1） ------------------ #
    st, results = call("GET", f"/races/{race_id}/results")
    ra = find_result(results["results"], "CHIP-A")
    show("暂定成绩-选手甲（首点应为 S-A1-CP1）", st, ra)
    rb = find_result(results["results"], "CHIP-B")
    show("暂定成绩-选手乙（未完赛）", st, rb)

    # 5) 撤销 S-A1-CP1，成绩应重算，首点改为 S-A2-CP1 ---------------------- #
    st, out = call("DELETE", "/scans/S-A1-CP1?chip_code=CHIP-A")
    show("撤销 S-A1-CP1", st, out)

    st, results = call("GET", f"/races/{race_id}/results")
    ra = find_result(results["results"], "CHIP-A")
    show("重算后-选手甲（首点应改为 S-A2-CP1）", st, ra)

    # 6) 封存 -------------------------------------------------------------- #
    st, sealed = call("POST", f"/races/{race_id}/seals")
    show("封存赛事", st, {"sealed": sealed["sealed"],
                          "sealed_at": sealed["sealed_at"]})

    # 7) 封存后拒绝提交与撤销 ---------------------------------------------- #
    st, out = call("POST", "/scans",
                   {"scan_id": "LATE", "chip_code": "CHIP-B",
                    "point_code": "CP1", "occurred_at": "2026-09-30T08:20:00+08:00"},
                   allow_status={409})
    print(f"\n封存后提交 -> {st}: {out['detail']}")

    st, out = call("DELETE", "/scans/S-A2-CP1?chip_code=CHIP-A",
                   allow_status={409})
    print(f"封存后撤销 -> {st}: {out['detail']}")

    # 8) 封存后成绩返回固定快照 -------------------------------------------- #
    st, results = call("GET", f"/races/{race_id}/results")
    show("封存快照", st, results)


if __name__ == "__main__":
    main()

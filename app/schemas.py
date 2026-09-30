"""请求模型（Pydantic v2）。响应直接以字典返回。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, field_validator


class SegmentWindow(BaseModel):
    """一个点位及其“从上一节点到本点”的耗时闭区间（秒）。"""

    code: str = Field(..., min_length=1, description="点位代码，全赛事唯一")
    name: str = Field(..., min_length=1, description="点位名称")
    lower_seconds: float = Field(..., ge=0, description="本段耗时下限（含），非负")
    upper_seconds: float = Field(..., ge=0, description="本段耗时上限（含）")


class ChipBinding(BaseModel):
    """选手芯片绑定。"""

    bib: str = Field(..., min_length=1)
    name: str = Field(..., min_length=1)
    chip_code: str = Field(..., min_length=1)


class RaceCreate(BaseModel):
    name: str = Field(..., min_length=1)
    start_time: datetime = Field(..., description="起跑（枪响）时间，ISO-8601")
    cutoff_time: datetime = Field(..., description="成绩截止时间，ISO-8601")
    points: list[SegmentWindow] = Field(..., min_length=1, description="有序检查点，末尾为终点")
    chips: list[ChipBinding] = Field(..., min_length=1)

    @field_validator("points")
    @classmethod
    def _validate_points(cls, points: list[SegmentWindow]) -> list[SegmentWindow]:
        codes = [p.code for p in points]
        if len(codes) != len(set(codes)):
            raise ValueError("点位代码重复")
        for p in points:
            if p.upper_seconds < p.lower_seconds:
                raise ValueError(f"点位 {p.code} 的耗时区间下限大于上限")
        return points

    @field_validator("chips")
    @classmethod
    def _validate_chips(cls, chips: list[ChipBinding]) -> list[ChipBinding]:
        chip_codes = [c.chip_code for c in chips]
        if len(chip_codes) != len(set(chip_codes)):
            raise ValueError("芯片码重复")
        bibs = [c.bib for c in chips]
        if len(bibs) != len(set(bibs)):
            raise ValueError("选手号码重复")
        return chips


class ScanCreate(BaseModel):
    scan_id: str = Field(..., min_length=1, description="扫描唯一编号")
    chip_code: str = Field(..., min_length=1)
    point_code: str = Field(..., min_length=1)
    occurred_at: datetime = Field(..., description="扫描发生时间，ISO-8601；提交顺序任意")

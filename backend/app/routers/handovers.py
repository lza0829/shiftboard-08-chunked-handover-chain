from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services import handover as svc

router = APIRouter(prefix="/handovers", tags=["交班"])


class StartBody(BaseModel):
    from_shift_id: int = Field(..., description="交班班次")
    to_shift_id: int = Field(..., description="接班班次")
    segment_size: int | None = Field(
        default=None, description="分段上限 N（≥1）：每段至多 N 键；缺省为单段全量"
    )


@router.get("")
def get_handovers():
    return svc.list_handovers()


@router.get("/{plan_id}")
def get_one(plan_id: int):
    return svc.get_handover(plan_id)


@router.post("/start")
def start(body: StartBody):
    return svc.start_handover(body.from_shift_id, body.to_shift_id, body.segment_size)


@router.post("/{plan_id}/complete")
def complete(plan_id: int):
    """按序完成剩余全部段（每段独立事务提交，任一段失败即停）。"""
    return svc.complete_handover(plan_id)


@router.post("/{plan_id}/segments/{seq}/complete")
def complete_segment(plan_id: int, seq: int):
    """完成指定段：仅当之前所有段已完成才允许办理。"""
    return svc.complete_segment(plan_id, seq)

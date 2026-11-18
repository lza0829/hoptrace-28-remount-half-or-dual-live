from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import connect
from app.modules.waybill_key_remount import (
    RemountError,
    get_proof,
    list_edges,
    remount_waybill_key,
)

router = APIRouter(prefix="/api", tags=["waybill-key-remount"])


class RemountRequest(BaseModel):
    old_key: str
    new_key: str
    new_title: str | None = None


@router.post("/remount")
def run_remount(req: RemountRequest):
    """同事务执行运单键重挂；冲突或中途失败整体回滚并返回 409。"""
    conn = connect()
    try:
        edge = remount_waybill_key(conn, req.old_key, req.new_key, new_title=req.new_title)
        return {"ok": True, "edge": edge}
    except RemountError as e:
        raise HTTPException(status_code=409, detail=str(e))
    finally:
        conn.close()


@router.get("/remounts")
def remount_edges():
    """迁移边审计列表（不可变、只追加）。"""
    conn = connect()
    try:
        return list_edges(conn)
    finally:
        conn.close()


@router.get("/remounts/{edge_id}/proof")
def remount_proof(edge_id: int):
    """重挂证明只读接口：旧键、新键、迁入点数、新旧投影与全序。"""
    conn = connect()
    try:
        proof = get_proof(conn, edge_id)
        if not proof:
            raise HTTPException(404, "迁移边不存在")
        return proof
    finally:
        conn.close()

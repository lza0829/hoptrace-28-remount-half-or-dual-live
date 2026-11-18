from fastapi import APIRouter, HTTPException

from app.db import connect
from app.services import ingest as ingest_svc
from app.services import timeline as timeline_svc

router = APIRouter(prefix="/api")


@router.get("/overview")
def overview():
    conn = connect()
    try:
        return {
            "waybills": conn.execute(
                "SELECT COUNT(*) AS c FROM waybills WHERE COALESCE(retired,0)=0"
            ).fetchone()["c"],
            "waybills_retired": conn.execute(
                "SELECT COUNT(*) AS c FROM waybills WHERE COALESCE(retired,0)=1"
            ).fetchone()["c"],
            "hops_primary": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='primary' AND retracted=0"
            ).fetchone()["c"],
            "hops_sidecar": conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE channel='sidecar'"
            ).fetchone()["c"],
            "quarantine": conn.execute("SELECT COUNT(*) AS c FROM quarantine").fetchone()["c"],
        }
    finally:
        conn.close()


@router.get("/waybills")
def list_waybills():
    conn = connect()
    try:
        # 运单索引协同：退休旧键不再对外列出，新键承接全序。
        rows = conn.execute(
            "SELECT id, title, status_proj, created_at FROM waybills "
            "WHERE COALESCE(retired,0)=0 ORDER BY id"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/waybills/{waybill_id}/timeline")
def waybill_timeline(waybill_id: str):
    conn = connect()
    try:
        guard = timeline_svc.access_guard(conn, waybill_id)
        if guard["status"] != 200:
            headers = None
            if guard["status"] == 410:
                headers = {"X-Remounted-To": guard.get("remounted_to", "")}
            raise HTTPException(
                status_code=guard["status"],
                detail={
                    "message": guard["detail"],
                    "remounted_to": guard.get("remounted_to"),
                    "edge_id": guard.get("edge_id"),
                    "edge_uid": guard.get("edge_uid"),
                },
                headers=headers,
            )
        wb = conn.execute("SELECT * FROM waybills WHERE id=?", (waybill_id,)).fetchone()
        hops = timeline_svc.list_primary(conn, waybill_id)
        proof = conn.execute(
            "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
            (waybill_id,),
        ).fetchone()
        return {
            "waybill": dict(wb),
            "hops": hops,
            "proof": dict(proof) if proof else None,
        }
    finally:
        conn.close()


@router.get("/quarantine")
def list_quarantine():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, reason, member_json, created_at FROM quarantine ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.get("/sidecar")
def list_sidecar():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, waybill_id, event_ts, location_code, event_code, channel FROM hops WHERE channel='sidecar' ORDER BY event_ts"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


@router.post("/ingest/run")
def run_ingest():
    return ingest_svc.run_batch()


@router.get("/status-proofs")
def status_proofs(waybill_id: str | None = None):
    conn = connect()
    try:
        if waybill_id:
            rows = conn.execute(
                "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC",
                (waybill_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM status_proofs ORDER BY id DESC LIMIT 50"
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()

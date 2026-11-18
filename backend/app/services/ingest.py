import json
import uuid
from datetime import datetime, timezone

from app.db import FIXTURES_DIR, connect
from app.seed import map_status


def run_batch() -> dict:
    """基座装载：按夹具追加；乱序重排 / 旁路 / 重复隔离留给功能题空桩。"""
    path = FIXTURES_DIR / "ingest_batch.json"
    if not path.exists():
        return {"ok": False, "error": "缺少 ingest_batch.json"}
    payload = json.loads(path.read_text(encoding="utf-8"))
    batch_id = payload.get("batch_id") or f"B-{uuid.uuid4().hex[:8]}"
    cutoff = payload.get("cutoff_ts")
    hops = payload.get("hops", [])

    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        from app.modules.waybill_key_remount import resolve_active_key

        written = 0
        for hop in hops:
            wid = resolve_active_key(conn, hop["waybill_id"])
            hop["waybill_id"] = wid
            exists = conn.execute("SELECT 1 FROM waybills WHERE id=?", (wid,)).fetchone()
            if not exists:
                conn.execute(
                    "INSERT INTO waybills(id, title, status_proj, created_at) VALUES (?,?,?,?)",
                    (wid, hop.get("waybill_title") or wid, "未知", _now()),
                )
            conn.execute(
                """
                INSERT OR IGNORE INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel)
                VALUES (?,?,?,?,?,?, 'primary')
                """,
                (
                    hop["id"],
                    wid,
                    hop["event_ts"],
                    hop["location_code"],
                    hop["event_code"],
                    hop.get("seq"),
                ),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                written += 1
                proj = map_status(hop["event_code"])
                conn.execute("UPDATE waybills SET status_proj=? WHERE id=?", (proj, wid))
                conn.execute(
                    "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) VALUES (?,?,?,?,?)",
                    (wid, hop["id"], hop["event_code"], proj, _now()),
                )
        report = {
            "batch_id": batch_id,
            "cutoff_ts": cutoff,
            "written": written,
            "input": len(hops),
        }
        conn.execute(
            "INSERT OR REPLACE INTO ingest_batches(id, cutoff_ts, created_at, report_json) VALUES (?,?,?,?)",
            (batch_id, cutoff, _now(), json.dumps(report, ensure_ascii=False)),
        )
        conn.commit()
        return {"ok": True, "report": report}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

from app.modules.waybill_key_remount import guard_axis_served


def list_primary(conn, waybill_id: str) -> list[dict]:
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq, channel, retracted
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, id ASC
        """,
        (waybill_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def access_guard(conn, waybill_id: str) -> dict:
    """竖轴准入检查；退休键返回 410 指引，正常键返回 {'status': 200}。"""
    return guard_axis_served(conn, waybill_id)

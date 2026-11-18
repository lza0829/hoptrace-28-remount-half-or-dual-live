"""waybill_key_remount — 运单业务键重挂。

落库形态（择一）：独立迁移边表 ``waybill_key_remounts``
（旧键、新键、迁入点数、时刻、新旧投影/全序快照），不可变、只追加。
``waybills`` 行另以 ``retired / remounted_to`` 列与运单索引协同，
但关系本身只落迁移边表——禁止只 UPDATE 标题/别名而点仍属旧键。

失败闭（择一）：同事务路径——改点归属 + 重算新旧投影 + 写迁移边，
任一步缺失或后置不变量校验不过即整体回滚。

硬约束：
* 禁止「新键已有投影而点未迁完」——目标键存在时必须零 hop，否则冲突回滚；
* 禁止「旧键点仍可当正式有效点查询」——提交后旧键 retired，正式有效点为零；
* 禁止「旧键与新键同时对外提供有效轴」——同一时刻只有新键承接全序。
"""

import json
import uuid
from datetime import datetime, timezone

STUB = False

RETIRED_PROJ = "已迁移"

EDGE_SCHEMA = """
CREATE TABLE IF NOT EXISTS waybill_key_remounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  edge_uid TEXT NOT NULL UNIQUE,
  old_key TEXT NOT NULL,
  new_key TEXT NOT NULL,
  point_count INTEGER NOT NULL,
  hops_moved_total INTEGER NOT NULL,
  old_proj TEXT NOT NULL,
  new_proj TEXT NOT NULL,
  old_axis_json TEXT NOT NULL,
  new_axis_json TEXT NOT NULL,
  completed_at TEXT NOT NULL,
  UNIQUE(old_key, new_key)
);
"""

class RemountError(Exception):
    """迁移失败：调用方事务已回滚，库内不留半成品。"""


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_schema(conn) -> None:
    """幂等建表 / 补列，供启动与测试调用。"""
    conn.executescript(EDGE_SCHEMA)
    existing = {r["name"] for r in conn.execute("PRAGMA table_info(waybills)").fetchall()}
    if "retired" not in existing:
        conn.execute("ALTER TABLE waybills ADD COLUMN retired INTEGER NOT NULL DEFAULT 0")
    if "remounted_to" not in existing:
        conn.execute("ALTER TABLE waybills ADD COLUMN remounted_to TEXT")
    conn.commit()


# ---------------------------------------------------------------- 读取协同

def is_retired(conn, key: str) -> bool:
    row = conn.execute("SELECT retired FROM waybills WHERE id=?", (key,)).fetchone()
    return bool(row and row["retired"])


def resolve_active_key(conn, key: str) -> str:
    """沿迁移边链求当前承接键；带环保护。装载侧旧键来数时改投新键。"""
    seen = set()
    cur = key
    while True:
        row = conn.execute(
            "SELECT remounted_to, retired FROM waybills WHERE id=?", (cur,)
        ).fetchone()
        if not row or not row["retired"] or not row["remounted_to"]:
            return cur
        if cur in seen:
            raise RemountError(f"迁移边成环：{key}")
        seen.add(cur)
        cur = row["remounted_to"]


def list_active_waybills(conn) -> list[dict]:
    """运单索引：旧键退休后不再对外列出。"""
    rows = conn.execute(
        "SELECT id, title, status_proj, created_at FROM waybills "
        "WHERE COALESCE(retired, 0)=0 ORDER BY id"
    ).fetchall()
    return [dict(r) for r in rows]


def guard_axis_served(conn, key: str) -> dict:
    """竖轴准入：退休键拒绝提供有效时间线，返回 410 指引。"""
    wb = conn.execute("SELECT * FROM waybills WHERE id=?", (key,)).fetchone()
    if not wb:
        return {"status": 404, "detail": "运单不存在"}
    if wb["retired"]:
        edge = conn.execute(
            "SELECT id, edge_uid, new_key FROM waybill_key_remounts "
            "WHERE old_key=? ORDER BY id DESC LIMIT 1",
            (key,),
        ).fetchone()
        active = resolve_active_key(conn, key)
        return {
            "status": 410,
            "detail": f"运单键 {key} 已重挂至 {active}，旧键不再提供有效时间线",
            "remounted_to": active,
            "edge_id": edge["id"] if edge else None,
            "edge_uid": edge["edge_uid"] if edge else None,
        }
    return {"status": 200}


def list_edges(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM waybill_key_remounts ORDER BY id DESC"
    ).fetchall()
    return [_edge_dict(r) for r in rows]


def get_proof(conn, edge_id: int) -> dict | None:
    """重挂证明只读接口：旧键、新键、迁入点数、新旧投影/全序。"""
    row = conn.execute(
        "SELECT * FROM waybill_key_remounts WHERE id=?", (edge_id,)
    ).fetchone()
    return _edge_dict(row) if row else None


def _edge_dict(row) -> dict:
    d = dict(row)
    d["old_axis"] = json.loads(d.pop("old_axis_json"))
    d["new_axis"] = json.loads(d.pop("new_axis_json"))
    return d


# ---------------------------------------------------------------- 迁移主路径

def _formal_axis(conn, key: str) -> list[dict]:
    """正式有效点全序（primary 且未撤回），与 timeline.list_primary 同序。"""
    rows = conn.execute(
        """
        SELECT id, waybill_id, event_ts, location_code, event_code, seq
        FROM hops
        WHERE waybill_id=? AND channel='primary' AND retracted=0
        ORDER BY event_ts ASC, id ASC
        """,
        (key,),
    ).fetchall()
    return [dict(r) for r in rows]


def _latest_projection(conn, key: str) -> tuple[str | None, str | None, str]:
    """末点驱动投影，口径与 seed._refresh_projections 一致。"""
    from app.seed import map_status

    row = conn.execute(
        """
        SELECT h.id, h.event_code
        FROM hops h
        JOIN (
          SELECT MAX(event_ts || '|' || id) AS mk
          FROM hops WHERE retracted=0 AND channel='primary' AND waybill_id=?
        ) t ON (h.event_ts || '|' || h.id)=t.mk
        WHERE h.waybill_id=? AND h.retracted=0 AND h.channel='primary'
        """,
        (key, key),
    ).fetchone()
    if not row:
        return None, None, "未知"
    return row["id"], row["event_code"], map_status(row["event_code"])


def remount_waybill_key(
    conn,
    old_key: str,
    new_key: str,
    *,
    new_title: str | None = None,
    edge_uid: str | None = None,
) -> dict:
    """同事务重挂：点归属改挂 → 新旧投影重算 → 迁移边落库，缺一即回滚。

    成功返回迁移边证明（dict）；失败抛 :class:`RemountError`，且已回滚。
    """
    if not old_key or not new_key or old_key == new_key:
        raise RemountError("旧键/新键不能为空且不能相同")

    conn.execute("BEGIN IMMEDIATE")
    try:
        old_wb = conn.execute(
            "SELECT * FROM waybills WHERE id=?", (old_key,)
        ).fetchone()
        if not old_wb:
            raise RemountError(f"旧键 {old_key} 不存在")
        if old_wb["retired"]:
            raise RemountError(f"旧键 {old_key} 已退休，不能作为迁移源")

        dup_edge = conn.execute(
            "SELECT 1 FROM waybill_key_remounts WHERE old_key=? AND new_key=?",
            (old_key, new_key),
        ).fetchone()
        if dup_edge:
            raise RemountError(f"迁移边 {old_key} → {new_key} 已存在，边不可变")

        # 硬约束：新键已有投影（任何 hop 行）即冲突，杜绝「有投影而点未迁完」。
        new_wb = conn.execute(
            "SELECT * FROM waybills WHERE id=?", (new_key,)
        ).fetchone()
        if new_wb is not None:
            taken = conn.execute(
                "SELECT COUNT(*) AS c FROM hops WHERE waybill_id=?", (new_key,)
            ).fetchone()["c"]
            if taken:
                raise RemountError(
                    f"新键 {new_key} 已持有 {taken} 个点（含投影），禁止半挂迁移"
                )

        old_axis = _formal_axis(conn, old_key)
        point_count = len(old_axis)
        if point_count == 0:
            raise RemountError(f"旧键 {old_key} 无正式有效点可迁移")
        old_axis_ids = [h["id"] for h in old_axis]
        old_proj = old_wb["status_proj"]
        old_last_hop, _, _ = _latest_projection(conn, old_key)

        total_before = conn.execute(
            "SELECT COUNT(*) AS c FROM hops WHERE waybill_id=?", (old_key,)
        ).fetchone()["c"]

        # 1) 新键行（空壳）落位——只建行，不借 UPDATE 标题冒充迁移。
        if new_wb is None:
            conn.execute(
                "INSERT INTO waybills(id, title, status_proj, created_at, retired, remounted_to) "
                "VALUES (?,?,?,?,0,NULL)",
                (new_key, new_title or f"{old_key} 重挂件", "未知", _now()),
            )

        # 2) 点归属整段改挂：正式有效点为主，旁路/撤回点随行，旧键不留点。
        conn.execute(
            "UPDATE hops SET waybill_id=? WHERE waybill_id=?", (new_key, old_key)
        )
        moved = conn.execute("SELECT changes()").fetchone()[0]
        if moved != total_before or moved < point_count:
            raise RemountError(
                f"点归属改挂不完整：应迁 {total_before}，实迁 {moved}"
            )

        # 3) 重算新键投影并写状态证明；旧键投影退休标记。
        new_last_hop, new_code, new_proj = _latest_projection(conn, new_key)
        if new_last_hop != old_last_hop or new_proj != old_proj:
            raise RemountError(
                f"新键投影承接异常：末点 {old_last_hop}→{new_last_hop}，"
                f"投影 {old_proj}→{new_proj}"
            )
        conn.execute(
            "UPDATE waybills SET status_proj=? WHERE id=?", (new_proj, new_key)
        )
        conn.execute(
            "INSERT INTO status_proofs(waybill_id, last_hop_id, mapped_code, proj_value, created_at) "
            "VALUES (?,?,?,?,?)",
            (new_key, new_last_hop, new_code, new_proj, _now()),
        )
        conn.execute(
            "UPDATE waybills SET status_proj=?, retired=0, remounted_to=? WHERE id=?",
            (RETIRED_PROJ, new_key, old_key),
        )

        new_axis = _formal_axis(conn, new_key)
        new_axis_ids = [h["id"] for h in new_axis]

        # 4) 写不可变迁移边（审计）。
        uid = edge_uid or f"E-{uuid.uuid4().hex[:12]}"
        conn.execute(
            """
            INSERT INTO waybill_key_remounts(
              edge_uid, old_key, new_key, point_count, hops_moved_total,
              old_proj, new_proj, old_axis_json, new_axis_json, completed_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                uid,
                old_key,
                new_key,
                point_count,
                moved,
                old_proj,
                new_proj,
                json.dumps(old_axis_ids, ensure_ascii=False),
                json.dumps(new_axis_ids, ensure_ascii=False),
                _now(),
            ),
        )

        # 5) 提交前不变量闸门：三条硬约束逐条验，失败即抛→回滚。
        _assert_post_invariants(conn, old_key, new_key, point_count, new_axis_ids)

        conn.commit()
        edge = conn.execute(
            "SELECT * FROM waybill_key_remounts WHERE edge_uid=?", (uid,)
        ).fetchone()
        return _edge_dict(edge)
    except RemountError:
        conn.rollback()
        raise
    except Exception as exc:  # 任何中途异常（含唯一键碰撞）都闭着失败
        conn.rollback()
        raise RemountError(f"迁移中途失败已回滚：{exc}") from exc


def _assert_post_invariants(conn, old_key, new_key, point_count, new_axis_ids):
    old_left = conn.execute(
        "SELECT COUNT(*) AS c FROM hops "
        "WHERE waybill_id=? AND channel='primary' AND retracted=0",
        (old_key,),
    ).fetchone()["c"]
    if old_left != 0:
        raise RemountError(f"旧键仍残留 {old_left} 个正式有效点")

    new_count = conn.execute(
        "SELECT COUNT(*) AS c FROM hops "
        "WHERE waybill_id=? AND channel='primary' AND retracted=0",
        (new_key,),
    ).fetchone()["c"]
    if new_count != point_count:
        raise RemountError(
            f"新键点数 {new_count} 与迁入点数 {point_count} 不符"
        )

    old_row = conn.execute(
        "SELECT retired, remounted_to FROM waybills WHERE id=?", (old_key,)
    ).fetchone()
    new_row = conn.execute(
        "SELECT retired FROM waybills WHERE id=?", (new_key,)
    ).fetchone()
    if not old_row["retired"] or old_row["remounted_to"] != new_key:
        raise RemountError("旧键未退休或迁移指针缺失")
    if new_row["retired"]:
        raise RemountError("新键被标记退休，有效轴缺失")
    if old_row["retired"] and new_row["retired"]:
        raise RemountError("新旧键同时退休，无对外有效轴")
    # 新键全序须与改挂前旧键全序逐点一致（全序承接）。
    if [h["id"] for h in _formal_axis(conn, new_key)] != new_axis_ids:
        raise RemountError("新键全序承接不完整")

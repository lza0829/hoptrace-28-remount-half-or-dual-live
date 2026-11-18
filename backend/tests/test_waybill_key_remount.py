"""waybill_key_remount 对照测试：一次成功迁移 vs 一次中途失败回滚。"""

import json

import pytest
from fastapi.testclient import TestClient

from app.db import connect
from app.main import app
from app.modules.waybill_key_remount import (
    RETIRED_PROJ,
    RemountError,
    ensure_schema,
    get_proof,
    is_retired,
    list_active_waybills,
    list_edges,
    remount_waybill_key,
    resolve_active_key,
)
from app.services import timeline as timeline_svc

OLD = "WB-OLD"
NEW = "WB-NEW"
EXPECTED_AXIS = ["RH-1", "RH-2", "RH-3"]


def _axis_ids(conn, key):
    return [h["id"] for h in timeline_svc.list_primary(conn, key)]


def _snapshot(conn):
    """迁移前全量快照，回滚对照用。"""
    return {
        "old_axis": _axis_ids(conn, OLD),
        "new_axis": _axis_ids(conn, NEW),
        "old_retired": is_retired(conn, OLD),
        "new_retired": is_retired(conn, NEW),
        "old_proj": conn.execute(
            "SELECT status_proj FROM waybills WHERE id=?", (OLD,)
        ).fetchone()["status_proj"],
        "edges": list_edges(conn),
        "hop_owners": {
            r["id"]: r["waybill_id"]
            for r in conn.execute("SELECT id, waybill_id FROM hops").fetchall()
        },
    }


# ---------------------------------------------------------------- 成功路径

def test_successful_remount_full_order_and_proof(remount_db):
    conn = connect()
    before = _snapshot(conn)
    assert before["old_axis"] == EXPECTED_AXIS
    assert before["new_axis"] == []
    assert not before["old_retired"]

    edge = remount_waybill_key(conn, OLD, NEW, new_title="新业务键运单（承接）")

    # 迁移边审计字段齐全
    assert edge["old_key"] == OLD
    assert edge["new_key"] == NEW
    assert edge["point_count"] == 3
    assert edge["hops_moved_total"] == 3
    assert edge["old_proj"] == "到站"
    assert edge["new_proj"] == "到站"
    assert edge["old_axis"] == EXPECTED_AXIS
    assert edge["new_axis"] == EXPECTED_AXIS
    assert edge["completed_at"]

    # 硬约束一：旧键正式有效点为零，不能再当有效轴查询
    assert _axis_ids(conn, OLD) == []
    assert is_retired(conn, OLD)
    guard = timeline_svc.access_guard(conn, OLD)
    assert guard["status"] == 410
    assert guard["remounted_to"] == NEW

    # 硬约束二：新键承接全序且只有新键对外
    assert _axis_ids(conn, NEW) == EXPECTED_AXIS
    assert not is_retired(conn, NEW)
    active_ids = [w["id"] for w in list_active_waybills(conn)]
    assert NEW in active_ids and OLD not in active_ids
    assert resolve_active_key(conn, OLD) == NEW

    # 投影承接 + 状态证明写到新键
    new_wb = conn.execute("SELECT * FROM waybills WHERE id=?", (NEW,)).fetchone()
    assert new_wb["status_proj"] == "到站"
    old_wb = conn.execute("SELECT * FROM waybills WHERE id=?", (OLD,)).fetchone()
    assert old_wb["status_proj"] == RETIRED_PROJ
    proof = conn.execute(
        "SELECT * FROM status_proofs WHERE waybill_id=? ORDER BY id DESC LIMIT 1",
        (NEW,),
    ).fetchone()
    assert proof["last_hop_id"] == "RH-3"

    # 重挂证明只读接口
    via_api = get_proof(conn, edge["id"])
    assert via_api["new_axis"] == EXPECTED_AXIS
    conn.close()


def test_remount_edge_is_immutable_and_idempotent_guard(remount_db):
    conn = connect()
    remount_waybill_key(conn, OLD, NEW)
    # 同边重复迁移必须拒绝
    with pytest.raises(RemountError):
        remount_waybill_key(conn, OLD, NEW)
    # 退休键不能再当源
    with pytest.raises(RemountError):
        remount_waybill_key(conn, OLD, "WB-X")
    conn.close()


# ---------------------------------------------------------------- 失败回滚

def test_conflict_rollback_leaves_no_half_remount(remount_db):
    """新键已有点（有投影）→ 拒绝，且库内状态与迁移前逐字节一致。"""
    conn = connect()
    before = _snapshot(conn)

    # 先给新键挂一个点，制造「新键已有投影」冲突
    conn.execute(
        "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
        "VALUES ('RH-X', ?, '2026-11-18T09:00:00Z', 'SH-LOCAL', 'PICKED', 1, 'primary')",
        (NEW,),
    )
    conn.commit()

    with pytest.raises(RemountError):
        remount_waybill_key(conn, OLD, NEW)
    conn.close()

    # 新连接复核：回滚彻底，无迁移边、无投影半成品
    conn = connect()
    after = _snapshot(conn)
    assert after["old_axis"] == before["old_axis"] == EXPECTED_AXIS
    assert after["new_axis"] == ["RH-X"]  # 冲突点保留，旧点未被搬过来
    assert after["old_retired"] is False
    assert after["new_retired"] is False
    assert after["edges"] == []
    assert after["hop_owners"] == before["hop_owners"] | {"RH-X": NEW}
    # 旧键仍是唯一有效轴，新键未承接
    assert timeline_svc.access_guard(conn, OLD)["status"] == 200
    conn.close()


def test_mid_transaction_failure_full_rollback(remount_db):
    """模拟写迁移边时中途故障：点已改挂也必须整体回滚。"""
    real_conn = connect()
    before = _snapshot(real_conn)

    class BoobyTrappedConn:
        """连接代理：迁移边 INSERT 时炸，其余透传。"""

        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, params=()):
            if "INSERT INTO waybill_key_remounts" in sql:
                raise RuntimeError("模拟磁盘故障：迁移边写入失败")
            return self._inner.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._inner, name)

    conn = BoobyTrappedConn(real_conn)
    with pytest.raises(RemountError):
        remount_waybill_key(conn, OLD, NEW)
    real_conn.close()

    conn = connect()
    after = _snapshot(conn)
    assert after == before  # 点归属、投影、退休标记、迁移边全部回到迁移前
    assert _axis_ids(conn, OLD) == EXPECTED_AXIS
    assert _axis_ids(conn, NEW) == []
    conn.close()


def test_ingest_rerouted_from_retired_old_key(remount_db, monkeypatch):
    """装载协同：旧键退休后，按旧键来的新点必须改投承接键，旧键不得复活。"""
    from app import db as db_mod
    from app.services import ingest as ingest_svc

    # ingest 在导入时绑定了 FIXTURES_DIR，直接钉到临时夹具目录。
    monkeypatch.setattr(ingest_svc, "FIXTURES_DIR", db_mod.FIXTURES_DIR)
    conn = connect()
    remount_waybill_key(conn, OLD, NEW)
    conn.close()

    batch = {
        "batch_id": "B-REMOUNT-01",
        "cutoff_ts": "2026-11-18T20:00:00Z",
        "hops": [
            {
                "id": "RH-4",
                "waybill_id": OLD,  # 上游仍用旧键
                "event_ts": "2026-11-18T19:20:00Z",
                "location_code": "HZ-STATION",
                "event_code": "OUT_FOR_DELIVERY",
                "seq": 4,
            }
        ],
    }
    (db_mod.FIXTURES_DIR / "ingest_batch.json").write_text(
        json.dumps(batch, ensure_ascii=False), encoding="utf-8"
    )

    report = ingest_svc.run_batch()
    assert report["ok"] and report["report"]["written"] == 1

    conn = connect()
    assert _axis_ids(conn, OLD) == []  # 旧键未复活
    assert _axis_ids(conn, NEW)[-1] == "RH-4"  # 新点落到承接键
    assert timeline_svc.access_guard(conn, OLD)["status"] == 410
    conn.close()


def test_invalid_requests_rejected(remount_db):
    conn = connect()
    with pytest.raises(RemountError):
        remount_waybill_key(conn, OLD, OLD)
    with pytest.raises(RemountError):
        remount_waybill_key(conn, "NO-SUCH", NEW)
    conn.close()


# ---------------------------------------------------------------- HTTP 对照

def test_http_success_and_410_for_old_key(remount_db, monkeypatch):
    # TestClient 启动事件会重跑 seed.init_db()，把连接全局先钉到临时库。
    from app import db as db_mod
    from app import seed as seed_mod

    monkeypatch.setattr(seed_mod, "connect", db_mod.connect)
    with TestClient(app) as client:
        res = client.post("/api/remount", json={"old_key": OLD, "new_key": NEW})
        assert res.status_code == 200
        edge = res.json()["edge"]
        assert edge["point_count"] == 3

        # 新键可查全序
        tl = client.get(f"/api/waybills/{NEW}/timeline")
        assert tl.status_code == 200
        assert [h["id"] for h in tl.json()["hops"]] == EXPECTED_AXIS

        # 旧键竖轴 410
        gone = client.get(f"/api/waybills/{OLD}/timeline")
        assert gone.status_code == 410
        assert gone.json()["detail"]["remounted_to"] == NEW

        # 索引只剩新键
        ids = [w["id"] for w in client.get("/api/waybills").json()]
        assert ids == [NEW]

        # 证明只读接口
        proof = client.get(f"/api/remounts/{edge['id']}/proof")
        assert proof.status_code == 200
        assert proof.json()["old_key"] == OLD and proof.json()["new_key"] == NEW

        edges = client.get("/api/remounts")
        assert edges.status_code == 200 and len(edges.json()) == 1


def test_http_conflict_returns_409_and_rolls_back(remount_db, monkeypatch):
    from app import seed as seed_mod
    from app.db import connect as _connect

    monkeypatch.setattr(seed_mod, "connect", _connect)
    conn = connect()
    conn.execute(
        "INSERT INTO hops(id, waybill_id, event_ts, location_code, event_code, seq, channel) "
        "VALUES ('RH-X', ?, '2026-11-18T09:00:00Z', 'SH-LOCAL', 'PICKED', 1, 'primary')",
        (NEW,),
    )
    conn.commit()
    conn.close()

    with TestClient(app) as client:
        res = client.post("/api/remount", json={"old_key": OLD, "new_key": NEW})
        assert res.status_code == 409
        # 回滚后旧键时间线仍完整
        tl = client.get(f"/api/waybills/{OLD}/timeline")
        assert tl.status_code == 200
        assert [h["id"] for h in tl.json()["hops"]] == EXPECTED_AXIS
        assert client.get("/api/remounts").json() == []


# ---------------------------------------------------------------- 列表退役核证

def test_list_and_search_retire_source_key(remount_db, monkeypatch):
    """换键后：对外有效列表与检索都不含源键，目标键承接。"""
    from app import seed as seed_mod
    from app.db import connect as _connect

    monkeypatch.setattr(seed_mod, "connect", _connect)
    with TestClient(app) as client:
        assert client.post("/api/remount", json={"old_key": OLD, "new_key": NEW}).status_code == 200

        ids = [w["id"] for w in client.get("/api/waybills").json()]
        assert OLD not in ids and NEW in ids

        # 检索入口同样不得命中源键
        assert client.get("/api/waybills", params={"q": OLD}).json() == []
        assert [w["id"] for w in client.get("/api/waybills", params={"q": NEW}).json()] == [NEW]

        # 源键入口不得进有效轴查询
        assert client.get(f"/api/waybills/{OLD}/timeline").status_code == 410

        # 新增的列表退役核证只读接口
        cert = client.get("/api/remounts/list-retirement")
        assert cert.status_code == 200
        body = cert.json()
        assert body["ok"] is True
        assert len(body["edges"]) == 1
        check = body["edges"][0]
        assert check["old_key"] == OLD and check["new_key"] == NEW
        assert check["old_key_retired"] is True
        assert check["old_key_in_active_list"] is False
        assert check["old_key_in_search"] is False
        assert check["new_key_in_active_list"] is True
        assert OLD not in body["active_waybill_ids"] and NEW in body["active_waybill_ids"]


def test_list_retirement_cert_detects_flag_leak(remount_db):
    """核证接口核的是「退役标记 + 列表实际口径」双条件，任一不符即 ok=false。"""
    from app.modules.waybill_key_remount import list_retirement_proofs

    conn = connect()
    remount_waybill_key(conn, OLD, NEW)
    assert list_retirement_proofs(conn)["ok"] is True

    # 模拟「迁移边已写而运单行退役标记丢了」的坏状态
    conn.execute("UPDATE waybills SET retired=0 WHERE id=?", (OLD,))
    conn.commit()
    leaked = list_retirement_proofs(conn)
    assert leaked["ok"] is False
    assert leaked["edges"][0]["old_key_retired"] is False
    # 列表口径另有边表兜底：即便标记滞后，源键也不会被列出
    assert leaked["edges"][0]["old_key_in_active_list"] is False
    conn.close()


def test_retire_flag_missing_still_hides_source(remount_db):
    """边已写而 retired 标记滞后：列表/检索/竖轴仍不得把源键当有效运单。"""
    from app.modules.waybill_key_remount import (
        guard_axis_served,
        query_active_waybills,
        resolve_active_key,
    )

    conn = connect()
    remount_waybill_key(conn, OLD, NEW)
    # 人为回退 retired 标记，模拟旧版本写坏的库
    conn.execute("UPDATE waybills SET retired=0, remounted_to=NULL WHERE id=?", (OLD,))
    conn.commit()

    assert OLD not in [w["id"] for w in query_active_waybills(conn)]
    assert OLD not in [w["id"] for w in query_active_waybills(conn, q=OLD)]
    assert guard_axis_served(conn, OLD)["status"] == 410
    assert resolve_active_key(conn, OLD) == NEW
    conn.close()


def test_gate_rolls_back_when_source_not_marked_retired(remount_db):
    """列表退役闸门：源键退役标记缺失时整笔重挂回滚，旧键列表状态不变。"""
    real_conn = connect()
    before = _snapshot(real_conn)

    class LeakyListConn:
        """连接代理：在 retire UPDATE 后把 retired 抹回 0，模拟列表退役失效。"""

        def __init__(self, inner):
            self._inner = inner
            self._tripped = False

        def execute(self, sql, params=()):
            res = self._inner.execute(sql, params)
            if (
                not self._tripped
                and sql.strip().startswith("UPDATE waybills SET status_proj=?")
                and params and params[0] == RETIRED_PROJ
            ):
                self._tripped = True
                self._inner.execute(
                    "UPDATE waybills SET retired=0 WHERE id=?", (params[2],)
                )
            return res

        def __getattr__(self, name):
            return getattr(self._inner, name)

    conn = LeakyListConn(real_conn)
    with pytest.raises(RemountError):
        remount_waybill_key(conn, OLD, NEW)
    real_conn.close()

    conn = connect()
    after = _snapshot(conn)
    assert after == before
    assert _axis_ids(conn, OLD) == EXPECTED_AXIS
    # 回滚后回到迁移前的列表形态：旧键仍有效、新键仍为空壳
    assert [w["id"] for w in list_active_waybills(conn)] == [NEW, OLD]
    conn.close()

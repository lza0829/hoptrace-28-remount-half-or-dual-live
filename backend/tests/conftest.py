"""waybill_key_remount 测试夹具：隔离的临时 SQLite 库 + 两键数据。"""

import json
from pathlib import Path

import pytest

from app import db as db_mod
from app import seed

FIXTURE_SRC = Path(__file__).resolve().parent.parent.parent / "fixtures" / "remount_two_keys.json"


@pytest.fixture
def remount_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    fx_dir = tmp_path / "fixtures"
    data_dir.mkdir()
    fx_dir.mkdir()
    (fx_dir / "seed_hops.json").write_text(FIXTURE_SRC.read_text(encoding="utf-8"), encoding="utf-8")

    # connect() 在调用时读取模块全局路径，直接改属性即可让所有调用方落到临时库。
    monkeypatch.setattr(db_mod, "DB_PATH", data_dir / "remount_test.db")
    monkeypatch.setattr(db_mod, "FIXTURES_DIR", fx_dir)
    monkeypatch.setattr(seed, "FIXTURES_DIR", fx_dir)

    seed.init_db()
    yield db_mod

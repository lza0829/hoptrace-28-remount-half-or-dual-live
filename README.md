# 途迹（Hoptrace）

轨迹点装载 → 运单时间线物化 → 迟到旁路与脏点隔离 → 状态证明互证。

**不是** Feed 归并、CDC 游标 Sink、质检签发、数仓分层、交班过户、正冲净额。  
**也不是** 电商订单 / 仓库库存 / 外卖工单 CRUD——去皮后是事件装载与有序物化。

## 启动

```bash
# 后端
cd backend
pip install -r requirements.txt
set DATA_DIR=%CD%\data
set FIXTURES_DIR=%CD%\..\fixtures
uvicorn app.main:app --host 127.0.0.1 --port 10800

# 前端
cd frontend
npm install
npm run dev
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://127.0.0.1:5800 |
| 接口 | http://127.0.0.1:10800 |

## UI

三栏测绘板：顶品牌条 + 中竖轴时间线 + 右检查器 + 底 hop 点阵。  
不使用左轨编号导航、不使用顶栏流程 ribbon。

## 运单键重挂（waybill_key_remount）

业务键变更时，将旧键正式有效点**整段**挂到新键，禁止只改标题/别名而点仍属旧键。

- **落库形态**：独立不可变迁移边表 `waybill_key_remounts`（旧键、新键、迁入点数、移动行数、新旧投影、新旧全序快照、完成时刻）；`waybills` 行以 `retired / remounted_to` 与索引协同。
- **失败闭**：同事务内「改点归属 → 重算新旧投影并写状态证明 → 写迁移边 → 提交前不变量闸门」，任一缺失或校验不过整体回滚。
- **硬约束**：新键已有点（有投影）即 409 冲突；提交后旧键竖轴返回 410 且正式有效点为零；同一时刻只有新键对外提供有效轴；装载侧按旧键来的点经 `resolve_active_key` 改投承接键。

| 接口 | 说明 |
| --- | --- |
| `POST /api/remount` | 同事务执行重挂，冲突/中途失败返回 409 并回滚 |
| `GET /api/remounts` | 迁移边审计列表 |
| `GET /api/remounts/{id}/proof` | 重挂证明只读接口（旧键、新键、迁入点数、新旧投影与全序） |
| `GET /api/waybills/{id}/timeline` | 退休旧键返回 410，body 指向承接键与迁移边 |

两键夹具 `fixtures/remount_two_keys.json`；对照测试 `backend/tests/`（成功迁移、冲突回滚、写边中途故障回滚、装载改投、HTTP 410/409）：

```bash
cd backend && .venv/bin/python -m pytest tests/ -q
```

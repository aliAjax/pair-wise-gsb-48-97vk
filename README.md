# 证券结算与企业行动处理

纯Python标准库实现的证券结算与企业行动处理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、净额结算、交收完整性和公司行动（拆股/合并换股/现金分红）调整和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8324
```

默认端口为`8324`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。
- `GET /api/records/{id}/cash-entitlement`：查询现金分红权益（待入账时`reconcilable=false`）。
- `POST /api/records/{id}/cash-entitlement/post`：现金权益入账（`settlement_officer`），请求体为`{"expected_version":1,"data":{"posted_amount":300.0}}`。
- `GET /api/records/{id}/cash-entitlement/reconcile`：核对已入账金额与按股数计算的应得金额，返回`matched`与`difference`。

### 企业行动

- 拆股（`split`）与合并（`merger`）均按换股比例调整持仓：有效数量=数量×比例，有效均价=价格÷比例（成本保持不变）；合并后比例小于1即缩减持仓。应用时在审计时间线记录数量、价格、金额的前后对照。
- 现金分红（`dividend`）不改变持仓数量与均价；应用企业行动时按股数×每股金额生成一笔`pending`现金权益，经入账（`post_cash`）转为`posted`后金额才可核对，入账金额与应得金额不符时`matched=false`。
- 交收（`settle`）一律按企业行动后的`effective_quantity`核对证券数量，与原始数量不符将被拒绝。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。

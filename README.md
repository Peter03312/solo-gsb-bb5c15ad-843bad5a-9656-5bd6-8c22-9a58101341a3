# 电影节片源放映许可核验服务

基于 **Python + FastAPI + SQLite** 的放映许可核验服务：录入影片/场馆授权（有效时段、场次上限、撤销记录、限时暂停），对待核验场次进行**幂等、并发安全**的准入判定，返回准入结果、命中授权、剩余额度与拒绝原因；发行方还可**一次集中提交 1～100 个场次**，服务按输入顺序核验、原序返回每项结果与输入位置，整批原子提交；已核准且未开场的场次可按原幂等键**取消**并释放额度；运营方还可为尚未开场、未取消的核准场次领取**一次性开场码**，场馆在计划开场时刻起 30 分钟内凭码核销。

## 功能特性

- **授权管理**：录入影片、场馆、有效时段、场次上限；查询实时已用/剩余额度
- **影片级共享额度池**：发行方可为影片登记**跨场馆总放映次数**池；授权录入时可选绑池（绑定后不可改），核准时授权与所绑池**各消耗一次**，池耗尽返回 `POOL_EXHAUSTED`
- **限时暂停**：运营可为单份授权登记带时区起止时刻的暂停区间（左闭右开，重叠按并集生效，登记后不可改）；暂停只影响**此后提交**且场次开始落入区间的新核验，既有流水与额度占用不变
- **场次取消**：已核准且**尚未开场**的场次可按原核验幂等键取消（非空原因）；取消后授权与所绑池的实时已用次数**各回退一次**，释放额度可被后续核验使用；原核验决定与扣减快照保持原样，核验查询可见取消状态
- **一次性开场码与核销（本轮新增）**：运营方按原核验幂等键为**尚未开场、未取消的核准场次**领取一次性开场码；每场仅一个码，首次领取返回原码，重复领取与状态查询只见状态（`issued`/`redeemed`/`voided`/`not_issued`）不泄露原码；场馆凭码及场馆编号在**计划开场时刻起 30 分钟**内（左闭右开 `[start, start+30min)`）核销，返回场次、实际核销时间与关联授权；过早/过期/场馆不符/未知码/已取消均拒绝且不产生回执；成功核销后重试恒返回首次回执（即使已过窗口），不重复记场次；取消成功后未核销的码立即失效
- **授权状态与核销解耦**：开场码核销**不再校验授权**——授权此后撤销或暂停不改变已核准场次的核销资格，原核验结果与授权/池额度占用也不因核销而变化
- **核验判定**：多份授权同时有效时只消耗一份额度，**跳过已撤销/已暂停授权**，优先最早到期且自身与所绑池均有余额的授权；覆盖时段且未撤销的授权全部暂停时返回 `LICENSE_SUSPENDED`
- **集中批量核验（本轮新增）**：`POST /verifications/batch` 一次请求按**输入顺序**核验 1～100 个场次，响应 `results` 与输入同序，每项为完整单场核验结果并附输入位置 `index`（从 0 开始）；每项沿用单场核验的授权、共享池、暂停、撤销和幂等规则，**前项消耗的额度影响后项**，业务拒绝同样落一条可追溯流水并继续处理；同批或历史中**同键同内容**返回原流水（`replay=true`）且**不重复扣额**，**同键不同内容使整批失败（409，带 `index`/`existing`）**；任一项格式错误（422）或处理异常时**整批回滚**——不留任何新流水、不占任何额度/池额度；整批在单个 `BEGIN IMMEDIATE` 事务内完成，与并发的单场核验、取消**完全串行**，彼此只能看到整批之前或之后的完整状态，不穿插超额；原单场接口 `POST /verifications` 语义与路径保持不变
- **并发安全**：核验的"查状态 + 扣额度"、暂停登记、场次取消以及开场码领取/核销均在单个 `BEGIN IMMEDIATE` 事务内串行化，SQLite WAL 模式下写请求串行化，**每次核验/领取/核销决定都对应其他登记前或登记后的完整状态**，取消与核验并发不超额、不重复释放，码与回执各至多一个
- **幂等语义**：同一业务幂等键重试返回原结果（`replay=true`，不重复扣额度、不重复占池、不受此后暂停影响）；键相同但请求内容变化返回 **409 冲突**；取消、开场码领取与核销同样幂等——重复操作返回首次记录、不重复释放/不重复记场次，原因不同返回 **409**
- **撤销语义**：撤销仅阻止后续核验；已核准场次记录及其池占用保留、不退还、可追溯，且不影响其开场码核销
- **明确拒绝**：时间不在任何授权时段、授权已撤销、授权全部暂停、额度用尽、共享池耗尽、无授权时分别返回具体拒绝原因

## 快速开始

### 方式一：Docker Compose 一键启动（推荐）

```bash
docker compose up --build -d
```

启动即自动完成建表初始化；`SEED_DEMO_DATA=1`（compose 默认开启）时，空库会写入两条演示授权，可直接体验核验流程。

- 服务地址：http://localhost:8000
- 交互式接口文档：http://localhost:8000/docs
- 健康检查：`curl http://localhost:8000/health`

数据持久化在命名卷 `licensing-data`（容器内 `/data/filmfest.db`）。停止并清理：

```bash
docker compose down        # 保留数据卷
docker compose down -v     # 连同数据一起删除
```

### 方式二：本地运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pytest -q        # 120 个用例，含 20 线程授权不超额 / 40 线程跨场馆不突破池上限 / 暂停登记与核验并发状态一致 / 取消与核验并发不超额不重复释放 / 20 线程开场码并发唯一领取 / 20 线程核销回执唯一 / 领取-取消竞态码与取消唯一 / 批量顺序扣额与拒绝留痕 / 批量-单场-取消并发不穿插超额 / 批量冲突与异常整批回滚
```

### 调用示例脚本

服务启动后执行完整演示（录入 → 核验 → 幂等重放 → 冲突 → 撤销 → 追溯）：

```bash
bash examples/demo.sh
```

集中批量核验完整演示（顺序扣额 → 拒绝留痕继续 → 同批/历史重放 → 同键异义整批 409 回滚 → 格式错误 422 → 取消释放后批量复用）：

```bash
bash examples/demo_batch.sh
```

共享额度池完整演示（建池 → 绑池校验 → 跨场馆消耗 → 幂等 → 池耗尽 → 撤销不退池）：

```bash
bash examples/demo_pools.sh
```

授权限时暂停完整演示（登记暂停 → 左闭右开边界 → 跳过暂停授权 → 全部暂停 `LICENSE_SUSPENDED` → 历史重放 → 查询与不可改）：

```bash
bash examples/demo_suspensions.sh
```

已核准场次取消完整演示（取消 → 额度回退 → 幂等重放 → 原因冲突 → 释放额度复用 → 已拒绝/已开场/未知键拒绝）：

```bash
bash examples/demo_cancellations.sh
```

一次性开场码与场馆核销完整演示（领取 → 状态不泄露码 → 过早/错场馆拒绝 → 到场核销 → 回执重放 → 取消即失效 → 未领/已开场/已拒绝拒绝）：

```bash
bash examples/demo_opening_codes.sh
```

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FESTIVAL_DB_PATH` | `./filmfest.db`（容器内 `/data/filmfest.db`） | SQLite 数据库文件路径 |
| `PORT` | `8000` | 服务监听端口（容器内） |
| `SEED_DEMO_DATA` | 关闭 | 设为 `1` 时，空库启动写入两条演示授权 |

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/pools` | 登记影片级共享额度池（影片、正整数上限） |
| GET | `/pools` | 池列表（含已用/剩余次数、绑定授权数），可按 `film_id` 过滤 |
| GET | `/pools/{id}` | 单个池详情 |
| POST | `/licenses` | 录入授权（影片、场馆、有效时段、场次上限，可选 `pool_id` 绑池） |
| GET | `/licenses` | 授权列表（含已用/剩余额度、所绑池快照、撤销状态），可按 `film_id`/`venue_id`/`pool_id` 过滤 |
| GET | `/licenses/{id}` | 单条授权详情 |
| POST | `/licenses/{id}/revoke` | 撤销授权（幂等保护：重复撤销返回 409） |
| POST | `/licenses/{id}/suspensions` | 登记限时暂停（带时区起止、非空原因；授权不存在 404，时刻非法 422） |
| GET | `/licenses/{id}/suspensions` | 某份授权的全部暂停记录（授权不存在 404） |
| GET | `/suspensions` | 全部暂停记录，可按 `license_id` 过滤（只读、不可改） |
| GET | `/suspensions/{id}` | 单条暂停记录详情 |
| POST | `/verifications` | 核验单场（幂等，原接口保持不变） |
| POST | `/verifications/batch` | **集中批量核验** 1～100 场（原子、按输入顺序、原序返回 `index`；同键异义 409，格式错误 422 整批不落流水） |
| GET | `/verifications` | 核验流水（含取消状态），可按 `film_id`/`venue_id`/`decision`/`license_id`/`pool_id` 过滤 |
| GET | `/verifications/{idempotency_key}` | 按幂等键查询单条核验记录（含取消状态） |
| POST | `/verifications/{idempotency_key}/cancel` | 取消已核准且未开场的场次（幂等；未知键 404，已拒绝/已开场 422，原因不同 409） |
| POST | `/verifications/{idempotency_key}/opening-code` | 运营方为未开场、未取消的核准场次领取一次性开场码（首次返回码；重复领取只见状态 `replay=true`；未知键 404，已拒绝/已取消/已开场 422） |
| GET | `/verifications/{idempotency_key}/opening-code` | 查询开场码状态（`not_issued`/`issued`/`redeemed`/`voided`），**不含原码**；未知键 404 |
| POST | `/redemptions` | 场馆凭 `code` + `venue_id` 在开场起 30 分钟内核销；成功返场次/实际核销时间/关联授权，重试返首次回执；过早/过期/场馆不符/已取消 422，未知码 404 |

### 调用示例（curl）

录入授权：

```bash
curl -X POST http://localhost:8000/licenses \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-1",
       "valid_from":"2026-10-01T00:00:00Z","valid_to":"2026-10-31T23:59:59Z",
       "max_sessions":5,"note":"主竞赛单元"}'
```

核验场次：

```bash
curl -X POST http://localhost:8000/verifications \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-1",
       "start_time":"2026-10-05T19:30:00Z",
       "idempotency_key":"screening-0001"}'
```

准入响应（命中授权 3，本次消耗后剩余 4 场；该授权未绑池，池字段为 null）：

```json
{
  "id": 1,
  "idempotency_key": "screening-0001",
  "film_id": "film-autumn-river",
  "venue_id": "hall-1",
  "start_time": "2026-10-05T19:30:00.000000Z",
  "decision": "approved",
  "license_id": 3,
  "pool_id": null,
  "reject_reason": null,
  "remaining_quota": 4,
  "pool_remaining_quota": null,
  "verified_at": "2026-10-04T08:00:00.000000Z",
  "replay": false,
  "cancelled": false,
  "cancellation": null
}
```

拒绝响应：

```json
{
  "decision": "rejected",
  "license_id": null,
  "pool_id": null,
  "reject_reason": "QUOTA_EXHAUSTED",
  "remaining_quota": null,
  "pool_remaining_quota": null,
  "replay": false
}
```

### 集中批量核验（1～100 场）

发行方一次集中提交排片时，将多场放在 `items` 数组中调用 `POST /verifications/batch`。服务**严格按数组顺序**核验，`results` 与输入**同序**返回，每项都是完整的单场核验结果，并额外带 `index`（该项在输入中的位置，从 0 开始）。下例授权额度为 2：第 0、1 项先后核准（消耗后剩余 1、0），第 2 项是第 0 项的**同键同内容重放**（`replay=true`，不重复扣额），第 3 项因前项已耗尽额度被拒 `QUOTA_EXHAUSTED`——**业务拒绝也落一条可追溯流水，整批继续处理**：

```bash
curl -X POST http://localhost:8000/verifications/batch \
  -H 'Content-Type: application/json' \
  -d '{"items":[
    {"film_id":"film-autumn-river","venue_id":"hall-1",
     "start_time":"2030-06-01T19:30:00Z","idempotency_key":"batch-1"},
    {"film_id":"film-autumn-river","venue_id":"hall-1",
     "start_time":"2030-06-02T19:30:00Z","idempotency_key":"batch-2"},
    {"film_id":"film-autumn-river","venue_id":"hall-1",
     "start_time":"2030-06-01T19:30:00Z","idempotency_key":"batch-1"},
    {"film_id":"film-autumn-river","venue_id":"hall-1",
     "start_time":"2030-06-03T19:30:00Z","idempotency_key":"batch-3"}
  ]}'
```

```json
{
  "count": 4,
  "results": [
    {"index": 0, "idempotency_key": "batch-1", "decision": "approved",
     "license_id": 1, "remaining_quota": 1, "replay": false, "...": "..."},
    {"index": 1, "idempotency_key": "batch-2", "decision": "approved",
     "license_id": 1, "remaining_quota": 0, "replay": false, "...": "..."},
    {"index": 2, "idempotency_key": "batch-1", "decision": "approved",
     "license_id": 1, "remaining_quota": 1, "replay": true, "...": "..."},
    {"index": 3, "idempotency_key": "batch-3", "decision": "rejected",
     "reject_reason": "QUOTA_EXHAUSTED", "replay": false, "...": "..."}
  ]
}
```

批量语义要点：

- **顺序额度联动**：每项沿用单场核验的授权选择、共享池、暂停、撤销与幂等规则；前项的核准立即改变授权/池实时已用次数，后项据此判定（上例第 3 项即因前两项耗尽而被拒）
- **拒绝也留痕**：业务拒绝（如 `NO_LICENSE` / `QUOTA_EXHAUSTED` / `POOL_EXHAUSTED` …）与核准一样写入核验流水，可在 `GET /verifications/{key}` 追溯，且不中断整批
- **幂等**：同一幂等键无论出现在同批还是历史中，只要请求内容（影片、场馆、开始时间）一致，就返回**原流水**并标 `replay=true`，**绝不重复扣额/重复占池**
- **同键不同内容 → 整批失败**：任一键与其历史（含本批前面的项）内容不一致，整批返回 `409`，响应 `index` 指出冲突项位置、`existing` 附首次流水，**包括冲突项之前各项在内的整批全部回滚**——不留下新流水、不占用额度：

```json
{"detail": "idempotency key 'batch-1' was already used with a different request payload",
 "code": "CONFLICT",
 "index": 1,
 "existing": {"idempotency_key": "batch-1", "decision": "approved", "...": "..."}}
```

- **格式错误 → 422 且整批不落库**：`items` 为空、超过 100 项、任一字段缺失/超长、`start_time` 无法解析等均在进入处理前被拒，数据库无任何变化
- **处理异常同样整批原子**：核验过程中出现任何异常，整批回滚，不留新流水或额度占用
- **并发完整前后态**：整批在**单个 `BEGIN IMMEDIATE` 事务**内完成，与并发的单场核验（`POST /verifications`）、场次取消、暂停/撤销登记共用同一把 SQLite 写锁完全串行；并发方只能观察到"整批之前"或"整批之后"的完整状态，**不会有别的核验穿插进本批中间而超额**，本批也不会穿插进别的写事务；取消先于整批则整批看到释放后的额度、整批先于取消则取消在整批之后生效
- **原接口不变**：`POST /verifications` 继续可用，其请求/响应（不含 `index`）与幂等语义保持原样；批量与单场产出的是同一张核验流水表，可互相重放、查询、取消、领取开场码

完整脚本演示见 `examples/demo_batch.sh`（`BASE_URL` 环境变量可改服务地址）。

撤销授权：

```bash
curl -X POST http://localhost:8000/licenses/3/revoke \
  -H 'Content-Type: application/json' -d '{"reason":"版权方临时撤片"}'
```

### 已核准场次取消

已核准且**尚未开场**的场次可按原核验幂等键取消（原因必填、非空白）：

```bash
curl -X POST http://localhost:8000/verifications/screening-0001/cancel \
  -H 'Content-Type: application/json' -d '{"reason":"放映设备故障，场次取消"}'
```

响应返回**取消时间、原因及关联授权和共享池**：

```json
{
  "id": 1,
  "idempotency_key": "screening-0001",
  "verification_id": 1,
  "license_id": 3,
  "pool_id": 1,
  "reason": "放映设备故障，场次取消",
  "cancelled_at": "2026-10-05T02:10:00.000000Z",
  "replay": false
}
```

- 取消成功后，该授权及所绑共享池的**实时已用次数各回退一次**（`/licenses`、`/pools` 立即可见），释放的额度可被后续核验使用
- 未知幂等键 → `404 {"code":"NOT_FOUND"}`；已拒绝的流水 → `422 {"code":"VERIFICATION_NOT_APPROVED"}`；场次已开场（开始时间已到）→ `422 {"code":"SESSION_ALREADY_STARTED"}`；原因为空白 → `422`，均**不留任何记录**
- **重复提交相同取消**返回首次记录（`replay=true`），额度不重复释放；同一键但**原因不同** → `409`，`existing` 字段附首次取消记录
- 原核验流水的 `decision`、扣减时余额快照（`remaining_quota`/`pool_remaining_quota`）及同键重放结果**保持原样**；核验查询（`GET /verifications`、`GET /verifications/{key}`）通过 `cancelled`/`cancellation` 字段可见取消状态：

```json
{
  "idempotency_key": "screening-0001",
  "decision": "approved",
  "remaining_quota": 4,
  "replay": false,
  "cancelled": true,
  "cancellation": {
    "reason": "放映设备故障，场次取消",
    "cancelled_at": "2026-10-05T02:10:00.000000Z"
  }
}
```

### 一次性开场码领取与场馆核销（本轮新增）

运营方按**原核验幂等键**，为已核准、未取消且**尚未到计划开场时刻**的场次领取一次性开场码。**每场仅有一个码**：首次领取返回原码，之后的重复领取与状态查询只能看到状态，原码不会再次出现。

领取（首次 200，`code` 仅此一次返回）：

```bash
curl -X POST http://localhost:8000/verifications/screening-0001/opening-code
```

```json
{
  "idempotency_key": "screening-0001",
  "status": "issued",
  "issued_at": "2026-10-05T11:20:00.000000Z",
  "cancelled_at": null,
  "redemption": null,
  "code": "OC-A5FA5BCB02B4BB7EA002350B",
  "replay": false
}
```

重复领取（或核销后/取消后再领）：`code` 恒为 `null`，只回状态，`replay=true`：

```json
{
  "idempotency_key": "screening-0001",
  "status": "issued",
  "issued_at": "2026-10-05T11:20:00.000000Z",
  "cancelled_at": null,
  "redemption": null,
  "code": null,
  "replay": true
}
```

状态查询（GET，响应体**没有 code 字段**，并在已核销时附首次回执）：

```bash
curl http://localhost:8000/verifications/screening-0001/opening-code
```

`status` 取值：

| 值 | 含义 |
|---|---|
| `not_issued` | 场次存在但从未领取过（已拒绝的流水也停留在此状态） |
| `issued` | 已领取，尚未核销、未取消 |
| `redeemed` | 已核销，`redemption` 字段附首次回执 |
| `voided` | 场次取消，未核销的码立即失效，`cancelled_at` 非空 |

场馆在**计划开场时刻起 30 分钟内**凭开场码及场馆编号核销，窗口左闭右开 `[start_time, start_time+30min)`：

```bash
curl -X POST http://localhost:8000/redemptions \
  -H 'Content-Type: application/json' \
  -d '{"code":"OC-A5FA5BCB02B4BB7EA002350B","venue_id":"hall-1"}'
```

成功返回**场次信息、实际核销时间与关联授权**（及所绑池）：

```json
{
  "id": 1,
  "idempotency_key": "screening-0001",
  "film_id": "film-autumn-river",
  "venue_id": "hall-1",
  "start_time": "2026-10-05T19:30:00.000000Z",
  "license_id": 3,
  "pool_id": null,
  "redeemed_at": "2026-10-05T19:30:12.000000Z",
  "replay": false
}
```

- 同一码成功核销后重试（即使已过 30 分钟窗口）：返回**首次回执** `replay=true`，`id`/`redeemed_at` 不变，**不重复记场次**；核销不改变授权与共享池的额度占用
- 错场馆重试（无论该码是否已核销）：一律 `422 {"code":"VENUE_MISMATCH"}`，已核销码的回执不会泄露给别的场馆
- 一切拒绝**都不产生回执**：

| HTTP | code | 触发条件 |
|---|---|---|
| 404 | `NOT_FOUND` | 领取/查询的幂等键对应核验流水不存在 |
| 404 | `OPENING_CODE_NOT_FOUND` | 核销用的开场码未知 |
| 422 | `VERIFICATION_NOT_APPROVED` | 为已拒绝的流水领取开场码 |
| 422 | `SESSION_ALREADY_STARTED` | 到达/已过计划开场时刻才领取开场码（取消接口沿用同名码） |
| 422 | `SESSION_CANCELLED` | 场次已取消：首次领取被拒；已领取的码状态变 `voided`，核销被拒 |
| 422 | `REDEMPTION_TOO_EARLY` | 早于计划开场时刻核销 |
| 422 | `REDEMPTION_WINDOW_CLOSED` | 到达开场后第 30 分钟仍未核销（未核销码此后无法核销） |
| 422 | `VENUE_MISMATCH` | 核销场馆编号与核准场次的场馆不一致 |

核销**不再校验授权**：授权在领取/核销之后被撤销或暂停，不影响该已核准场次的核销资格；原核验决定、扣减时余额快照及授权/池占用均不因核销而变化（授权/池的实时已用次数仍只统计未取消的 approved 流水）。

并发保证：开场码表以 `verification_id`/`idempotency_key`/`code` 三个唯一约束兜底，核销回执表以 `opening_code_id` 唯一约束兜底；领取、核销与取消都在 `BEGIN IMMEDIATE` 事务内完成，因此并发领取只会产生一个码、并发核销只会产生一条回执、领取与取消并发时结果必为"取消先到→领取 422 且无码"或"领取先到→取消后码变 `voided`"二者之一。

### 授权限时暂停

为授权 3 登记一个限时暂停（区间**左闭右开** `[suspend_from, suspend_to)`，时间必须带时区偏移，库内统一存 UTC；结束须严格晚于开始）：

```bash
curl -X POST http://localhost:8000/licenses/3/suspensions \
  -H 'Content-Type: application/json' \
  -d '{"suspend_from":"2026-10-05T00:00:00+08:00",
       "suspend_to":"2026-10-07T00:00:00+08:00",
       "reason":"版权方临时通知暂停两日"}'
```

响应返回**暂停编号、归一化到 UTC 的区间和原因**：

```json
{
  "id": 1,
  "license_id": 3,
  "suspend_from": "2026-10-04T16:00:00.000000Z",
  "suspend_to": "2026-10-06T16:00:00.000000Z",
  "reason": "版权方临时通知暂停两日",
  "created_at": "2026-10-05T01:20:00.000000Z"
}
```

- 授权不存在 → `404 {"code":"NOT_FOUND"}`；时刻不带时区 / 无法解析 / 结束不晚于开始 / 原因为空白 → `422`，均**不留任何记录**
- 同一授权可重复登记、区间允许重叠，核验时**重叠部分按并集生效**
- 记录登记后**不可修改、不可删除**（`PUT`/`PATCH`/`DELETE` 返回 405）

暂停影响下的核验（仅影响暂停登记**之后提交**、且场次开始时间落入区间的新核验）：

```json
{
  "decision": "rejected",
  "license_id": null,
  "pool_id": null,
  "reject_reason": "LICENSE_SUSPENDED",
  "remaining_quota": null,
  "pool_remaining_quota": null,
  "replay": false
}
```

查询暂停记录：

```bash
curl http://localhost:8000/licenses/3/suspensions     # 按授权
curl 'http://localhost:8000/suspensions?license_id=3' # 全局列表过滤
curl http://localhost:8000/suspensions/1              # 按暂停编号
```

### 影片级共享额度池（跨场馆总放映次数）

登记池：

```bash
curl -X POST http://localhost:8000/pools \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","max_sessions":3,"note":"跨场馆总放映次数"}'
```

录入授权时用 `pool_id` 绑定（绑定后不可修改）：

```bash
curl -X POST http://localhost:8000/licenses \
  -H 'Content-Type: application/json' \
  -d '{"film_id":"film-autumn-river","venue_id":"hall-2",
       "valid_from":"2026-10-01T00:00:00Z","valid_to":"2026-10-31T23:59:59Z",
       "max_sessions":10,"pool_id":1}'
```

- 池不存在 → `422 {"code":"POOL_NOT_FOUND"}`；池的影片与授权影片不符 → `422 {"code":"POOL_FILM_MISMATCH"}`
- 不传 `pool_id` 的授权（含所有旧数据）保持原语义，核验不检查池
- 绑定池的授权核准时，响应额外给出 `pool_id` 与消耗后的 `pool_remaining_quota`：

```json
{
  "decision": "approved",
  "license_id": 4,
  "pool_id": 1,
  "reject_reason": null,
  "remaining_quota": 9,
  "pool_remaining_quota": 2,
  "replay": false
}
```

池耗尽时（授权自身仍有余额）拒绝：

```json
{
  "decision": "rejected",
  "license_id": null,
  "pool_id": null,
  "reject_reason": "POOL_EXHAUSTED",
  "remaining_quota": null,
  "pool_remaining_quota": null,
  "replay": false
}
```

查询池与授权展示一致的已用/剩余次数（同一批 approved 流水同时计入两者）：

```bash
curl http://localhost:8000/pools/1
curl 'http://localhost:8000/licenses?pool_id=1'
```

## 核验规则说明

**授权选择**：取该 `影片+场馆` 的全部授权，按 `valid_to` 升序遍历，命中第一个"覆盖开始时间、未撤销、**未暂停**、自身剩余额度 > 0，且**未绑池或所绑池剩余 > 0**"的授权——即最早到期且自身与池均有余额者；一次核验只消耗一份额度，且授权与其所绑池**各消耗一次**（`remaining_quota` / `pool_remaining_quota` 为消耗后的剩余值）。

**限时暂停**：暂停是挂在**单份授权**上的左闭右开区间 `[suspend_from, suspend_to)`，按场次开始时间判定（`suspend_from <= start < suspend_to`）；同一授权多条暂停的重叠部分按并集生效。暂停只影响**暂停登记之后提交**的新核验：核验时暂停授权与撤销授权一样被跳过；暂停**之前**已存在的核准/拒绝流水、授权已用额度与池占用一律不变；同一幂等键重放始终返回首次结果（含首次的批准），不重新评估暂停。暂停不退还任何额度，区间结束后授权自动恢复可用；永久撤销仍然优先阻止该授权（已撤销+已暂停按撤销处理）。暂停记录一经登记不可修改、不可删除。

**共享额度池**：池为影片级，被同一影片下任意场馆的绑定授权共享。池的已用次数等于所有绑定该池的授权产生的 `approved` 流水数；未绑定池的授权保持旧语义。池一经授权绑定不可更换、不可解绑；未提供删池接口。

**幂等**：`idempotency_key` 全库唯一。重试同一键且请求内容（影片、场馆、开始时间）一致 → 返回首次存储的结果并标记 `replay=true`，不重复扣额度、**不重复占用池**、也不受首次登记之后新增的暂停影响；内容不一致 → `409` 并在 `existing` 字段附上首次结果。**集中批量核验**（`POST /verifications/batch`，1～100 项）逐项复用同一套幂等判定：同批内重复键或与历史同键同内容均返回原流水（`replay=true`）、不重复扣额；同键不同内容则**整批 409**（响应带 `index` 指出冲突项位置）并整体回滚。

**场次取消**：取消记录独立存表（每条核验流水至多一条，唯一约束兜底）。仅**已核准且场次开始时间未到**的流水可取消；取消不回写原核验行——`decision` 仍为 `approved`、扣减时余额快照不变、同键重放仍返回首次结果——但实时已用次数统计（授权与所绑池）会**排除已取消流水**，即各回退一次，释放额度立即可被后续核验使用。取消本身按原幂等键幂等：同键同原因重放返回首次记录（`replay=true`），同键不同原因 `409`。已取消流水在核验查询中标记 `cancelled=true` 并附 `cancellation`（原因、取消时间），`decision` 过滤仍按其原决定归类。

**一次性开场码与核销**：开场码独立存表（每场至多一个：`verification_id`、`idempotency_key`、`code` 均唯一）。领取门槛与取消相同——已核准、未取消且计划开场时刻未到；首次领取生成随机码（`OC-` 前缀 + 12 字节随机十六进制）并返回，之后的重复领取、GET 状态查询**都不读不回原码**。状态由码、回执、取消三表联合导出：`not_issued`（无码行）→ `issued`（有码无回执无取消）→ `redeemed`（有回执）/`voided`（有取消；取消先于领取则根本不建行）。核销在计划开场时刻起 30 分钟的左闭右开窗口内进行，场馆编号必须与核准场馆一致；一切失败回滚、不留回执。回执独立存表（每码至多一条：`opening_code_id` 唯一），成功后任何重试都返回首次回执（`replay=true`），即使窗口已关闭、即使授权此后被撤销/暂停；错场馆的重试恒为 `VENUE_MISMATCH`，无法借此探测他馆场次。核销路径**完全不读取授权状态、不触碰额度/池计数**，因此撤销/暂停与核销彻底解耦，原核验结果与额度占用保持原样。

**撤销**：撤销记录独立存表。被撤销的授权不再参与后续核验；撤销前已核准的场次记录及其池占用原样保留、**不予退还**，可通过 `/verifications` 与 `/verifications/{key}` 追溯，其已领取的开场码仍可在窗口内正常核销。

**并发**：所有核验在 `BEGIN IMMEDIATE` 事务内完成"查重 → 读暂停 → 选授权 → 扣额度（含池） → 落库"，暂停登记、场次取消、开场码领取与场馆核销同样以 `BEGIN IMMEDIATE` 串行写入；**集中批量核验把全批 1～100 项放在同一个 `BEGIN IMMEDIATE` 事务内按序处理**，整批只在全部成功后一次提交。SQLite 写锁将所有写请求统一串行化，因此暂停/取消/领取/核销登记与核验并发、以及批量核验与单场核验/取消并发时，**每次决定都对应登记前或登记后的完整状态**——并发方只能看到"整批之前"或"整批之后"的状态，不会读到"只生效一半"，不会有别的写事务穿插进整批中间而超额，取消与核验并发也**不会超额或重复释放**（取消的唯一约束保证每条流水至多释放一次）。批量任一同键异义冲突或处理异常均在提交前整体回滚：**不留任何新流水、不占用任何额度/池额度**。开场码以 `verification_id`/`code` 唯一约束保证并发领取每场只产生一个码、原码只在首次返回；核销回执以 `opening_code_id` 唯一约束保证每码至多一条回执，并发重试由锁判定唯一获胜者、其余全部返回首次回执；领取与取消并发时两表唯一约束共同保证结果必为"取消先到→无码且领取 422"或"领取先到→取消后码变 `voided`"二者之一。WAL 模式 + 30s busy_timeout 保证读写不互相阻塞。测试以 20 线程并发抢 3 场授权额度、40 线程跨 4 场馆抢容量 5 的池、20 路核验与 1 路暂停登记同时开跑、20 路核验与 1 路取消并发及 10 路同键并发取消、20 路同场次并发领取开场码、10 路同码并发核销及 10 路领取与 1 路取消竞态、10 单场核验与 2 个 10 项批次并发、两批次并发以及 10 轮"整批与取消同栅并发"验证：批准数恰好等于上限、其余按原因为 `QUOTA_EXHAUSTED` / `POOL_EXHAUSTED` / `LICENSE_SUSPENDED` 拒绝，额度占用与批准数严格一致，取消后实时已用次数恰好回退一次，批量-取消并发结果恒为两种完整前后态之一，开场码与核销回执各恰好一个。

**拒绝原因**（`reject_reason`）：

| 值 | 含义 |
|---|---|
| `NO_LICENSE` | 该影片+场馆没有任何授权 |
| `TIME_OUT_OF_WINDOW` | 有授权，但开始时间不在任何有效时段内 |
| `LICENSE_REVOKED` | 覆盖该时段的授权均已撤销 |
| `LICENSE_SUSPENDED` | 存在覆盖该时段且未撤销的授权，但它们在该时刻**全部处于暂停中** |
| `QUOTA_EXHAUSTED` | 授权有效但场次自身额度已用完 |
| `POOL_EXHAUSTED` | 时段内未撤销、未暂停的授权仍有自身余额，却都因所绑池耗尽而不可用 |

拒绝原因按上表顺序判定（旧原因优先级不变）：先判定无授权/时间外/全部撤销；随后若覆盖时段且未撤销的授权**全部暂停**即返回 `LICENSE_SUSPENDED`（即使它们自身额度也已用尽）；否则**跳过暂停授权**后沿用原有判定与次序——例如某授权自身余额尚在、但池耗尽，同时另一份授权自身额度也已用完时，只要仍存在"自身有余额但仅被池卡住"的未暂停授权，即返回 `POOL_EXHAUSTED`；未绑池的授权不受池影响。

**时间处理**：核验场次时间按 ISO-8601 解析，不带时区按 UTC 处理，无法解析返回 422；**暂停起止时刻必须显式带时区偏移**（如 `Z` 或 `+08:00`），不带时区、无法解析或结束不晚于开始均返回 422。库内统一存定长 UTC 字符串，保证比较即时间序。

## 项目结构

```
app/
  main.py       # FastAPI 入口与路由（create_app 工厂）
  service.py    # 业务逻辑：额度池/授权/撤销/限时暂停/单场与集中批量幂等核验/场次取消/授权选择/一次性开场码/场馆核销
  db.py         # SQLite 连接与建表（WAL、外键、busy_timeout、旧库迁移）
  schemas.py    # Pydantic 请求/响应模型（含 1～100 项批量核验模型）
  timeutil.py   # UTC 时间规范化
tests/
  test_api.py           # API 端到端测试 + 并发不超额测试
  test_batch_verifications.py # 集中批量核验：顺序扣额/拒绝留痕/同批与历史重放/同键异义整批回滚/格式错误/异常回滚/与单场及取消并发不穿插超额
  test_pools.py         # 共享额度池：绑定校验/跨场馆/池耗尽/撤销不退/并发不突破上限
  test_suspensions.py    # 限时暂停：登记校验/左闭右开/并集/跳过选择/拒绝原因次序/幂等/并发状态一致
  test_cancellations.py  # 场次取消：登记校验/额度与池回退/幂等重放/原因冲突/原样保持/并发不超额不重复释放
  test_opening_codes.py  # 开场码：领取唯一/状态不泄露码/30 分钟左闭右开窗口/场馆不符/取消失效/回执重放/撤销暂停不影响核销/并发码与回执唯一
examples/
  demo.sh                # 完整调用示例脚本
  demo_batch.sh          # 集中批量核验调用示例脚本（顺序扣额/重放/整批失败回滚/取消后复用）
  demo_pools.sh          # 共享额度池调用示例脚本
  demo_suspensions.sh    # 授权限时暂停调用示例脚本
  demo_cancellations.sh  # 已核准场次取消调用示例脚本
  demo_opening_codes.sh  # 一次性开场码领取与场馆核销调用示例脚本（实时时钟演示）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```

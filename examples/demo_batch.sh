#!/usr/bin/env bash
# 集中批量核验调用示例。启动服务后执行：bash examples/demo_batch.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-batch-demo"
HALL="hall-batch"
T0="2030-06-01T19:30:00Z"
T1="2030-06-02T19:30:00Z"
T2="2030-06-03T19:30:00Z"
T3="2030-06-04T19:30:00Z"
T5="2030-06-06T19:30:00Z"

batch() {  # $1 = JSON items 数组体
  curl -s -X POST "$BASE/verifications/batch" \
    -H 'Content-Type: application/json' \
    -d "{\"items\": $1}"
}

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入授权（额度 2）：前 2 项将被核准，第 3 项起 QUOTA_EXHAUSTED =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"$HALL\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 2, \"note\": \"批量核验演示授权\"
}" | pretty

echo
echo "== 2. 一批 4 项，按输入顺序返回每项结果与输入位置 index =="
echo "-- 第 0/1 项 approved（剩余 1/0）；第 2 项是第 0 项的同键同内容重放 replay=true；"
echo "-- 第 3 项因前项已耗尽额度而 rejected=QUOTA_EXHAUSTED，拒绝也落流水并继续处理。 --"
batch "[
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T0\",\"idempotency_key\":\"batch-1\"},
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T1\",\"idempotency_key\":\"batch-2\"},
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T0\",\"idempotency_key\":\"batch-1\"},
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T2\",\"idempotency_key\":\"batch-3\"}
]" | pretty

echo
echo "== 3. 授权实时已用恰为 2：同键重放与业务拒绝都不额外扣额 =="
curl -s "$BASE/licenses?film_id=$FILM" | pretty

echo
echo "== 4. 拒绝流水同样可追溯（batch-3） =="
curl -s "$BASE/verifications/batch-3" | pretty

echo
echo "== 5. 整批重放：全部返回原流水 replay=true，不产生新流水、不扣额 =="
batch "[
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T0\",\"idempotency_key\":\"batch-1\"},
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T2\",\"idempotency_key\":\"batch-3\"}
]" | pretty

echo
echo "== 6. 同键不同内容：整批失败 409，响应带 index=1 与 existing 原流水 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/batch" \
  -H 'Content-Type: application/json' -d "{
  \"items\": [
    {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T5\",\"idempotency_key\":\"brand-new-x\"},
    {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T1\",\"idempotency_key\":\"batch-1\"}
  ]
}"

echo
echo "== 7. 上一批整体回滚：brand-new-x 未落库（404），授权已用仍为 2 =="
curl -s -o /dev/null -w 'GET /verifications/brand-new-x -> HTTP %{http_code}\n' \
  "$BASE/verifications/brand-new-x"
curl -s "$BASE/licenses?film_id=$FILM" | pretty

echo
echo "== 8. 格式错误整批拒绝（422）：时间非法 / 空批次 / 超过 100 项，均不落流水 =="
curl -s -o /dev/null -w 'bad start_time -> HTTP %{http_code}\n' \
  -X POST "$BASE/verifications/batch" -H 'Content-Type: application/json' -d "{
  \"items\": [
    {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"not-a-time\",\"idempotency_key\":\"bad-time\"}
  ]
}"
curl -s -o /dev/null -w 'empty items     -> HTTP %{http_code}\n' \
  -X POST "$BASE/verifications/batch" -H 'Content-Type: application/json' -d '{"items": []}'

echo
echo "== 9. 无授权的影片在批次中只影响自己那一项：拒绝留痕，其余继续 =="
batch "[
  {\"film_id\":\"no-such-film\",\"venue_id\":\"$HALL\",\"start_time\":\"$T3\",\"idempotency_key\":\"batch-no-license\"},
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T3\",\"idempotency_key\":\"batch-4\"}
]" | pretty

echo
echo "== 10. 单场核验接口保持不变、继续可用（此处被拒 QUOTA_EXHAUSTED） =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications" \
  -H 'Content-Type: application/json' -d "{
  \"film_id\":\"$FILM\", \"venue_id\":\"$HALL\",
  \"start_time\":\"$T3\", \"idempotency_key\":\"single-still-works\"
}"

echo
echo "== 11. 取消释放的额度可被后续整批使用：取消 batch-1（未开场）后再提一批 =="
curl -s -X POST "$BASE/verifications/batch-1/cancel" \
  -H 'Content-Type: application/json' -d '{"reason":"设备故障，取消以释放额度"}' | pretty
echo "-- 新批次第 0 项获得释放出的额度 approved，第 1 项仍 rejected --"
batch "[
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T3\",\"idempotency_key\":\"batch-after-cancel-1\"},
  {\"film_id\":\"$FILM\",\"venue_id\":\"$HALL\",\"start_time\":\"$T5\",\"idempotency_key\":\"batch-after-cancel-2\"}
]" | pretty

echo
echo "== 12. 核验流水列表：本演示全部条目（含拒绝、含取消状态）可追溯 =="
curl -s "$BASE/verifications?film_id=$FILM" | pretty

#!/usr/bin/env bash
# 场次改签调用示例。启动服务后执行：bash examples/demo_reschedules.sh
set -euo pipefail

BASE="${BASE_URL:-http://localhost:8000}"
pretty() { if command -v jq >/dev/null 2>&1; then jq .; else cat; fi; }

FILM="film-reschedule-demo"

echo "== 0. 健康检查 =="
curl -s "$BASE/health" | pretty

echo
echo "== 1. 录入授权 A（上限 1，2030-07-01 到期）与授权 B（上限 1，2031-01-01 到期） =="
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2030-07-01T00:00:00Z\",
  \"max_sessions\": 1, \"note\": \"改签演示授权 A（早到期）\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 1, \"note\": \"改签演示授权 B（兜底）\"
}" | pretty

echo
echo "== 2. 核准场次 resched-demo-1（2030-06-01）：命中早到期授权 A，A 已占满 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-06-01T19:30:00Z\", \"idempotency_key\": \"resched-demo-1\"
}" | pretty

echo
echo "== 3. 改签 2030-06-01 -> 2030-06-02：授权 A 虽已占满，本场次的当前占用 =="
echo "-- 参与本次释放后的可用额度，故仍核准（from/to 均为授权 A） --"
curl -s -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-1", "new_start_time": "2030-06-02T19:30:00Z"
}' | pretty

echo
echo "== 4. 核准场次 resched-demo-2（2030-06-03）：A 已满，命中授权 B，B 也占满 =="
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2030-06-03T19:30:00Z\", \"idempotency_key\": \"resched-demo-2\"
}" | pretty

echo
echo "== 5. 把 resched-demo-1 改到只被授权 B 覆盖的 2030-08-01：B 已被占满 =="
echo "-- 422 RESCHEDULE_REJECTED，附既有拒绝原因 QUOTA_EXHAUSTED，排片与占用不变 --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-2", "new_start_time": "2030-08-01T19:30:00Z"
}'
echo "-- 当前排片不变、改签历史仍只有第 3 步一条；失败不消耗操作键 --"
curl -s "$BASE/verifications/resched-demo-1" | pretty
curl -s "$BASE/verifications/resched-demo-1/reschedules" | pretty

echo
echo "== 6. 取消 resched-demo-2 释放授权 B 后，同一操作键重试即成功：A->B 迁移 =="
curl -s -X POST "$BASE/verifications/resched-demo-2/cancel" \
  -H 'Content-Type: application/json' -d '{"reason": "放映计划调整，场次取消"}' | pretty
curl -s -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-2", "new_start_time": "2030-08-01T19:30:00Z"
}' | pretty
echo "-- 授权 A 已用 1->0（释放一次），授权 B 已用 0->1（增加一次） --"
curl -s "$BASE/licenses?film_id=$FILM" | pretty

echo
echo "== 7. 同操作键同内容重放：返回首次记录（replay=true），不重复迁移 =="
curl -s -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-2", "new_start_time": "2030-08-01T19:30:00Z"
}' | pretty

echo
echo "== 8. 同操作键异内容：409 冲突，existing 附首次改签记录 =="
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-2", "new_start_time": "2030-09-01T19:30:00Z"
}'

echo
echo "== 9. 再次改签（新操作键）2030-08-01 -> 2030-06-05：按当前排片处理，移回授权 A =="
curl -s -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-3", "new_start_time": "2030-06-05T19:30:00Z"
}' | pretty
echo "-- 改签历史累计三条，当前排片以最后一次为准 --"
curl -s "$BASE/verifications/resched-demo-1/reschedules" | pretty

echo
echo "== 10. 共享池场景：池上限 1，授权 C 绑池；场次在池内改签仍只占用一次 =="
curl -s -X POST "$BASE/pools" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"max_sessions\": 1, \"note\": \"改签演示池\"
}" | pretty
curl -s -X POST "$BASE/licenses" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-2\",
  \"valid_from\": \"2020-01-01T00:00:00Z\", \"valid_to\": \"2031-01-01T00:00:00Z\",
  \"max_sessions\": 5, \"pool_id\": 1, \"note\": \"改签演示授权 C（绑池）\"
}" | pretty
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-2\",
  \"start_time\": \"2030-06-10T19:30:00Z\", \"idempotency_key\": \"resched-demo-9\"
}" | pretty
curl -s -X POST "$BASE/verifications/resched-demo-9/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-9", "new_start_time": "2030-06-11T19:30:00Z"
}' | pretty
echo "-- 池实时已用仍为 1（原占用释放一次、新占用增加一次，自身参与判定） --"
curl -s "$BASE/pools/1" | pretty

echo
echo "== 11. 资格与参数校验（均不留记录、不改排片） =="
echo "-- 已取消的场次：422 SESSION_CANCELLED --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-2/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-4", "new_start_time": "2030-06-04T19:30:00Z"
}'
echo
echo "-- 已领取开场码的场次：422 OPENING_CODE_ALREADY_ISSUED --"
curl -s -X POST "$BASE/verifications/resched-demo-9/opening-code" | pretty
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-9/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-5", "new_start_time": "2030-06-12T19:30:00Z"
}'
echo
echo "-- 已开场的场次：422 SESSION_ALREADY_STARTED --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"$FILM\", \"venue_id\": \"hall-1\",
  \"start_time\": \"2020-06-01T19:30:00Z\", \"idempotency_key\": \"resched-demo-past\"
}" | pretty
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-past/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-6", "new_start_time": "2030-06-06T19:30:00Z"
}'
echo
echo "-- 已拒绝的流水：422 VERIFICATION_NOT_APPROVED --"
curl -s -X POST "$BASE/verifications" -H 'Content-Type: application/json' -d "{
  \"film_id\": \"film-no-license\", \"venue_id\": \"hall-9\",
  \"start_time\": \"2030-06-01T19:30:00Z\", \"idempotency_key\": \"resched-demo-rej\"
}" | pretty
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-rej/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-7", "new_start_time": "2030-06-07T19:30:00Z"
}'
echo
echo "-- 新时间须晚于当前时刻：422 NEW_START_NOT_IN_FUTURE --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-8", "new_start_time": "2020-06-01T19:30:00Z"
}'
echo
echo "-- 新时间须不同于当前排片：422 NEW_START_UNCHANGED --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/resched-demo-1/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-8", "new_start_time": "2030-06-05T19:30:00Z"
}'
echo
echo "-- 未知幂等键：404 NOT_FOUND --"
curl -s -w '\nHTTP %{http_code}\n' -X POST "$BASE/verifications/no-such-key/reschedule" \
  -H 'Content-Type: application/json' -d '{
  "operation_key": "resched-op-10", "new_start_time": "2030-06-08T19:30:00Z"
}'

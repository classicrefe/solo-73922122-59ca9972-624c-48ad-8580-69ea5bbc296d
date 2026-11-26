#!/usr/bin/env bash
# 并发返印演示：报废提交后「排程还在算」期间，查单 / 他单操作 / 完工确认
# 都能立即完成，并验证生效时冲突规则：
#   A. 拟报废印张先凭有效派工号确认完工 -> 整批 scrap 返回 409，不报废、不改派
#   B. 先完工的是其他印张               -> 留在原位，按最新状态为剩余位置重排
#   C. 并发报废：相同批只有一个成功(200 vs 409)；不同批各自生效、最终都登记
#
# 用法:
#   ./demo-concurrent.sh [base_url]
#
# 为稳定看到「计算进行中」的窗口，请让服务端带重排延迟启动（默认 0 时择纸
# 太快，可能来不及插入请求）：
#   REPLAN_DELAY_SECONDS=2 docker compose up --build
# 或:
#   docker compose -f docker-compose.yml -f docker-compose.demo.yml up --build
set -u

BASE="${1:-http://localhost:8000}"
PAUSE="${DEMO_PAUSE:-0.7}"   # 等后台 scrap 进入计算窗口
PY="$(command -v python3 || command -v python)"
TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT
SCRAP_PID=""

pick() { "$PY" -c "import sys,json; d=json.load(sys.stdin); print($1)"; }
show() { "$PY" -m json.tool --no-ensure-ascii 2>/dev/null || cat; }
# 紧凑打印工单的报废集合与位置
summ() { "$PY" -c '
import sys, json
d = json.load(sys.stdin)
print("  scrapped:", d["scrapped_sheets"])
for p in d["positions"]:
    print("  pos", p["position"], ": use=", p["use"], " status=", p["status"],
          " dispatch=", p["dispatch_id"][:8], " faces=", p["faces_to_print"])
'; }

echo "== 0. 健康检查 ($BASE) =="
curl -sS "$BASE/health" | show

# 标准两位置工单：pos0->S1, pos1->S2, S3 用不上
ORDER_BODY='{
  "pages": ["COVER", "P2", "P3", "P4", "P5"],
  "old_sheets": [
    {"id": "S1", "front": {"left": null, "right": "COVER"}, "back": null},
    {"id": "S2", "front": null, "back": {"left": "P4", "right": "P5"}},
    {"id": "S3", "front": {"left": "OLD", "right": "STUFF"}, "back": null}
  ]}'

create_order() { # echoes oid; full json -> $TMPD/last.json
  curl -sS -X POST "$BASE/work-orders" -H 'Content-Type: application/json' \
    -d "$ORDER_BODY" > "$TMPD/last.json"
  pick "d['work_order_id']" < "$TMPD/last.json"
}

# scrap_bg OID JSON_IDS TAG：在当前 shell 派生后台 scrap，PID 写入 SCRAP_PID；
# 结果落在 $TMPD/TAG.{code,body}
scrap_bg() {
  local oid="$1" ids="$2" tag="$3"
  curl -sS -o "$TMPD/$tag.body" -w "%{http_code}" \
    -X POST "$BASE/work-orders/$oid/scrap" \
    -H 'Content-Type: application/json' -d "{\"sheet_ids\":$ids}" \
    > "$TMPD/$tag.code" &
  SCRAP_PID=$!
}

warn_if_window_missed() { # tag
  if [ -s "$TMPD/$1.code" ]; then
    echo "  [警告] scrap 在插入请求前就结束了；请用 REPLAN_DELAY_SECONDS=2 启动服务后重试。"
  fi
}

echo
echo "################################################################"
echo "# 场景 A：拟报废印张先被有效派工确认完工 -> 整批 409，无副作用  #"
echo "################################################################"
OID_A="$(create_order)"; D_A1="$(pick "d['positions'][1]['dispatch_id']" < "$TMPD/last.json")"
OID_B="$(create_order)"; D_B0="$(pick "d['positions'][0]['dispatch_id']" < "$TMPD/last.json")"
echo "工单 A=$OID_A (pos1 dispatch ${D_A1:0:8})；他单 B=$OID_B"

scrap_bg "$OID_A" '["S1","S2"]' a; PID_A="$SCRAP_PID"
sleep "$PAUSE"
warn_if_window_missed a

echo "-- A1. 重排计算中：查同单 A，只显示已生效状态（未见报废/改派） --"
curl -sS "$BASE/work-orders/$OID_A" | summ

echo "-- A2. 计算中：查他单 B / 列表，均即时返回 --"
curl -sS -o /dev/null -w "  GET B -> %{http_code} (%{time_total}s)\n" "$BASE/work-orders/$OID_B"
curl -sS -o /dev/null -w "  GET /work-orders -> %{http_code} (%{time_total}s)\n" "$BASE/work-orders"

echo "-- A3. 计算中：他单 B 的 pos0 凭派工确认完工 --"
curl -sS -o /dev/null -w "  confirm B pos0 -> %{http_code} (%{time_total}s)\n" \
  -X POST "$BASE/work-orders/$OID_B/positions/0/confirm" \
  -H 'Content-Type: application/json' -d "{\"dispatch_id\":\"$D_B0\"}"

echo "-- A4. 拟报废的 S2 所在 pos1 先凭有效派工号确认完工 --"
curl -sS -o /dev/null -w "  confirm A pos1 -> %{http_code} (%{time_total}s)\n" \
  -X POST "$BASE/work-orders/$OID_A/positions/1/confirm" \
  -H 'Content-Type: application/json' -d "{\"dispatch_id\":\"$D_A1\"}"

wait "$PID_A"
CODE_A="$(cat "$TMPD/a.code")"
echo "-- A5. scrap 整批 [S1,S2] 最终状态：HTTP $CODE_A（期望 409） --"
"$PY" -c "import json; print('  detail:', json.load(open('$TMPD/a.body')).get('detail'))"
[ "$CODE_A" = "409" ] && echo "  PASS: 整批 409" || echo "  FAIL: 期望 409，实得 $CODE_A"

echo "-- A6. 复查 A：无报废登记、无改派；pos1 completed 且派工号不变 --"
curl -sS "$BASE/work-orders/$OID_A" | summ

echo
echo "################################################################"
echo "# 场景 B：先完工的是其他印张 S1 -> 留原位，剩余位置按最新态重排  #"
echo "################################################################"
OID_C="$(create_order)"; D_C0="$(pick "d['positions'][0]['dispatch_id']" < "$TMPD/last.json")"
D_C1_OLD="$(pick "d['positions'][1]['dispatch_id']" < "$TMPD/last.json")"
echo "工单 C=$OID_C"

scrap_bg "$OID_C" '["S2"]' c; PID_C="$SCRAP_PID"
sleep "$PAUSE"
warn_if_window_missed c

echo "-- B1. 重排计算中查 C（仍是上一个生效状态） --"
curl -sS "$BASE/work-orders/$OID_C" | summ

echo "-- B2. 其他印张 S1 的 pos0 先确认完工 --"
curl -sS -o /dev/null -w "  confirm C pos0 -> %{http_code} (%{time_total}s)\n" \
  -X POST "$BASE/work-orders/$OID_C/positions/0/confirm" \
  -H 'Content-Type: application/json' -d "{\"dispatch_id\":\"$D_C0\"}"

wait "$PID_C"
CODE_C="$(cat "$TMPD/c.code")"
echo "-- B3. scrap [S2] 最终状态：HTTP $CODE_C（期望 200；状态变化会触发一次最新态重排） --"
"$PY" - "$TMPD/c.body" "$TMPD/d_c1_new" <<'PYEOF'
import json, sys
body_path, out_path = sys.argv[1], sys.argv[2]
d = json.load(open(body_path))
print("  returned_dispatches:", d.get("returned_dispatches"))
for p in d["positions"]:
    print("  pos", p["position"], ": use=", p["use"], "status=", p["status"],
          "faces=", p["faces_to_print"], "dispatch=", p["dispatch_id"][:8])
new_id = next(p["dispatch_id"] for p in d["positions"] if p["position"] == 1)
open(out_path, "w").write(new_id)
PYEOF
D_C1_NEW="$(cat "$TMPD/d_c1_new")"

echo "-- B4. 旧派工确认 -> 409；新派工确认 -> 200；完工 pos0 原派工仍有效(幂等200) --"
curl -sS -o /dev/null -w "  old dispatch -> %{http_code} (期望 409)\n" \
  -X POST "$BASE/work-orders/$OID_C/positions/1/confirm" \
  -H 'Content-Type: application/json' -d "{\"dispatch_id\":\"$D_C1_OLD\"}"
curl -sS -o /dev/null -w "  new dispatch -> %{http_code} (期望 200)\n" \
  -X POST "$BASE/work-orders/$OID_C/positions/1/confirm" \
  -H 'Content-Type: application/json' -d "{\"dispatch_id\":\"$D_C1_NEW\"}"
curl -sS -o /dev/null -w "  pos0 repeat  -> %{http_code} (期望 200)\n" \
  -X POST "$BASE/work-orders/$OID_C/positions/0/confirm" \
  -H 'Content-Type: application/json' -d "{\"dispatch_id\":\"$D_C0\"}"

echo
echo "################################################################"
echo "# 场景 C：并发报废，各自按生效时状态判断                         #"
echo "################################################################"
OID_D="$(create_order)"
echo "-- C1. 同一批 [S2] 并发 3 个请求（期望恰有 1 个 200，其余 409） --"
scrap_bg "$OID_D" '["S2"]' d1; P1="$SCRAP_PID"
scrap_bg "$OID_D" '["S2"]' d2; P2="$SCRAP_PID"
scrap_bg "$OID_D" '["S2"]' d3; P3="$SCRAP_PID"
wait "$P1" "$P2" "$P3"
CODES_D="$(cat "$TMPD/d1.code") $(cat "$TMPD/d2.code") $(cat "$TMPD/d3.code")"
echo "  codes: $CODES_D"
curl -sS "$BASE/work-orders/$OID_D" | summ

OID_E="$(create_order)"
echo "-- C2. 不同批 [S2] 与 [S3] 并发（期望均 200，最终 scraps=[S2,S3]） --"
scrap_bg "$OID_E" '["S2"]' e2; Q1="$SCRAP_PID"
scrap_bg "$OID_E" '["S3"]' e3; Q2="$SCRAP_PID"
wait "$Q1" "$Q2"
echo "  codes: $(cat "$TMPD/e2.code") $(cat "$TMPD/e3.code")"
curl -sS "$BASE/work-orders/$OID_E" | summ

echo
echo "演示完成。接口未变：POST /work-orders、GET /work-orders[/{id}]、"
echo "POST /work-orders/{id}/positions/{i}/confirm、POST /work-orders/{id}/scrap。"

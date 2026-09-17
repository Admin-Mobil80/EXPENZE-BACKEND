"""Pretty-print one audit response from the ExpensifyAI API."""
import json
import sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print(f"  non-JSON response: {raw[:400]}")
    sys.exit(0)

if "error" in data:
    print(f"  ERROR: {data['error']}")
    sys.exit(0)

v = data.get("verdict", {})
print(f"  verdict     : {v.get('verdict')}")
print(f"  currency    : {v.get('currency')}")
print(f"  receipt     : {v.get('receipt_total')}")
print(f"  reimbursable: {v.get('reimbursable_total')}")
print(f"  disallowed  : {v.get('disallowed_total')}")
print(f"  attendees   : {v.get('attendee_count')} ({v.get('attendee_count_source')})")
for item in v.get("violations", []):
    print(f"  ! {item['code']} - {item['message']}")
rationale = (data.get("rationale") or "").strip()
if rationale:
    print(f"  rationale   : {rationale[:240]}")

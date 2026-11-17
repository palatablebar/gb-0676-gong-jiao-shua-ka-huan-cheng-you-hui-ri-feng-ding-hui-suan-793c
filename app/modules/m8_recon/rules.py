"""M8 坏卡换卡同人权益并账引擎（纯函数、确定性、分币守恒）。

裁决（凭证未齐时）：先保留候选权益，不做正式并账。
- 候选：以同人疑似换卡链预占一份同人日额度（PROVISIONAL），当天服务结果按分卡结果交付，
  候选差额挂账可见但不入正式减免，待换卡凭证到达后再转正并账（CONFIRMED）。
- 每张卡的每条原始刷卡只保留一条现行权益结果；同一补证指令幂等，重复提交不重复减免。
"""
from decimal import Decimal
from app.values import number, money, instant
from app.errors import DomainError

ZERO = Decimal("0.00")

# 同人疑似换卡链的启发式窗口：相邻两卡首末行程间隔不超过该分钟数，才生成候选链接。
CANDIDATE_GAP_MINUTES = 120


def _sorted_rides(rows):
    return sorted(rows, key=lambda r: (instant(r["at"]), r["event_id"]))


def settle_chain(rides, cap, off, window):
    """对一条同人卡链按时间序结算，复刻 M5 的优惠→封顶裁决顺序，返回逐行程结果。"""
    spent = ZERO
    anchor = None
    out = []
    for row in _sorted_rides(rides):
        base = number(row["base_fare"])
        eligible = False
        if anchor is not None:
            delta = Decimal(str((instant(row["at"]) - instant(anchor["at"])).total_seconds())) / 60
            eligible = anchor["route"] != row["route"] and Decimal(0) <= delta < window
        transfer = min(base, off) if eligible else ZERO
        after_transfer = base - transfer
        remaining = max(ZERO, cap - spent)
        charge = min(after_transfer, remaining)
        cap_discount = after_transfer - charge
        spent += charge
        out.append({
            "event_id": row["event_id"], "card": row["card"], "route": row["route"],
            "business_day": row["business_day"], "at": row["at"],
            "base_fare": money(base), "transfer_anchor": anchor["event_id"] if eligible else "—",
            "transfer_discount": money(transfer), "cap_discount": money(cap_discount),
            "expected": money(charge), "day_paid": money(spent),
            "_sources": row.get("_sources", [row.get("_line", 0)]),
        })
        if charge > 0:
            anchor = row
    return out


def _total(rows, field):
    return sum((number(r[field]) for r in rows), ZERO)


def _params(context):
    p = context.get("parameters", {})
    cap, off, window = number(p.get("daily_cap", "6.00")), number(p.get("transfer_discount", "1.00")), number(p.get("transfer_minutes", "60"))
    if cap < 0 or off < 0 or window < 0:
        raise DomainError("BAD_DISCOUNT", "优惠参数不能为负")
    return cap, off, window


def _per_card(rides, cap, off, window):
    """分卡现行结果：每张卡各自一条链、各自一个日额度。"""
    cards = {}
    for row in rides:
        cards.setdefault(row["card"], []).append(row)
    result, card_totals = [], {}
    for card, items in cards.items():
        chain = settle_chain(items, cap, off, window)
        result.extend(chain)
        card_totals[card] = {
            "paid": money(_total(chain, "expected")),
            "transfer_discount": money(_total(chain, "transfer_discount")),
            "cap_discount": money(_total(chain, "cap_discount")),
            "cap_remaining": money(cap - _total(chain, "expected")),
        }
    return _sorted_rides(result), card_totals


def _detect_candidates(rides, window_gap=CANDIDATE_GAP_MINUTES):
    """启发式发现同人疑似换卡链：同一业务日内、时间相邻、间隔在窗口内的不同卡。"""
    by_day = {}
    for row in rides:
        by_day.setdefault(row["business_day"], []).append(row)
    found = []
    for day, items in by_day.items():
        ordered = _sorted_rides(items)
        for prev, nxt in zip(ordered, ordered[1:]):
            if prev["card"] == nxt["card"]:
                continue
            gap = Decimal(str((instant(nxt["at"]) - instant(prev["at"])).total_seconds())) / 60
            if Decimal(0) <= gap <= window_gap:
                found.append({"business_day": day, "old_card": prev["card"], "new_card": nxt["card"],
                              "gap_minutes": str(gap.quantize(Decimal("0.1")))})
    return found


def _diff_rows(before_rows, after_rows):
    """以 event_id 对齐两版逐行程结果，给出发生变化的字段（旧值→新值）。"""
    before = {r["event_id"]: r for r in before_rows}
    fields = ["transfer_anchor", "transfer_discount", "cap_discount", "expected", "day_paid"]
    changes = []
    for r in after_rows:
        old = before.get(r["event_id"])
        if old is None:
            changes.append({"event_id": r["event_id"], "card": r["card"], "changed": {f: {"from": None, "to": r[f]} for f in fields}})
            continue
        changed = {}
        for f in fields:
            if str(old.get(f)) != str(r.get(f)):
                changed[f] = {"from": old.get(f), "to": r.get(f)}
        if changed:
            changes.append({"event_id": r["event_id"], "card": r["card"], "changed": changed})
    return changes


def evaluate(rows, context):
    """M8 入口：对一批已配对行程（M4 形态）做分卡结算 + 候选换卡链探测。"""
    cap, off, window = _params(context)
    per_card, card_totals = _per_card(rows, cap, off, window)
    candidates = _detect_candidates(rows)
    metrics = {
        "rides": len(rows), "cards": len(card_totals),
        "per_card_paid": money(_total(per_card, "expected")),
        "candidate_links": len(candidates),
        "provisional_held": money(sum((cap for _ in candidates), ZERO)),
    }
    return {"rows": per_card, "quarantine": [], "metrics": metrics,
            "card_totals": card_totals, "candidates": candidates}

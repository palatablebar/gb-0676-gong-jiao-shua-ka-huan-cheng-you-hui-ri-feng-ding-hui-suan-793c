"""补证并账服务：换卡凭证到达后，把候选链转正为同人唯一权益结果。

纯函数部分负责确定性计算，store 部分负责持久化与幂等。
关键不变量：
1. 每条原始刷卡（event_id）始终只有一条现行权益结果，原刷卡记录与来源行不被改写，可追溯。
2. 同人一个业务日只有一个日封顶额度；并账后分卡额度账本释放，换成同人共享账本，两卡不重复占用。
3. 同一补证指令（command_id）重试返回同一结果，不产生第二次减免。
4. 每次补证形成一个新版本快照，差异明细列出两卡哪些行程、优惠、封顶余量发生变化。
"""
from decimal import Decimal
from app.values import number, money, instant
from app.errors import DomainError
from app.modules.m8_recon.rules import settle_chain, _per_card, _sorted_rides, _total, _diff_rows, ZERO


def build_case(rides, params):
    """从一批行程构造初始并账案卷：分卡结果 + 候选链（均为未并账）。"""
    cap, off, window = number(params.get("daily_cap", "6.00")), number(params.get("transfer_discount", "1.00")), number(params.get("transfer_minutes", "60"))
    per_card, card_totals = _per_card(rides, cap, off, window)
    from app.modules.m8_recon.rules import _detect_candidates
    candidates = _detect_candidates(rides)
    # 同业务日同卡对去重，生成稳定候选编号
    links, seen = [], set()
    for c in candidates:
        k = (c["business_day"], c["old_card"], c["new_card"])
        if k in seen:
            continue
        seen.add(k)
        links.append({"link_id": "L%03d" % (len(links) + 1), "status": "PROVISIONAL", **c})
    return {
        "params": {"daily_cap": money(cap), "transfer_discount": money(off), "transfer_minutes": str(window)},
        "rides": _sorted_rides(rides),
        "links": links,
        "versions": [],
        "commands": [],
    }


def _validate_claim(claim, case):
    day, old_card, new_card = claim.get("business_day"), claim.get("old_card"), claim.get("new_card")
    if not day or not old_card or not new_card:
        raise DomainError("BAD_CLAIM", "补证需要业务日、旧卡号、新卡号")
    day_rides = [r for r in case["rides"] if r["business_day"] == day]
    cards = {r["card"] for r in day_rides}
    if old_card not in cards:
        raise DomainError("OLD_CARD_UNKNOWN", "旧卡号在该业务日无刷卡记录")
    if new_card not in cards:
        raise DomainError("NEW_CARD_UNKNOWN", "新卡号在该业务日无刷卡记录")
    if old_card == new_card:
        raise DomainError("SAME_CARD", "换卡凭证的新旧卡不能相同")
    # 一张卡在同一业务日只能归属一条已确认链，防止一张卡被两笔凭证重复占用额度
    for link in case["links"]:
        if link["status"] != "CONFIRMED" or link["business_day"] != day:
            continue
        if new_card in (link["old_card"], link["new_card"]) or old_card in (link["old_card"], link["new_card"]):
            raise DomainError("CARD_ALREADY_MERGED", "该卡已存在确认并账链，不能重复占用额度")
    replaced_at = claim.get("replaced_at")
    if replaced_at is not None:
        instant(replaced_at)  # 仅校验时间格式，迟到凭证不要求当天到达


def apply_claim(case, command):
    """应用一条补证指令，返回新版本号（1 起）。command 含 command_id 与 claim。"""
    cid = command.get("command_id")
    if not cid:
        raise DomainError("BAD_COMMAND", "补证指令需要 command_id")
    prior = next((v for v in case["versions"] if v["command_id"] == cid), None)
    if prior is not None:
        return prior["version"], False  # 幂等：同一补证重试，复用既有版本，不重复减免
    claim = command.get("claim") or {}
    _validate_claim(claim, case)
    cap = number(case["params"]["daily_cap"])
    off = number(case["params"]["transfer_discount"])
    window = number(case["params"]["transfer_minutes"])
    day, old_card, new_card = claim["business_day"], claim["old_card"], claim["new_card"]

    before_rows, _ = _per_card(case["rides"], cap, off, window)
    before_day = [r for r in before_rows if r["business_day"] == day]
    before_paid = _total(before_day, "expected")

    # 并账：该业务日两卡视为同一持卡人，合成一条时间序卡链，共享一个日封顶额度
    day_rides = [r for r in case["rides"] if r["business_day"] == day and r["card"] in (old_card, new_card)]
    merged_chain = settle_chain(day_rides, cap, off, window)
    merged_paid = _total(merged_chain, "expected")
    refund = before_paid - merged_paid
    if refund < 0:
        raise DomainError("REFUND_NEGATIVE", "并账结果高于分卡结果，拒绝入账")

    # 候选链转正（存在匹配候选则关联，否则为凭证直接确认）
    matched = next((l for l in case["links"] if l["business_day"] == day and l["old_card"] == old_card
                    and l["new_card"] == new_card and l["status"] == "PROVISIONAL"), None)
    link_id = matched["link_id"] if matched else "L%03d" % (len(case["links"]) + 1)
    if matched:
        matched["status"] = "CONFIRMED"
    else:
        case["links"].append({"link_id": link_id, "status": "CONFIRMED", "business_day": day,
                              "old_card": old_card, "new_card": new_card, "gap_minutes": None})

    # 释放两笔分卡预占、登记一笔同人共享额度占用，保证两卡不重复占用同一笔额度
    ledger = {
        "released": [
            {"scope": "CARD_DAY", "key": f"{old_card}|{day}", "cap": money(cap)},
            {"scope": "CARD_DAY", "key": f"{new_card}|{day}", "cap": money(cap)},
        ],
        "held": [{"scope": "PERSON_DAY", "key": f"{old_card}+{new_card}|{day}", "cap": money(cap),
                  "used": money(merged_paid), "remaining": money(cap - merged_paid)}],
    }

    diff = _diff_rows(before_day, merged_chain)
    card_view = {}
    for card in (old_card, new_card):
        b = [r for r in before_day if r["card"] == card]
        a = [r for r in merged_chain if r["card"] == card]
        # 并账后日封顶额度为同人共享，单卡不再独立持有封顶余量（余量仅在额度账本按人日呈现）
        card_view[card] = {
            "before": {"paid": money(_total(b, "expected")), "transfer_discount": money(_total(b, "transfer_discount")),
                       "cap_discount": money(_total(b, "cap_discount")), "cap_remaining": money(cap - _total(b, "expected"))},
            "after": {"paid": money(_total(a, "expected")), "transfer_discount": money(_total(a, "transfer_discount")),
                      "cap_discount": money(_total(a, "cap_discount")), "cap_remaining": "共享额度"},
        }

    version = len(case["versions"]) + 1
    snapshot = {
        "version": version, "command_id": cid, "claim": claim, "link_id": link_id,
        "status": "CONFIRMED", "refund": money(refund),
        "before_paid": money(before_paid), "after_paid": money(merged_paid),
        "merged_rows": merged_chain, "changes": diff, "card_view": card_view, "quota_ledger": ledger,
    }
    case["versions"].append(snapshot)
    case["commands"].append({"command_id": cid, "claim": claim, "version": version})
    return version, True


def current_view(case):
    """生成案卷当前唯一同人权益结果（叠加所有已确认并账）。"""
    cap = number(case["params"]["daily_cap"])
    off = number(case["params"]["transfer_discount"])
    window = number(case["params"]["transfer_minutes"])
    confirmed = [v for v in case["versions"]]
    merged_groups = {(v["claim"]["business_day"], v["claim"]["old_card"], v["claim"]["new_card"]) for v in confirmed}

    rows, groups = [], []
    handled = set()  # 已并入同人链的（业务日, 卡），按粒度排除，避免同业务日第三张卡丢失
    for day, old_card, new_card in sorted(merged_groups):
        handled.update({(day, old_card), (day, new_card)})
        day_rides = [r for r in case["rides"] if r["business_day"] == day and r["card"] in (old_card, new_card)]
        chain = settle_chain(day_rides, cap, off, window)
        rows.extend(chain)
        groups.append({"business_day": day, "cards": [old_card, new_card], "merged": True,
                       "paid": money(_total(chain, "expected")),
                       "cap_remaining": money(cap - _total(chain, "expected"))})
    # 未并账的其它业务日/卡仍按分卡结果
    rest = [r for r in case["rides"] if (r["business_day"], r["card"]) not in handled]
    if rest:
        per_card, card_totals = _per_card(rest, cap, off, window)
        rows.extend(per_card)
    pending = [l for l in case["links"] if l["status"] == "PROVISIONAL"]
    return {
        "rows": _sorted_rides(rows), "groups": groups, "pending_candidates": pending,
        "confirmed_count": len(merged_groups),
        "total_refund": money(sum((number(v["refund"]) for v in confirmed), ZERO)),
    }

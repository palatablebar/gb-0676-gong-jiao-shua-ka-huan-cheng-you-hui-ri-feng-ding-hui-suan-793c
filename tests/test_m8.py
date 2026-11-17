"""M8 坏卡换卡同人权益并账测试。"""
from decimal import Decimal
import pytest
from app.values import number
from app.errors import DomainError
from app.modules.m8_recon.rules import evaluate, settle_chain
from app.modules.m8_recon import service
from app.modules.m8_recon.handler import _demo_csv, _rides_from_csv

DAY = "2026-11-16"


def ride(eid, card, route, hm, day=DAY):
    h, m = hm.split(":")
    return {"event_id": eid, "card": card, "route": route, "business_day": day,
            "at": f"2026-11-16T{h}:{m}:00+00:00", "base_fare": "2.50",
            "_sources": [int(eid[1:])], "_line": int(eid[1:])}


def demo_rides():
    return [
        ride("T101", "C-OLD", "01", "07:40"), ride("T102", "C-OLD", "02", "08:05"),
        ride("T103", "C-OLD", "03", "08:25"),
        ride("T201", "C-NEW", "01", "09:05"), ride("T202", "C-NEW", "02", "09:30"),
        ride("T203", "C-NEW", "03", "09:55"), ride("T204", "C-NEW", "01", "10:20"),
        ride("T205", "C-NEW", "02", "10:45"),
    ]


CLAIM = {"business_day": DAY, "old_card": "C-OLD", "new_card": "C-NEW"}


# ---------- 裁决：凭证未齐，先保留候选权益 ----------

def test_candidate_link_detected_and_held_not_merged():
    ev = evaluate(demo_rides(), {})
    assert ev["metrics"]["candidate_links"] == 1
    assert ev["candidates"][0]["old_card"] == "C-OLD"
    # 候选预占一份同人额度，但现行结果仍是分卡（两卡各自计到 6.00 与 5.50）
    assert ev["metrics"]["provisional_held"] == "6.00"
    assert ev["metrics"]["per_card_paid"] == "11.50"


def test_provisional_does_not_change_current_result():
    case = service.build_case(demo_rides(), {})
    view = service.current_view(case)
    assert view["confirmed_count"] == 0
    assert view["total_refund"] == "0.00"
    assert view["pending_candidates"][0]["status"] == "PROVISIONAL"
    # 新卡首程在分卡结果中没有换乘锚点（被当成两张卡）
    t201 = next(r for r in view["rows"] if r["event_id"] == "T201")
    assert t201["transfer_anchor"] == "—"


def test_far_apart_cards_not_candidate():
    rows = [ride("A1", "CA", "01", "07:00"), ride("B1", "CB", "02", "18:00")]
    assert evaluate(rows, {})["candidates"] == []


# ---------- 补证转正：唯一同人权益结果 ----------

def test_claim_merges_and_refunds():
    case = service.build_case(demo_rides(), {})
    version, created = service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    assert version == 1 and created is True
    snap = case["versions"][0]
    assert snap["before_paid"] == "11.50" and snap["after_paid"] == "6.00"
    assert snap["refund"] == "5.50"
    view = service.current_view(case)
    assert view["confirmed_count"] == 1
    assert sum((number(r["expected"]) for r in view["rows"]), Decimal(0)) == Decimal("6.00")


def test_transfer_chain_continues_across_cards():
    case = service.build_case(demo_rides(), {})
    service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    rows = {r["event_id"]: r for r in service.current_view(case)["rows"]}
    # 新卡首程接续旧卡末程作为换乘锚点
    assert rows["T201"]["transfer_anchor"] == "T103"
    assert rows["T201"]["transfer_discount"] == "1.00"


def test_single_shared_cap_not_double_occupied():
    case = service.build_case(demo_rides(), {})
    service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    ledger = case["versions"][0]["quota_ledger"]
    assert len(ledger["released"]) == 2  # 两笔分卡额度释放
    assert len(ledger["held"]) == 1 and ledger["held"][0]["scope"] == "PERSON_DAY"
    assert ledger["held"][0]["used"] == "6.00" and ledger["held"][0]["remaining"] == "0.00"


def test_diff_lists_changed_trips_discounts_and_cap_margin():
    case = service.build_case(demo_rides(), {})
    service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    snap = case["versions"][0]
    changed = {c["event_id"]: c["changed"] for c in snap["changes"]}
    # 旧卡三程不变，新卡五程全部重算
    assert "T101" not in changed and "T201" in changed
    assert "transfer_discount" in changed["T201"]
    cv = snap["card_view"]["C-NEW"]
    # 新卡分卡时独自触顶（实付 6.00、余量 0.00）；并账后纳入同人共享额度，实付降为 0.50
    assert cv["before"]["paid"] == "6.00" and cv["before"]["cap_remaining"] == "0.00"
    assert cv["after"]["paid"] == "0.50" and cv["after"]["cap_remaining"] == "共享额度"
    old = snap["card_view"]["C-OLD"]
    assert old["before"]["paid"] == old["after"]["paid"] == "5.50"


def test_money_conservation_after_merge():
    case = service.build_case(demo_rides(), {})
    service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    for r in service.current_view(case)["rows"]:
        assert number(r["base_fare"]) == number(r["expected"]) + number(r["transfer_discount"]) + number(r["cap_discount"])


def test_raw_tap_records_remain_traceable():
    rides = demo_rides()
    case = service.build_case(rides, {})
    service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    out = service.current_view(case)["rows"]
    src = {r["event_id"]: r["_sources"] for r in out}
    assert src["T101"] == [101] and src["T205"] == [205]
    # 原始案卷行程不被改写
    assert len(case["rides"]) == len(rides)


# ---------- 幂等：同一补证重试不得重复减免 ----------

def test_retry_same_command_is_idempotent():
    case = service.build_case(demo_rides(), {})
    cmd = {"command_id": "CMD-DUP", "claim": CLAIM}
    v1, c1 = service.apply_claim(case, cmd)
    v2, c2 = service.apply_claim(case, cmd)
    assert (v1, c1) == (1, True) and (v2, c2) == (1, False)
    assert len(case["versions"]) == 1
    assert service.current_view(case)["total_refund"] == "5.50"


# ---------- 防双占与凭证校验 ----------

def test_card_cannot_be_merged_twice():
    case = service.build_case(demo_rides(), {})
    service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    with pytest.raises(DomainError) as exc:
        service.apply_claim(case, {"command_id": "CMD-2",
                                   "claim": {"business_day": DAY, "old_card": "C-NEW", "new_card": "C-OLD"}})
    assert exc.value.code == "CARD_ALREADY_MERGED"


def test_unknown_card_rejected():
    case = service.build_case(demo_rides(), {})
    with pytest.raises(DomainError) as exc:
        service.apply_claim(case, {"command_id": "CMD-X",
                                   "claim": {"business_day": DAY, "old_card": "C-OLD", "new_card": "GHOST"}})
    assert exc.value.code == "NEW_CARD_UNKNOWN"


def test_late_evidence_accepted_next_day():
    # 凭证次日到达：replaced_at 在业务日之后仍可受理，结果只取决于业务事实
    case = service.build_case(demo_rides(), {})
    claim = {**CLAIM, "replaced_at": "2026-11-17T09:00:00+08:00"}
    v, created = service.apply_claim(case, {"command_id": "CMD-LATE", "claim": claim})
    assert created is True and v == 1


# ---------- 候选到期 ----------

def test_expire_releases_provisional_keeps_split_result():
    case = service.build_case(demo_rides(), {})
    link_id = case["links"][0]["link_id"]
    case["links"][0]["status"] = "EXPIRED"
    assert service.current_view(case)["pending_candidates"] == []
    assert service.current_view(case)["total_refund"] == "0.00"
    assert case["links"][0]["link_id"] == link_id


# ---------- 确定性 ----------

def test_order_invariant():
    rides = demo_rides()
    a = settle_chain(rides, Decimal("6.00"), Decimal("1.00"), Decimal("60"))
    b = settle_chain(list(reversed(rides)), Decimal("6.00"), Decimal("1.00"), Decimal("60"))
    assert a == b


def test_third_card_same_day_and_other_day_kept_split():
    rides = demo_rides() + [
        ride("T301", "C-THIRD", "01", "09:10"),
        ride("T401", "C-NEW", "02", "08:00", day="2026-11-17"),
    ]
    case = service.build_case(rides, {})
    service.apply_claim(case, {"command_id": "CMD-1", "claim": CLAIM})
    view = service.current_view(case)
    ids = {r["event_id"] for r in view["rows"]}
    # 同业务日第三张卡与次日行程都不丢，仍按分卡结果保留一条现行结果
    assert {"T301", "T401"} <= ids
    assert len(view["rows"]) == len(rides)
    t301 = next(r for r in view["rows"] if r["event_id"] == "T301")
    assert t301["transfer_anchor"] == "—"  # 未被并入同人链


# ---------- HTTP 全链路 ----------

def test_http_full_flow(client):
    created = client.post("/api/recon/cases", json={}).get_json()
    cid = created["case_id"]
    assert created["current"]["confirmed_count"] == 0
    assert created["links"][0]["status"] == "PROVISIONAL"

    body = {"command_id": "CMD-WEB-1", "claim": CLAIM}
    r1 = client.post(f"/api/recon/cases/{cid}/claim", json=body).get_json()
    r2 = client.post(f"/api/recon/cases/{cid}/claim", json=body).get_json()
    assert r1["applied"] is True and r2["applied"] is False
    assert r1["version"] == r2["version"] == 1

    detail = client.get(f"/api/recon/cases/{cid}").get_json()
    assert len(detail["versions"]) == 1
    assert detail["current"]["total_refund"] == "5.50"
    # 每条原始刷卡恰有一条现行结果
    assert len(detail["current"]["rows"]) == len(detail["raw_event_ids"])


def test_http_demo_ingest_uses_real_pipeline():
    rides, quarantine = _rides_from_csv(_demo_csv(), {})
    assert len(rides) == 8 and quarantine == []


def test_http_expire_endpoint(client):
    cid = client.post("/api/recon/cases", json={}).get_json()["case_id"]
    link = client.get(f"/api/recon/cases/{cid}").get_json()["links"][0]["link_id"]
    r = client.post(f"/api/recon/cases/{cid}/expire/{link}")
    assert r.status_code == 200
    assert r.get_json()["links"][0]["status"] == "EXPIRED"


def test_http_case_404(client):
    assert client.get("/api/recon/cases/NOPE").status_code == 404


def test_http_module_index(client):
    assert client.get("/api/recon").status_code == 200
    assert client.get("/recon").status_code == 200

import hashlib
import json
from flask import Blueprint, request, jsonify
from app.errors import DomainError
from app import store
from app.values import instant
from app.modules.m8_recon import service
from app.modules.m4_journeys.rules import evaluate as pair_journeys
from app.modules.m3_fares.rules import evaluate as attach_fares
from app.modules.m2_routes.rules import evaluate as map_routes
from app.modules.m1_taps.rules import evaluate as intake

bp = Blueprint("recon", __name__)

# 内置换卡演示批：乘客 P1 当天在网点坏卡换卡，旧卡 C-OLD 与新卡 C-NEW 的刷卡流水。
DEMO_TAPS = [
    ("T101", "C-OLD", "01", "2026-11-16T07:40:00+08:00"),
    ("T102", "C-OLD", "02", "2026-11-16T08:05:00+08:00"),
    ("T103", "C-OLD", "03", "2026-11-16T08:25:00+08:00"),
    ("T201", "C-NEW", "01", "2026-11-16T09:05:00+08:00"),
    ("T202", "C-NEW", "02", "2026-11-16T09:30:00+08:00"),
    ("T203", "C-NEW", "03", "2026-11-16T09:55:00+08:00"),
    ("T204", "C-NEW", "01", "2026-11-16T10:20:00+08:00"),
    ("T205", "C-NEW", "02", "2026-11-16T10:45:00+08:00"),
]


def _demo_csv():
    lines = ["event_id,card,route,event_time,result,reported,note"]
    for eid, card, route, at in DEMO_TAPS:
        lines.append(f'{eid},{card},{route},{at},success,2.50,换卡演示')
    return ("\n".join(lines)).encode()


def _rides_from_csv(raw, params):
    """复用 M1→M4 真实导入路径，产出带 business_day/at/base_fare 的行程行。"""
    context = {"parameters": params or {}, "quarantine": []}
    r1 = intake(raw, context)
    context["quarantine"].extend(r1["quarantine"])
    r2 = map_routes(r1["rows"], context)
    context["quarantine"].extend(r2["quarantine"])
    r3 = attach_fares(r2["rows"], context)
    context["quarantine"].extend(r3["quarantine"])
    r4 = pair_journeys(r3["rows"], context)
    if not r4["rows"]:
        raise DomainError("NO_JOURNEY", "没有可并账的有效行程")
    return r4["rows"], context["quarantine"]


def _case_id(raw, params):
    return "RC-" + hashlib.sha256(raw + json.dumps(params or {}, sort_keys=True).encode()).hexdigest()[:16]


@bp.route("/api/recon", methods=["GET"])
def index():
    return jsonify({"module": "recon", "cases": store.list_cases()})


@bp.route("/api/recon/cases", methods=["GET", "POST"])
def cases():
    if request.method == "GET":
        return jsonify({"cases": store.list_cases()})
    payload = request.get_json(silent=True) or {}
    params = payload.get("parameters", {}) or {}
    raw = payload.get("csv")
    raw = raw.encode() if isinstance(raw, str) else _demo_csv()
    rides, quarantine = _rides_from_csv(raw, params)
    case = service.build_case(rides, params)
    case["case_id"] = _case_id(raw, params)
    case["quarantine"] = quarantine
    saved = store.save_case(case["case_id"], case)
    return jsonify(_case_payload(saved))


@bp.route("/api/recon/cases/<case_id>", methods=["GET"])
def case_detail(case_id):
    return jsonify(_case_payload(store.get_case(case_id)))


@bp.route("/api/recon/cases/<case_id>/claim", methods=["POST"])
def claim(case_id):
    case = store.get_case(case_id)
    command = request.get_json(silent=True) or {}
    if "command_id" not in command or "claim" not in command:
        raise DomainError("BAD_COMMAND", "需要 command_id 与 claim")
    version, created = service.apply_claim(case, command)
    store.save_case(case_id, case)
    body = _case_payload(store.get_case(case_id))
    body["applied"] = created
    body["version"] = version
    return jsonify(body)


@bp.route("/api/recon/cases/<case_id>/expire/<link_id>", methods=["POST"])
def expire(case_id, link_id):
    """凭证始终未到：候选到期，释放预占额度，维持分账结果。"""
    case = store.get_case(case_id)
    link = next((l for l in case["links"] if l["link_id"] == link_id), None)
    if link is None:
        raise DomainError("LINK_NOT_FOUND", "候选链不存在", 404)
    if link["status"] != "PROVISIONAL":
        raise DomainError("LINK_NOT_PENDING", "候选链已裁决，不能放行")
    link["status"] = "EXPIRED"
    store.save_case(case_id, case)
    return jsonify(_case_payload(store.get_case(case_id)))


def _case_payload(case):
    view = service.current_view(case)
    return {
        "case_id": case["case_id"], "params": case["params"], "links": case["links"],
        "commands": case["commands"], "versions": case["versions"],
        "current": view, "quarantine": case.get("quarantine", []),
        "raw_event_ids": [r["event_id"] for r in case["rides"]],
    }

"""闸门核定服务：只读取计量流水线结果，只写核定账本，不发出任何闸门控制。"""
from app.config import DEFAULTS
from app.errors import DomainError
from app.normalize import number
from app.pipeline import run
from app import store as run_store
from .rules import parse_tickets, parse_ratings, verify_outflow
from . import store as gate_store


def _rating_params(params):
    raw = params.get("gate_ratings")
    if raw is None:
        raw = DEFAULTS["gate_ratings"]
    try:
        return parse_ratings(raw)
    except ValueError as exc:
        code = exc.args[0]
        raise DomainError("CURVE" if code.startswith("RATING") else code,
                          "闸门泄流曲线格式不合法：需为 {闸门:[[开度,泄量],...]} 且开度严格递增")


def verify(ticket_text=None, ticket_bytes=None, meter_bytes=None, params=None, run_id=None):
    """核定并幂等保存工作稿。

    计量依据二选一：已有批次 run_id（可回查），或随请求提供的测站 CSV（meter_bytes）。
    """
    params = {**DEFAULTS, **(params or {})}
    if run_id:
        meter_run = run_store.fetch(run_id)
    else:
        if meter_bytes is None:
            raise DomainError("MISSING", "请指定计量依据批次 run_id 或上传测站 CSV")
        meter_run = run(meter_bytes, params, stop=3, persist=False)
    ratings = _rating_params(params)

    if ticket_bytes is None and ticket_text is not None:
        ticket_bytes = ticket_text.encode("utf-8")
    if ticket_bytes is None:
        raise DomainError("MISSING", "请提供闸门操作票 CSV 文本或文件")
    parsed = parse_tickets(ticket_bytes)

    saturation = number(params.get("outflow_saturation", DEFAULTS["outflow_saturation"]))
    tolerance = number(params.get("gate_tolerance", DEFAULTS["gate_tolerance"]))
    days = verify_outflow(parsed["tickets"], ratings, meter_run["flows"], saturation, tolerance)
    if not days:
        raise DomainError("EMPTY", "操作票与计量段没有可核定的共同时段")

    source_run_id = meter_run.get("manifest", {}).get("run_id") or run_id
    keys = gate_store.upsert_working(days, source_run_id)
    return {
        "days": days,
        "keys": [{"station": s, "date": d} for s, d in keys],
        "quarantine": parsed["quarantine"],
        "ticket_input_count": parsed["input_count"],
        "meter_run_id": source_run_id,
        "rules": meter_run.get("manifest", {}).get("rules"),
        "params": {"outflow_saturation": str(saturation), "gate_tolerance": str(tolerance)},
    }

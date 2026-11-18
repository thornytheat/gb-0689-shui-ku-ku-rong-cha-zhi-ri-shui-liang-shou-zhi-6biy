from datetime import timedelta
from decimal import Decimal as D

import pytest

from app.errors import DomainError
from app.modules.gate.rules import (
    verify_outflow, parse_tickets, parse_ratings, rate_discharge, BASIS,
)
from app.normalize import instant

RATINGS = parse_ratings({
    "1号闸": [["0.0", "0.0"], ["1.0", "2.0"], ["2.0", "5.0"]],
    "2号闸": [["0.0", "0.0"], ["1.0", "1.0"]],
})
SAT = D("3.0")
TOL = D("0.10")


def meter(station, start, end, q_lo, q_hi, id_lo="m1", id_hi="m2"):
    return {"station": station, "start": instant(start), "end": instant(end),
            "left": {"id": id_lo, "outflow": D(q_lo)},
            "right": {"id": id_hi, "outflow": D(q_hi)}}


def ticket(tid, station, gate, start, end, opening):
    return {"ticket_id": tid, "station": station, "gate": gate,
            "start": instant(start), "end": instant(end),
            "opening_m": D(opening), "line": 1}


def seg_map(days):
    return {s["basis"]: s for d in days for s in d["segments"]}


def test_saturated_interval_adopts_ticket():
    # 计量段端点达到饱和阈值 → 采用操作票泄量，而非计量
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "3.0", "3.2")]
    tickets = [ticket("T1", "S", "1号闸", "2026-11-16T08:00:00+08:00",
                      "2026-11-16T09:00:00+08:00", "1.0")]
    days = verify_outflow(tickets, RATINGS, parts, SAT, TOL)
    seg = days[0]["segments"][0]
    assert seg["basis"] == "GATE_SAT"
    # 开度1.0 → 泄量2.0 m³/s × 3600s
    assert seg["adopted_m3"] == D("7200")
    assert days[0]["verified_m3"] == D("7200")


def test_agreement_adopts_meter_within_tolerance():
    # 票计 2.0 vs 计量 1.9，相对偏差 5% ≤ 10% → 采用计量
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "1.9", "1.9")]
    tickets = [ticket("T1", "S", "1号闸", "2026-11-16T08:00:00+08:00",
                      "2026-11-16T09:00:00+08:00", "1.0")]
    seg = seg_map(verify_outflow(tickets, RATINGS, parts, SAT, TOL))["AGREE"]
    assert seg["adopted_m3"] == D("1.9") * 3600


def test_dispute_is_unadjudicated():
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "1.0", "1.0")]
    tickets = [ticket("T1", "S", "1号闸", "2026-11-16T08:00:00+08:00",
                      "2026-11-16T09:00:00+08:00", "1.0")]
    days = verify_outflow(tickets, RATINGS, parts, SAT, TOL)
    seg = days[0]["segments"][0]
    assert seg["basis"] == "DISPUTE" and seg["adopted_m3"] is None
    assert days[0]["state"] == "待裁决" and len(days[0]["uncertain"]) == 1
    # 无法裁决时段保留两个候选量，便于回查
    assert seg["meter_vol_m3"] == D("3600") and seg["gate_vol_m3"] == D("7200")


def test_meter_only_uses_meter_even_without_ticket():
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "1.2", "1.2")]
    seg = seg_map(verify_outflow([], RATINGS, parts, SAT, TOL))["BASE_METER"]
    assert seg["adopted_m3"] == D("1.2") * 3600


def test_saturated_without_ticket_blocks():
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "3.5", "3.5")]
    days = verify_outflow([], RATINGS, parts, SAT, TOL)
    assert days[0]["segments"][0]["basis"] == "SAT_NO_TICKET"
    assert days[0]["state"] == "待裁决"


def test_meter_gap_filled_by_ticket():
    # 只有操作票、没有计量段 → GATE_GAP，按票计
    tickets = [ticket("T1", "S", "1号闸", "2026-11-16T08:00:00+08:00",
                      "2026-11-16T10:00:00+08:00", "1.0")]
    days = verify_outflow(tickets, RATINGS, [], SAT, TOL)
    seg = days[0]["segments"][0]
    assert seg["basis"] == "GATE_GAP" and seg["adopted_m3"] == D("2.0") * 7200


def test_opening_outside_curve_not_extrapolated():
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "1.0", "1.0")]
    tickets = [ticket("T1", "S", "1号闸", "2026-11-16T08:00:00+08:00",
                      "2026-11-16T09:00:00+08:00", "2.5")]
    assert rate_discharge("1号闸", D("2.5"), RATINGS) is None
    days = verify_outflow(tickets, RATINGS, parts, SAT, TOL)
    assert days[0]["segments"][0]["basis"] == "UNRATED"
    assert days[0]["state"] == "待裁决"


def test_overlapping_tickets_same_gate_conflict():
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T10:00:00+08:00", "1.0", "1.0")]
    tickets = [
        ticket("T1", "S", "1号闸", "2026-11-16T08:00:00+08:00", "2026-11-16T09:30:00+08:00", "1.0"),
        ticket("T2", "S", "1号闸", "2026-11-16T09:00:00+08:00", "2026-11-16T10:00:00+08:00", "1.0"),
    ]
    days = verify_outflow(tickets, RATINGS, parts, SAT, TOL)
    assert any(s["basis"] == "OVERLAP" for s in days[0]["segments"])


def test_two_gates_open_together_sum_discharge():
    # 两闸同开，泄量相加：2.0 + 1.0 = 3.0 m³/s
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "2.9", "3.1")]
    tickets = [
        ticket("T1", "S", "1号闸", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "1.0"),
        ticket("T2", "S", "2号闸", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "1.0"),
    ]
    days = verify_outflow(tickets, RATINGS, parts, SAT, TOL)
    seg = days[0]["segments"][0]
    assert seg["basis"] == "GATE_SAT" and seg["adopted_m3"] == D("3.0") * 3600


def test_cross_midnight_ticket_split_into_two_days():
    # 跨 +08:00 午夜的操作票按同一口径拆到两个落日
    tickets = [ticket("T1", "S", "1号闸", "2026-11-16T23:00:00+08:00",
                      "2026-11-17T01:00:00+08:00", "1.0")]
    days = verify_outflow(tickets, RATINGS, [], SAT, TOL)
    assert [d["date"] for d in days] == ["2026-11-16", "2026-11-17"]
    assert all(d["verified_m3"] == D("2.0") * 3600 for d in days)
    # 票号在两个落日的段上都可回查
    assert all(d["segments"][0]["ticket_ids"] == ["T1"] for d in days)


def test_meter_segment_split_at_midnight_keeps_total():
    # 计量段本身跨午夜，线性拆值后两日计量之和等于整段梯形体积
    parts = [meter("S", "2026-11-16T23:00:00+08:00", "2026-11-17T01:00:00+08:00", "1.0", "2.0")]
    days = verify_outflow([], RATINGS, parts, SAT, TOL)
    total = sum(d["meter_m3"] for d in days)
    # 整段平均 1.5 m³/s × 7200s
    assert total == D("1.5") * 7200


def test_partial_coverage_day_flagged():
    parts = [meter("S", "2026-11-16T08:00:00+08:00", "2026-11-16T09:00:00+08:00", "1.0", "1.0")]
    day = verify_outflow([], RATINGS, parts, SAT, TOL)[0]
    assert day["state"] == "部分覆盖" and day["coverage_hours"] == D("1")


def test_parse_ticket_quarantine():
    raw = (
        "ticket_id,station,gate,start,end,opening_m\n"
        "T1,S,1号闸,2026-11-16T08:00:00+08:00,2026-11-16T09:00:00+08:00,1.0\n"
        "T2,S,1号闸,2026-11-16T10:00:00+08:00,2026-11-16T09:00:00+08:00,1.0\n"
        "T3,S,1号闸,2026-11-16T10:00:00+08:00,2026-11-16T11:00:00+08:00,-1\n"
    ).encode()
    parsed = parse_tickets(raw)
    assert len(parsed["tickets"]) == 1
    assert {q["code"] for q in parsed["quarantine"]} == {"RANGE", "NEGATIVE"}


def test_rating_curve_validation():
    with pytest.raises(ValueError):
        parse_ratings({"g": [["1.0", "1"], ["1.0", "2"]]})  # 开度未递增
    with pytest.raises(ValueError):
        parse_ratings({"g": [["1.0"]]})  # 结构非法


def test_basis_codes_have_chinese_legend():
    # 所有依据码都必须能在页面回查到中文说明
    for code in ("GATE_SAT", "AGREE", "BASE_METER", "GATE_GAP", "DISPUTE",
                 "UNRATED", "OVERLAP", "SAT_NO_TICKET"):
        assert BASIS[code]

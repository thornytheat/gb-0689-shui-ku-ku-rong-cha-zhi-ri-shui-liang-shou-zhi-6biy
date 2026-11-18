"""闸门启闭期出库量核定（纯函数，不接触数据库与远程控制）。

口径：
- 流量计按相邻观测梯形积分；任一端点出流达到饱和阈值，则该积分段判为饱和，计量不可信。
- 操作票按孔口开度查闸门泄流曲线（分段线性、不外推）求泄量，多闸同时开启时泄量相加。
- 时间轴以操作票端点与计量积分段端点合并，跨 +08:00 午夜先线性拆分再落日。
- 逐段裁决依据：饱和用票、票计一致用计、无票用计、缺口用票、分歧/越界/冲突无法裁决。
"""
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from app.normalize import rows_from, number, instant
from app.reasons import rejected


def _dt(value):
    """兼容内存流水线的 datetime 与已存储批次的 ISO 字符串。"""
    return value if isinstance(value, datetime) else instant(value)


def _dec(value):
    return value if isinstance(value, D) else D(str(value))

ZONE = timezone(timedelta(hours=8))

# 裁决依据码（可回查的采用依据）
BASIS = {
    "GATE_SAT": "饱和改按操作票",
    "AGREE": "票计一致",
    "BASE_METER": "无票按流量计",
    "GATE_GAP": "计量缺口按操作票",
    "DISPUTE": "票计分歧无法裁决",
    "UNRATED": "开度越出泄流曲线",
    "OVERLAP": "同闸操作票时段冲突",
    "SAT_NO_TICKET": "饱和且无操作票",
}
RESOLVED = {"GATE_SAT", "AGREE", "BASE_METER", "GATE_GAP"}


def _parse_opening(value):
    try:
        opening = number(value)
    except Exception:
        raise ValueError("OPENING")
    if opening < 0:
        raise ValueError("NEGATIVE")
    return opening


def parse_tickets(raw):
    """解析操作票 CSV，返回有效票与隔离行。"""
    result = rows_from(raw, ["ticket_id", "station", "gate", "start", "end", "opening_m"])
    good, quarantine = [], result["quarantine"]
    for row in result["raw"]:
        if not row["ticket_id"] or not row["station"] or not row["gate"]:
            quarantine.append(rejected(row, "MISSING", "票号/测站/闸门缺失"))
            continue
        try:
            start, end = instant(row["start"]), instant(row["end"])
            opening = _parse_opening(row["opening_m"])
        except ValueError as exc:
            code = exc.args[0]
            quarantine.append(rejected(row, code if code in ("NEGATIVE",) else "NUMBER", "开度格式错误或为负"))
            continue
        except Exception:
            quarantine.append(rejected(row, "TIME", "启闭时间缺少偏移或格式无效"))
            continue
        if end <= start:
            quarantine.append(rejected(row, "RANGE", "关闭时刻必须晚于开启时刻"))
            continue
        good.append({"ticket_id": row["ticket_id"], "station": row["station"], "gate": row["gate"],
                     "start": start, "end": end, "opening_m": opening, "line": row.get("_line", "")})
    return {"tickets": good, "quarantine": quarantine,
            "input_count": result["input_count"], "blank_count": result["blank_count"]}


def parse_ratings(raw):
    """泄流曲线：{gate: [(opening_m, discharge_m3s), ...]}，开度严格递增、泄量非负。"""
    if not isinstance(raw, dict):
        raise ValueError("RATING_SHAPE")
    ratings = {}
    for gate, points in raw.items():
        if not isinstance(points, list) or len(points) < 2:
            raise ValueError("RATING_SHAPE")
        curve = []
        for point in points:
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                raise ValueError("RATING_SHAPE")
            curve.append((number(point[0]), number(point[1])))
        if any(curve[i + 1][0] <= curve[i][0] for i in range(len(curve) - 1)):
            raise ValueError("RATING_MONO")
        if any(discharge < 0 for _, discharge in curve):
            raise ValueError("NEGATIVE")
        ratings[str(gate)] = curve
    return ratings


def rate_discharge(gate, opening, ratings):
    """按闸门泄流曲线分段线性插值，范围外不外推，返回 None 表示无法查定。"""
    curve = ratings.get(gate)
    if not curve:
        return None
    if opening < curve[0][0] or opening > curve[-1][0]:
        return None
    for (a_open, a_q), (b_open, b_q) in zip(curve, curve[1:]):
        if a_open <= opening <= b_open:
            if b_open == a_open:
                return a_q
            return a_q + (b_q - a_q) * (opening - a_open) / (b_open - a_open)
    return None


def split_interval(start, end, endpoints):
    """用给定端点切分区间，返回子段列表。"""
    cuts = sorted({t for t in endpoints if start < t < end})
    pieces, cursor = [], start
    for cut in cuts:
        pieces.append((cursor, cut))
        cursor = cut
    pieces.append((cursor, end))
    return pieces


def verify_outflow(tickets, ratings, meter_parts, saturation, rel_tolerance):
    """核定出库。meter_parts 复用 M3 积分段结构（含 left/right 观测与 start/end）。"""
    sat = number(saturation)
    tol = number(rel_tolerance)
    stations = sorted({t["station"] for t in tickets} |
                      {p["station"] for p in meter_parts})
    days = []
    for station in stations:
        days.extend(_verify_station(station, [t for t in tickets if t["station"] == station],
                                    ratings, [p for p in meter_parts if p["station"] == station],
                                    sat, tol))
    days.sort(key=lambda d: (d["station"], d["date"]))
    return days


def _meter_outflow_at(part, t):
    start, end = part["start"], part["end"]
    ratio = D(str((t - start).total_seconds())) / D(str((end - start).total_seconds()))
    return part["left"]["outflow"] + (part["right"]["outflow"] - part["left"]["outflow"]) * ratio


def _verify_station(station, tickets, ratings, meter_parts, sat, tol):
    # 同闸重叠票登记（重叠区间内该闸无法唯一查定）
    overlap_gates = set()
    for i, a in enumerate(tickets):
        for b in tickets[i + 1:]:
            if a["gate"] == b["gate"] and a["start"] < b["end"] and b["start"] < a["end"]:
                overlap_gates.add(a["gate"])

    # 1) 计量积分段先按午夜拆分，并记录饱和
    meter_subs = []
    for part in meter_parts:
        part = {**part, "start": _dt(part["start"]), "end": _dt(part["end"]),
                "left": {**part["left"], "outflow": _dec(part["left"]["outflow"])},
                "right": {**part["right"], "outflow": _dec(part["right"]["outflow"])}}
        for lo, hi in split_interval(part["start"], part["end"], _midnights_between(part["start"], part["end"])):
            q_lo, q_hi = _meter_outflow_at(part, lo), _meter_outflow_at(part, hi)
            meter_subs.append({"start": lo, "end": hi, "q_lo": q_lo, "q_hi": q_hi,
                               "saturated": q_lo >= sat or q_hi >= sat,
                               "source_ids": [part["left"].get("id", ""), part["right"].get("id", "")]})

    # 2) 操作票按午夜拆分并逐闸查定泄量
    ticket_subs = []
    for ticket in tickets:
        q = rate_discharge(ticket["gate"], ticket["opening_m"], ratings)
        for lo, hi in split_interval(ticket["start"], ticket["end"],
                                     _midnights_between(ticket["start"], ticket["end"])):
            ticket_subs.append({"start": lo, "end": hi, "gate": ticket["gate"],
                                "opening_m": ticket["opening_m"], "q": q,
                                "ticket_id": ticket["ticket_id"], "line": ticket["line"],
                                "overlap": ticket["gate"] in overlap_gates})

    # 3) 合并时间轴：仅在“计量或操作票覆盖到的并集”内逐段裁决
    boundaries = sorted({s["start"] for s in meter_subs} | {s["end"] for s in meter_subs} |
                        {s["start"] for s in ticket_subs} | {s["end"] for s in ticket_subs})
    segments = []
    for lo, hi in zip(boundaries, boundaries[1:]):
        m_hits = [m for m in meter_subs if m["start"] <= lo and hi <= m["end"]]
        t_hits = [t for t in ticket_subs if t["start"] <= lo and hi <= t["end"]]
        if not m_hits and not t_hits:
            continue
        seconds = D(str((hi - lo).total_seconds()))
        meter = m_hits[0] if m_hits else None
        meter_q_lo = meter["q_lo"] if meter else None
        meter_q_hi = meter["q_hi"] if meter else None
        meter_vol = ((meter_q_lo + meter_q_hi) / 2 * seconds) if meter else None
        saturated = bool(meter and meter["saturated"])

        gate_q, unrated, overlap = D(0), False, False
        evidence_tickets = []
        for t in t_hits:
            evidence_tickets.append(t["ticket_id"])
            if t["overlap"]:
                overlap = True
            if t["q"] is None:
                unrated = True
            else:
                gate_q += t["q"]
        gate_vol = gate_q * seconds if t_hits else None

        basis, detail, adopted = _adjudicate(meter, saturated, meter_vol, bool(t_hits),
                                             gate_vol, unrated, overlap, tol)
        if basis in RESOLVED:
            adopted_vol = adopted
        else:
            adopted_vol = None
        segments.append({
            "station": station, "start": lo, "end": hi,
            "date": lo.astimezone(ZONE).date().isoformat(),
            "seconds": seconds,
            "meter_vol_m3": meter_vol, "gate_vol_m3": gate_vol,
            "saturated": saturated, "basis": basis, "basis_detail": BASIS[basis],
            "adopted_m3": adopted_vol, "detail": detail,
            "ticket_ids": sorted(set(evidence_tickets)),
            "meter_ids": sorted(x for x in (meter["source_ids"] if meter else []) if x),
        })

    return _aggregate_days(station, segments)


def _adjudicate(meter, saturated, meter_vol, has_ticket, gate_vol, unrated, overlap, tol):
    """返回 (依据码, 说明, 采用体积)。无法裁决时采用体积为 None。"""
    if overlap:
        return "OVERLAP", "同一闸门存在时段重叠的操作票，泄量无法唯一查定", None
    if unrated:
        return "UNRATED", "开度超出该闸门泄流曲线范围且禁止外推", None
    if meter and has_ticket:
        if saturated:
            return "GATE_SAT", f"流量计饱和(≥阈值)，采用操作票泄量 {gate_vol:.3f} m³", gate_vol
        diff = abs(meter_vol - gate_vol)
        denom = max(abs(meter_vol), abs(gate_vol), D("0.000001"))
        if diff / denom <= tol:
            return "AGREE", f"票计偏差 {diff:.3f} m³ 在容差内，采用流量计计量 {meter_vol:.3f} m³", meter_vol
        return "DISPUTE", f"票计偏差 {diff:.3f} m³ 超过相对容差，人工裁决", None
    if meter and not has_ticket:
        if saturated:
            return "SAT_NO_TICKET", "流量计饱和且该时段无操作票可替代", None
        return "BASE_METER", f"无操作票，采用流量计计量 {meter_vol:.3f} m³", meter_vol
    if has_ticket and not meter:
        return "GATE_GAP", f"计量缺口，采用操作票泄量 {gate_vol:.3f} m³", gate_vol
    return "DISPUTE", "缺少任何可采用依据", None


def _midnights_between(start, end):
    cursor = start.astimezone(ZONE)
    out = []
    day = cursor.date()
    while True:
        midnight = datetime.combine(day + timedelta(days=1), datetime.min.time(), ZONE).astimezone(timezone.utc)
        if midnight >= end:
            break
        if midnight > start:
            out.append(midnight)
        day += timedelta(days=1)
    return out


def _aggregate_days(station, segments):
    bucket = {}
    for seg in segments:
        day = bucket.setdefault(seg["date"], {
            "station": station, "date": seg["date"], "segments": [],
            "verified_m3": D(0), "meter_m3": D(0), "gate_m3": D(0),
            "unadjudicated_m3": D(0), "coverage_hours": D(0),
            "basis_count": {}, "uncertain": []})
        day["segments"].append(seg)
        day["coverage_hours"] += seg["seconds"] / 3600
        if seg["meter_vol_m3"] is not None:
            day["meter_m3"] += seg["meter_vol_m3"]
        if seg["gate_vol_m3"] is not None:
            day["gate_m3"] += seg["gate_vol_m3"]
        day["basis_count"][seg["basis"]] = day["basis_count"].get(seg["basis"], 0) + 1
        if seg["adopted_m3"] is not None:
            day["verified_m3"] += seg["adopted_m3"]
        else:
            day["uncertain"].append({"start": seg["start"], "end": seg["end"],
                                     "basis": seg["basis"], "detail": seg["detail"],
                                     "ticket_ids": seg["ticket_ids"], "meter_ids": seg["meter_ids"]})
            vol = seg["meter_vol_m3"] if seg["meter_vol_m3"] is not None else seg["gate_vol_m3"]
            if vol is not None:
                day["unadjudicated_m3"] += vol
    result = []
    for day in bucket.values():
        day["state"] = ("待裁决" if day["uncertain"]
                        else ("完整" if day["coverage_hours"] == 24 else "部分覆盖"))
        result.append(day)
    result.sort(key=lambda d: d["date"])
    return result

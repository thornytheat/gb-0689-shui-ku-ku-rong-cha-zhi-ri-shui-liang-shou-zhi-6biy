import json
from decimal import Decimal as D

import pytest

from app.errors import DomainError
from app.modules.gate import store as gate_store
from app.modules.gate import service
from app.config import SAMPLES


def _ticket_csv():
    return (
        "ticket_id,station,gate,start,end,opening_m\n"
        "T1,青岚库,1号闸,2026-11-16T08:00:00+08:00,2026-11-16T09:00:00+08:00,1.0\n"
    ).encode()


def test_verify_persists_working_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    meter = (SAMPLES / "dirty.csv").read_bytes()
    r1 = service.verify(ticket_bytes=_ticket_csv(), meter_bytes=meter, params={})
    key = ("青岚库", "2026-11-16")
    assert key in [tuple(k.values()) for k in r1["keys"]]

    first = gate_store.fetch_day(*key)
    # 重跑同一天：整体替换工作稿，不新增行、不重复扣减
    r2 = service.verify(ticket_bytes=_ticket_csv(), meter_bytes=meter, params={})
    second = gate_store.fetch_day(*key)
    assert len(gate_store.list_ledger()) == len({d["date"] for d in r2["days"]})
    assert second["working"]["verified_m3"] == first["working"]["verified_m3"]
    assert second["published_version"] == 0 and second["published"] is None


def test_publish_then_failed_rerun_keeps_published_version(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    meter = (SAMPLES / "dirty.csv").read_bytes()
    service.verify(ticket_bytes=_ticket_csv(), meter_bytes=meter, params={})
    key = ("青岚库", "2026-11-16")
    published = gate_store.publish(*key, version=0)
    assert published["published_version"] == 1
    assert published["published"]["verified_m3"] == published["working"]["verified_m3"]

    # 重跑引入无法裁决时段（开度超出泄流曲线）→ 核定失败，旧版发布账原样保留
    bad = (
        "ticket_id,station,gate,start,end,opening_m\n"
        "T9,青岚库,1号闸,2026-11-16T08:00:00+08:00,2026-11-16T09:00:00+08:00,9.0\n"
    ).encode()
    service.verify(ticket_bytes=bad, meter_bytes=meter, params={})
    after = gate_store.fetch_day(*key)
    assert after["working"]["state"] == "待裁决"
    assert after["published_version"] == 1  # 旧版保留
    with pytest.raises(DomainError) as exc:
        gate_store.publish(*key, version=1)
    assert exc.value.code == "REVIEW"
    # 发布失败也留痕，但版本不前进
    assert gate_store.fetch_day(*key)["published_version"] == 1


def test_resume_from_confirmed_periods_keeps_published_sum(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    meter = (SAMPLES / "dirty.csv").read_bytes()
    service.verify(ticket_bytes=_ticket_csv(), meter_bytes=meter, params={})
    key = ("青岚库", "2026-11-16")
    before = gate_store.publish(*key, version=0)
    confirmed_total = before["published"]["verified_m3"]

    # 从已确认时段续算：新票与已确认时段不重叠，重跑后已发布总量不被重复累计
    extra = (
        "ticket_id,station,gate,start,end,opening_m\n"
        "T1,青岚库,1号闸,2026-11-16T08:00:00+08:00,2026-11-16T09:00:00+08:00,1.0\n"
        "T2,青岚库,2号闸,2026-11-16T09:00:00+08:00,2026-11-16T10:00:00+08:00,0.5\n"
    ).encode()
    service.verify(ticket_bytes=extra, meter_bytes=meter, params={})
    ledger = gate_store.fetch_day(*key)
    assert ledger["published"]["verified_m3"] == confirmed_total  # 发布版未被改动
    assert ledger["working"]["basis_count"].get("GATE_GAP", 0) >= 0


def test_optimistic_lock_conflict_returns_409(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    meter = (SAMPLES / "dirty.csv").read_bytes()
    service.verify(ticket_bytes=_ticket_csv(), meter_bytes=meter, params={})
    gate_store.publish("青岚库", "2026-11-16", 0)
    with pytest.raises(DomainError) as exc:
        gate_store.publish("青岚库", "2026-11-16", 0)  # 陈旧版本
    assert exc.value.status == 409 and exc.value.code == "STALE"


def test_audit_trail_records_every_action(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    meter = (SAMPLES / "dirty.csv").read_bytes()
    service.verify(ticket_bytes=_ticket_csv(), meter_bytes=meter, params={})
    gate_store.publish("青岚库", "2026-11-16", 0)
    actions = [row["action"] for row in gate_store.audit("青岚库", "2026-11-16")]
    assert "核定" in actions and "发布" in actions


def test_verify_requires_ticket(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    with pytest.raises(DomainError) as exc:
        service.verify(meter_bytes=(SAMPLES / "dirty.csv").read_bytes(), params={})
    assert exc.value.code == "MISSING"


def test_http_verify_publish_flow(client):
    # 先建立计量批次（M1-M3 流水线），核定再按 run_id 回查计量依据
    meter = client.post("/api/runs", json={
        "text": (SAMPLES / "dirty.csv").read_text(encoding="utf-8-sig"),
        "params": {}}).get_json()
    payload = {"ticket_text": _ticket_csv().decode(),
               "run_id": meter["manifest"]["run_id"], "params": {}}
    res = client.post("/api/gate/verify", json=payload)
    assert res.status_code == 200
    body = res.get_json()
    assert body["keys"] and body["meter_run_id"] == meter["manifest"]["run_id"]

    listing = client.get("/api/gate/ledger").get_json()
    assert any(row["date"] == "2026-11-16" for row in listing)

    detail = client.get("/api/gate/ledger/青岚库/2026-11-16").get_json()
    pub = client.post("/api/gate/publish/青岚库/2026-11-16",
                      json={"version": detail["published_version"]})
    assert pub.status_code == 200 and pub.get_json()["published_version"] == 1

    audit_rows = client.get("/api/gate/audit?station=青岚库&date=2026-11-16").get_json()
    assert any(r["action"] == "发布" for r in audit_rows)


def test_no_remote_control_endpoints_exist(client):
    # 闸门远程控制属于明确禁区：任何控制类路径都不得存在
    for method in ("post", "put", "patch", "delete"):
        for path in ("/api/gate/control/青岚库", "/api/gate/open", "/api/gate/close"):
            resp = getattr(client, method)(path, json={})
            assert resp.status_code == 404

"""闸门核定出库账 HTTP 接口（只读+账本维护；无任何远程控制端点）。"""
import json

from flask import Blueprint, jsonify, request

from app.errors import DomainError
from app.normalize import serial
from . import service, store

bp = Blueprint("gate", __name__)


@bp.route("/api/gate/verify", methods=["POST"])
def verify():
    if request.files:
        try:
            params = json.loads(request.form.get("params", "{}"))
        except ValueError:
            raise DomainError("PARAMS", "参数格式错误")
        run_id = params.pop("run_id", None) if isinstance(params, dict) else None
        ticket_file = request.files.get("ticket")
        if ticket_file is None:
            raise DomainError("FILE", "请选择闸门操作票文件")
        meter_file = request.files.get("file")
        result = service.verify(ticket_bytes=ticket_file.read(),
                                meter_bytes=meter_file.read() if meter_file else None,
                                params=params, run_id=run_id)
    else:
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            raise DomainError("BODY", "请求必须是对象")
        params = payload.get("params")
        if not isinstance(params, dict):
            raise DomainError("PARAMS", "参数必须是对象")
        result = service.verify(ticket_text=payload.get("ticket_text"),
                                params=params, run_id=payload.get("run_id"))
    return jsonify(serial(result))


@bp.route("/api/gate/ledger", methods=["GET"])
def ledger():
    return jsonify(serial(store.list_ledger()))


@bp.route("/api/gate/ledger/<station>/<date>", methods=["GET"])
def day_detail(station, date):
    return jsonify(serial(store.fetch_day(station, date)))


@bp.route("/api/gate/publish/<station>/<date>", methods=["POST"])
def publish(station, date):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise DomainError("BODY", "请求必须是对象")
    return jsonify(serial(store.publish(station, date, payload.get("version"))))


@bp.route("/api/gate/audit", methods=["GET"])
def audit():
    return jsonify(serial(store.audit(request.args.get("station"),
                                      request.args.get("date"))))

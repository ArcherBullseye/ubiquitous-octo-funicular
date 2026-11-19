"""Home workspace page and API."""
import time

from flask import Blueprint, jsonify, render_template, request

from .schema import number, public_document
from .metrics import SOURCES, DEVICE_METRICS, dashboard_readings
from ..db import get_settings
from .store import ConflictError, load_document, recent_events, save_document, set_manual_hold


def create_home_blueprint(service, state, discovery=None):
    bp = Blueprint('home', __name__)

    @bp.get('/home')
    def index():
        return render_template('home.html')

    @bp.get('/api/home')
    def get_home():
        return jsonify(document=public_document(load_document()), devices=service.snapshot(),
                       events=recent_events(), condition_sources=SOURCES, device_metrics=DEVICE_METRICS,
                       dashboard=dashboard_readings(state.snapshot(), get_settings()))

    @bp.post('/api/home')
    def save_home():
        try:
            doc = save_document(request.get_json())
        except ConflictError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        state.home_refresh.set()
        return jsonify(document=public_document(doc))

    @bp.post('/api/home/devices/<device_id>/power')
    def power(device_id):
        data = request.get_json() or {}
        if not isinstance(data, dict) or not isinstance(data.get('on'), bool):
            return jsonify(error='Choose ON or OFF.'), 400
        try:
            hold = number(data.get('hold_minutes', 15), 'Manual hold minutes', 0, 1440)
            service.command(device_id, data['on'], hold_minutes=hold)
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(queued=True), 202

    @bp.post('/api/home/devices/<device_id>/resume')
    def resume(device_id):
        with service.lock:
            if device_id not in service.readings:
                return jsonify(error='Device not found.'), 404
            set_manual_hold(device_id, 0)
            service.readings[device_id]['hold_until'] = 0
            service.publish()
        return jsonify(ok=True)

    @bp.get('/api/home/preview/<rule_id>')
    def preview_rule(rule_id):
        from .engine import matching
        doc = load_document()
        rule = next((r for r in doc['rules'] if r['id'] == rule_id), None)
        if not rule:
            return jsonify(error='Save this automation first.'), 404
        dashboard = dashboard_readings(state.snapshot(), get_settings())
        result = matching(rule, service.snapshot(), dashboard['solar'],
                          doc.get('timezone', 'America/Detroit'), time.time(), dashboard)
        return jsonify(result=result)

    @bp.get('/api/home/discovery')
    def discovery_status():
        return jsonify(discovery.snapshot())

    @bp.post('/api/home/discovery/<operation>')
    def discovery_action(operation):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify(error='Expected a discovery request object.'), 400
        try:
            if operation == 'scan':
                discovery.scan(data.get('network', ''))
            elif operation == 'signin':
                discovery.signin(data.get('user_code', ''), data.get('remember', False))
            elif operation == 'confirm':
                discovery.confirm()
            elif operation == 'sync':
                discovery.sync()
            elif operation == 'disconnect':
                discovery.disconnect()
            elif operation == 'import':
                doc, count, disabled = discovery.import_devices(data)
                return jsonify(document=public_document(doc), imported=count, disabled=disabled)
            else:
                return jsonify(error='Discovery operation not found.'), 404
        except ConflictError as exc:
            return jsonify(error=str(exc)), 409
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(discovery.snapshot()), 202

    return bp

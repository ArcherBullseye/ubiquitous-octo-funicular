"""Home workspace page and API."""
import time

from flask import Blueprint, jsonify, render_template, request

from .schema import number, public_document
from .store import ConflictError, load_document, recent_events, save_document, set_manual_hold


def create_home_blueprint(service, state):
    bp = Blueprint('home', __name__)

    @bp.get('/home')
    def index():
        return render_template('home.html')

    @bp.get('/api/home')
    def get_home():
        return jsonify(document=public_document(load_document()), devices=service.snapshot(),
                       events=recent_events())

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
        readings = state.get('readings') or {}
        solar = {'status': 'connected', **readings,
                 'updated_at': {key: state.get('last_updated') for key in readings}}
        result = matching(rule, service.snapshot(), solar, doc.get('timezone', 'America/Detroit'), time.time())
        return jsonify(result=result)

    return bp

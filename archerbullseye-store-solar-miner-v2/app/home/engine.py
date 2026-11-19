"""Evaluate saved rules against fresh readings and a local clock."""
import json
import time
from datetime import datetime

from zoneinfo import ZoneInfo

from .metrics import numeric


def fresh_value(snapshot, metric, now):
    stamp = (snapshot.get('updated_at') or {}).get(metric)
    try:
        age = now - datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError, OverflowError):
        return None
    if snapshot.get('status') != 'connected' or not 0 <= age <= snapshot.get('max_age_seconds', 1800):
        return None
    return snapshot.get(metric)


def evaluate_condition(condition, devices, solar, timezone, now, dashboard=None):
    metric = condition['metric']
    if condition['source'] == 'clock':
        clock = datetime.fromtimestamp(now, ZoneInfo(timezone)).strftime('%H:%M')
        start, end = condition['value'], condition['end']
        return start <= clock < end if start < end else clock >= start or clock < end
    if condition['source'] == 'solar':
        value = fresh_value(solar, metric, now)
    elif condition['source'] in (dashboard or {}):
        value = fresh_value(dashboard[condition['source']], metric, now)
    else:
        value = fresh_value(devices.get(condition['source'], {}), metric, now)
    if value is None:
        return None
    operator = condition['operator']
    if operator in ('is_on', 'is_off') and type(value) is not bool:
        return None
    if operator == 'is_on':
        return value is True
    if operator == 'is_off':
        return value is False
    if not numeric(value):
        return None
    threshold = condition['value']
    if operator == 'above': return value > threshold
    if operator == 'below': return value < threshold
    if operator == 'at_least': return value >= threshold
    if operator == 'at_most': return value <= threshold
    if operator == 'equals': return value == threshold
    if operator == 'between': return threshold <= value <= condition['end']
    if operator == 'outside': return value < threshold or value > condition['end']
    return None


def matching(rule, devices, solar, timezone, now, dashboard=None):
    results = [evaluate_condition(c, devices, solar, timezone, now, dashboard) for c in rule['conditions']]
    # Missing required readings pause a rule, including rules matching "any".
    if None in results:
        return None
    return all(results) if rule['match'] == 'all' else any(results)


class RuleEngine:
    def __init__(self):
        self.baselines = {}

    def tick(self, document, devices, solar, timezone, run_action, log, now=None, dashboard=None):
        now = time.time() if now is None else now
        active = set()
        for rule in document['rules']:
            if not rule['enabled']:
                continue
            active.add(rule['id'])
            signature = json.dumps(rule, sort_keys=True)
            memory = self.baselines.get(rule['id'])
            result = matching(rule, devices, solar, timezone, now, dashboard)
            if memory is None or memory['signature'] != signature or result is None:
                self.baselines[rule['id']] = {'signature': signature, 'previous': result,
                    'since': None, 'fired': bool(result), 'last_fired': float('-inf')}
                continue
            if result is False:
                memory.update(previous=False, since=None, fired=False)
                continue
            if memory['previous'] is None:
                memory.update(previous=True, fired=True)
                continue
            if memory['previous'] is False:
                memory.update(previous=True, since=now)
            if memory['fired'] or memory['since'] is None or now - memory['since'] < rule['for_seconds']:
                continue
            memory['fired'] = True
            if now - memory['last_fired'] < rule['cooldown_seconds']:
                log('skipped', rule['name'], 'Crossed during cooldown. Waiting for the conditions to reset.')
                continue
            memory['last_fired'] = now
            log('trigger', rule['name'], 'Conditions matched. Running actions in order.')
            for action in rule['actions']:
                try:
                    run_action(action, rule['name'])
                except Exception as exc:
                    # Only sanitized service errors are exposed by the action adapter.
                    log('error', rule['name'], str(exc))
        self.baselines = {key: value for key, value in self.baselines.items() if key in active}

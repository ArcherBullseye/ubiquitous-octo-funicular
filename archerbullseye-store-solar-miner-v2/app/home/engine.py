"""Evaluate saved rules against fresh readings and a local clock."""
import json
import time
from datetime import datetime

from zoneinfo import ZoneInfo

from .metrics import numeric
from .groups import execution_rule


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


def run_mode(rule):
    # Existing power automations are state controllers; notification-only rules
    # retain their edge-triggered behavior unless explicitly configured otherwise.
    return rule.get('run_mode') or ('maintain' if any(a['type'] == 'plug' for a in rule['actions']) else 'crossing')


class RuleEngine:
    def __init__(self):
        self.baselines = {}
        self.states = {}

    def tick(self, document, devices, solar, timezone, run_action, log, now=None, dashboard=None):
        now = time.time() if now is None else now
        active, states = set(), {}
        names = {d['id']: d['name'] for d in document['devices']}
        for rule in document['rules']:
            mode = run_mode(rule)
            if not rule['enabled']:
                states[rule['id']] = dict(code='paused', detail='Automation is paused.', mode=mode)
                continue
            active.add(rule['id'])
            signature = json.dumps(execution_rule(rule), sort_keys=True)
            memory = self.baselines.get(rule['id'])
            result = matching(rule, devices, solar, timezone, now, dashboard)
            if mode == 'maintain':
                if memory is None or memory['signature'] != signature:
                    memory = dict(signature=signature, since=None, last_attempt=float('-inf'), sent=set(), attempted=False)
                    self.baselines[rule['id']] = memory
                state = self.maintain(rule, memory, result, devices, names, run_action, log, now)
            else:
                state = self.crossing(rule, signature, memory, result, run_action, log, now)
            states[rule['id']] = dict(state, mode=mode)
        self.baselines = {key: value for key, value in self.baselines.items() if key in active}
        # Publish a complete snapshot so the API never sees a partially updated dict.
        self.states = states

    @staticmethod
    def state(code, detail):
        return dict(code=code, detail=detail)

    def maintain(self, rule, memory, result, devices, names, run_action, log, now):
        if result is None:
            memory['since'] = None
            return self.state('readings', 'Waiting for fresh readings from every condition source.')
        if not result:
            memory.update(since=None, sent=set(), attempted=False)
            return self.state('unmatched', 'Conditions do not match.')
        if memory['since'] is None:
            memory['since'] = now
        remaining = rule['for_seconds'] - (now - memory['since'])
        if remaining > 0:
            return self.state('duration', f'Conditions match; hold duration has {int(remaining + 0.999)} seconds left.')
        targets = [a for a in rule['actions'] if a['type'] == 'plug']
        needed = set()
        for action in targets:
            target = devices.get(action['device_id'], {})
            name = names.get(action['device_id'], action['device_id'])
            if target.get('hold_until', 0) > now:
                return self.state('manual_hold', f'{name}: manual hold is active. Automatic control resumes when it expires or is cleared.')
            if target.get('pending'):
                return self.state('pending', f'{name}: waiting for the queued power command to be confirmed.')
            power = fresh_value(target, 'power', now)
            if type(power) is not bool:
                return self.state('target_unavailable', f'{name}: waiting for fresh confirmed power state.')
            if power is not action['on']:
                needed.add(action['id'])
        unsent = any(a['type'] != 'plug' and a['id'] not in memory['sent'] for a in rule['actions'])
        if targets and not needed and not (memory['attempted'] and unsent):
            return self.state('satisfied', 'Conditions match; all target power states are confirmed correct.')
        if not targets and all(a['id'] in memory['sent'] for a in rule['actions']):
            return self.state('satisfied', 'Conditions match; actions have already run for this match.')
        # Retrying a failed/unconfirmed power request is bounded even when the
        # configured cooldown is zero. A failure must not consume the match.
        remaining = max(30, rule['cooldown_seconds']) - (now - memory['last_attempt'])
        if remaining > 0:
            return self.state('cooldown', f'Conditions match; next attempt in {int(remaining + 0.999)} seconds (cooldown/retry limit).')
        memory.update(last_attempt=now, attempted=True)
        log('trigger', rule['name'], 'Conditions matched. Applying actions; power changes require device confirmation.')
        failed = False
        for action in rule['actions']:
            if action['type'] == 'plug' and action['id'] not in needed:
                continue
            if action['type'] != 'plug' and action['id'] in memory['sent']:
                continue
            try:
                run_action(action, rule['name'])
                if action['type'] != 'plug':
                    memory['sent'].add(action['id'])
            except Exception as exc:
                failed = True
                log('error', rule['name'], str(exc))
        return self.state('retry' if failed else 'requested',
                          'An action failed; the rule will retry while conditions still match.' if failed else
                          'Actions requested; power changes are complete only after device confirmation.')

    def crossing(self, rule, signature, memory, result, run_action, log, now):
        if memory is None or memory['signature'] != signature or result is None:
            self.baselines[rule['id']] = {'signature': signature, 'previous': result,
                'since': None, 'fired': bool(result), 'last_fired': float('-inf')}
            if result is None:
                return self.state('readings', 'Waiting for fresh readings from every condition source.')
            return self.state('crossing', 'Baseline established. Waiting for conditions to stop matching, then match again.' if result else 'Conditions do not match; waiting for a new crossing.')
        if result is False:
            memory.update(previous=False, since=None, fired=False)
            return self.state('unmatched', 'Conditions do not match; waiting for a new crossing.')
        if memory['previous'] is None:
            memory.update(previous=True, fired=True)
            return self.state('crossing', 'Fresh baseline established. Waiting for a new crossing.')
        if memory['previous'] is False:
            memory.update(previous=True, since=now)
        if memory['fired'] or memory['since'] is None:
            return self.state('crossing', 'Conditions match. Waiting for conditions to stop matching, then match again.')
        remaining = rule['for_seconds'] - (now - memory['since'])
        if remaining > 0:
            return self.state('duration', f'Conditions match; hold duration has {int(remaining + 0.999)} seconds left.')
        memory['fired'] = True
        if now - memory['last_fired'] < rule['cooldown_seconds']:
            log('skipped', rule['name'], 'Crossed during cooldown. Waiting for the conditions to reset.')
            return self.state('crossing', 'Crossed during cooldown. Waiting for a new crossing.')
        memory['last_fired'] = now
        log('trigger', rule['name'], 'Conditions matched. Running actions in order.')
        for action in rule['actions']:
            try:
                run_action(action, rule['name'])
            except Exception as exc:
                # Only sanitized service errors are exposed by the action adapter.
                log('error', rule['name'], str(exc))
        return self.state('requested', 'Actions requested for this crossing. Check Activity for confirmation or errors.')

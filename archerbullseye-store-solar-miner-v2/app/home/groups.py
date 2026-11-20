"""Automation presentation metadata, independent of rule execution."""
import copy

GROUP_ICONS = {
    'folder': {'label':'Folder','symbol':'📁'},
    'solar': {'label':'Solar','symbol':'☀️'},
    'battery': {'label':'Battery','symbol':'🔋'},
    'power': {'label':'Power','symbol':'⚡'},
    'plug': {'label':'Plug','symbol':'🔌'},
    'temperature': {'label':'Temperature','symbol':'🌡️'},
    'humidity': {'label':'Humidity','symbol':'💧'},
    'heating': {'label':'Heating','symbol':'🔥'},
    'cooling': {'label':'Cooling','symbol':'❄️'},
    'fan': {'label':'Fan','symbol':'🌀'},
    'home': {'label':'Home','symbol':'🏠'},
    'bedroom': {'label':'Bedroom','symbol':'🛏️'},
    'garage': {'label':'Garage','symbol':'🚗'},
    'garden': {'label':'Garden','symbol':'🌿'},
    'clock': {'label':'Time','symbol':'⏰'},
    'night': {'label':'Night','symbol':'🌙'},
    'alerts': {'label':'Alerts','symbol':'🔔'},
    'telegram': {'label':'Messages','symbol':'💬'},
    'mining': {'label':'Mining','symbol':'⛏️'},
    'tools': {'label':'Tools','symbol':'🛠️'},
}


def canonical(value):
    # Browser JSON serializes integral floats as integers. That formatting
    # change must not re-baseline an otherwise unchanged automation.
    if type(value) is float and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key:canonical(item) for key,item in value.items()}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


def execution_rule(rule):
    return canonical({key:value for key,value in rule.items() if key != 'group_id'})


def execution_document(document):
    result = copy.deepcopy(document)
    result.pop('revision', None)
    result.pop('groups', None)
    result['rules'] = [execution_rule(rule) for rule in result['rules']]
    for device in result.get('devices', []):
        if not device.get('telemetry'):
            device.pop('telemetry', None)
    return result

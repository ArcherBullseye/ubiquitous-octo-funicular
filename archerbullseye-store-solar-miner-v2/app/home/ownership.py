"""Avoid two local connections/controllers owning the same dehumidifier."""


def legacy_owner(document, settings):
    device_id = settings.get('dehum_device_id')
    address = settings.get('dehum_ip')
    return next((d for d in document.get('devices', [])
                 if d.get('enabled') and d.get('kind') == 'dehumidifier' and d.get('transport') == 'wifi'
                 and ((device_id and d.get('device_id') == device_id)
                      or (address and d.get('address') == address))), None)

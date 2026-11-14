"""Triggers evaluated on reports from the local Home sensor."""
from html import escape

from ..config import home_temperature_range
from .notifications import get_bot


class TemperatureTrigger:
    def __init__(self):
        self.rule = None
        self.zone = None

    def observe(self, settings, temperature_c=None):
        """Use the first report as a baseline. Alert on entry to a hot or cold zone."""
        limits = home_temperature_range(settings) if settings.get("home_hub_enabled") else None
        bot = get_bot(settings) if limits else None
        if not bot:
            self.rule = self.zone = None
            return

        low, high = limits
        rule = (settings['home_hub_id'], settings['home_sensor_id'],
                settings['home_sensor_node'], settings['home_temperature_dp'],
                settings['home_temperature_divisor'], settings['home_temperature_unit'],
                limits, bot.token, bot.chat_id)
        if self.rule != rule:
            self.rule = rule
            self.zone = None
        if temperature_c is None:
            return

        temperature_f = round(temperature_c * 9 / 5 + 32, 1)
        zone = 'low' if temperature_f < low else 'high' if temperature_f > high else 'in range'
        previous, self.zone = self.zone, zone
        if previous is None or previous == zone or zone == 'in range':
            return

        name = escape(settings.get('home_sensor_name') or 'Home sensor')
        direction, limit = ('below', low) if zone == 'low' else ('above', high)
        bot.send(f'🌡 <b>Home temperature {direction} limit</b>\n'
                 f'{name}: {temperature_f:g} °F ({direction} {limit:g} °F)')

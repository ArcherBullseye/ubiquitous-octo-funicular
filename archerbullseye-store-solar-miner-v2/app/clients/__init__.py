"""Thin API clients — one module per external system, no app logic.

solis      Solis Cloud inverter API (readings + SOC)
luxos      LuxOS miner TCP API (cgminer port 4028) + per-IP session pool
luxor      Luxor mining pool stats
openmeteo  Weather forecast + ZIP geocoding
telegram   Telegram bot send/poll
tuya       Tuya local-LAN devices (dehumidifier)
"""

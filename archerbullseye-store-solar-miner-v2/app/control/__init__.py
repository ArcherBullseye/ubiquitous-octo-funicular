"""Control layer — the decision logic, kept pure where possible.

smart_start   sunny-day threshold lowering (pure)
eod           end-of-day SOC projection (pure)
ramp          surplus-tracking power-profile ramp (pure compute + apply)
commands      manual start/stop with per-miner holds (dashboard + Telegram)
notifications edge-triggered Telegram alerts
loop          the orchestrating control loop + weather/pool/telegram loops
"""

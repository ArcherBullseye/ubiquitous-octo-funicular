"""Entry point: build the app, start the background loops, serve."""
from dotenv import load_dotenv

load_dotenv()

from app import create_app                     # noqa: E402
from app.background import start_background_threads  # noqa: E402

flask_app, state, fleet, ramp, notifier = create_app()

if __name__ == "__main__":
    start_background_threads(state, fleet, ramp, notifier)
    flask_app.run(host="0.0.0.0", port=3000, debug=False)

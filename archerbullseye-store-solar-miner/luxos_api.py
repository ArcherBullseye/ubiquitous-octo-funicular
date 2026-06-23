import json
import socket
from typing import Optional


class LuxOsError(Exception):
    pass


class LuxOsClient:
    """
    Controls a LuxOS miner via the cgminer TCP API on port 4028.

    Authentication is a simple logon/logoff session — no username or password.
    Only one session can be active at a time on the miner, so we hold the
    session for the lifetime of this object and logoff on close().
    """

    def __init__(self, ip: str, port: int = 4028, timeout: float = 10.0):
        self.ip = ip.strip()
        self.port = port
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self.last_hashrate_mhs: float = 0.0

    def _send(self, command: str, parameter: Optional[str] = None) -> dict:
        payload: dict = {"command": command}
        if parameter is not None:
            payload["parameter"] = parameter

        try:
            with socket.create_connection((self.ip, self.port), timeout=self.timeout) as sock:
                sock.sendall(json.dumps(payload).encode())
                chunks = []
                while True:
                    chunk = sock.recv(4096)
                    if not chunk:
                        break
                    chunks.append(chunk)
                raw = b"".join(chunks).rstrip(b"\x00")
        except OSError as e:
            raise LuxOsError(f"TCP connection to {self.ip}:{self.port} failed: {e}") from e

        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise LuxOsError(f"Invalid JSON from miner: {raw[:200]}") from e

    def _status_ok(self, response: dict) -> bool:
        return response.get("STATUS", [{}])[0].get("STATUS") == "S"

    def logon(self) -> str:
        """Obtain a new session ID. Raises LuxOsError if another session is active."""
        resp = self._send("logon")
        if not self._status_ok(resp):
            msg = resp.get("STATUS", [{}])[0].get("Msg", "unknown")
            raise LuxOsError(f"logon failed: {msg}")
        self._session_id = resp["SESSION"][0]["SessionID"]
        return self._session_id

    def logoff(self) -> None:
        """Release the current session."""
        if self._session_id:
            self._send("logoff", self._session_id)
            self._session_id = None

    def _ensure_session(self) -> str:
        if not self._session_id:
            self.logon()
        return self._session_id  # type: ignore

    def get_status(self) -> list[dict]:
        """Returns the list of ASC hashboard status dicts from the 'devs' command."""
        resp = self._send("devs")
        if not self._status_ok(resp):
            msg = resp.get("STATUS", [{}])[0].get("Msg", "unknown")
            raise LuxOsError(f"devs command failed: {msg}")
        return resp.get("DEVS", [])

    def is_mining(self) -> bool:
        """
        Returns True if at least one hashboard is alive and hashing.
        Also updates self.last_hashrate_mhs (sum of MHS 5s across all boards).
        When sleeping/curtailed, all boards show Status='Dead' and MHS=0.
        """
        devs = self.get_status()
        self.last_hashrate_mhs = sum(float(d.get("MHS 5s") or 0) for d in devs)
        return any(
            d.get("Status", "Dead") != "Dead" or float(d.get("MHS 5s", 0)) > 0
            for d in devs
        )

    def start_mining(self) -> None:
        """Wake the miner up from sleep/curtailment."""
        sid = self._ensure_session()
        resp = self._send("curtail", f"{sid},wakeup")
        if not self._status_ok(resp):
            msg = resp.get("STATUS", [{}])[0].get("Msg", "unknown")
            # Already awake is fine
            if "already" not in msg.lower():
                raise LuxOsError(f"wakeup failed: {msg}")

    def stop_mining(self) -> None:
        """Put the miner to sleep."""
        sid = self._ensure_session()
        resp = self._send("curtail", f"{sid},sleep")
        if not self._status_ok(resp):
            msg = resp.get("STATUS", [{}])[0].get("Msg", "unknown")
            # Already sleeping is fine
            if "already" not in msg.lower():
                raise LuxOsError(f"sleep failed: {msg}")

    def close(self) -> None:
        self.logoff()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

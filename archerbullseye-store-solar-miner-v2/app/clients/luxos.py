"""LuxOS miner client + per-IP session pool.

LuxOsClient speaks the cgminer TCP API on port 4028. Authentication is a
simple logon/logoff session — only ONE session can be active per miner, so
sessions are precious: LuxOsPool caches one client per miner IP, persists the
session id to disk (so a restart can release a session it left behind), and
recovers from "Another session is active" by logging off the saved session.
"""
import json
import socket
from pathlib import Path
from threading import Lock
from typing import Dict, Optional


class LuxOsError(Exception):
    pass


class LuxOsClient:
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

    def _err(self, response: dict, what: str) -> LuxOsError:
        msg = response.get("STATUS", [{}])[0].get("Msg", "unknown")
        return LuxOsError(f"{what} failed: {msg}")

    # ── Session ──────────────────────────────────────────────────

    def logon(self) -> str:
        resp = self._send("logon")
        if not self._status_ok(resp):
            raise self._err(resp, "logon")
        self._session_id = resp["SESSION"][0]["SessionID"]
        return self._session_id

    def logoff(self) -> None:
        if self._session_id:
            self._send("logoff", self._session_id)
            self._session_id = None

    def _ensure_session(self) -> str:
        if not self._session_id:
            self.logon()
        return self._session_id  # type: ignore

    # ── Reads (no session required) ──────────────────────────────

    def get_status(self) -> list:
        """List of ASC hashboard status dicts from the 'devs' command."""
        resp = self._send("devs")
        if not self._status_ok(resp):
            raise self._err(resp, "devs")
        return resp.get("DEVS", [])

    def is_mining(self) -> bool:
        """True if the miner is powered on (not curtailed to sleep).
        Also updates self.last_hashrate_mhs (sum of MHS 5s across boards).

        A curtailed/sleeping miner can keep a board reporting Status='Alive'
        with 0 hashrate, so judge on the curtail/power state from config,
        not board Status — otherwise a sleeping miner reads as "on".
        """
        devs = self.get_status()
        self.last_hashrate_mhs = sum(float(d.get("MHS 5s") or 0) for d in devs)
        try:
            cfg = self.get_config()
            curtailed = str(cfg.get("CurtailMode") or "").lower() == "sleep"
            if curtailed or cfg.get("IsPowerSupplyOn") is False:
                return False
            return True
        except LuxOsError:
            # Fall back to hashrate if config is unavailable.
            return self.last_hashrate_mhs > 0

    def get_config(self) -> dict:
        """The miner's CONFIG dict (current Profile, ProfileStep, curtail and
        power-target capability flags)."""
        resp = self._send("config")
        if not self._status_ok(resp):
            raise self._err(resp, "config")
        cfg = resp.get("CONFIG", [])
        return cfg[0] if cfg else {}

    def get_profiles(self) -> list:
        """The miner's performance-profile ladder, normalized. Each entry:
        name, step, frequency (MHz), hashrate_ths, watts, voltage, is_dynamic.

        This is the dimmable-load ladder the surplus-tracking ramp drives via
        profileset (from sleep at 0 W up through each rung)."""
        resp = self._send("profiles")
        if not self._status_ok(resp):
            raise self._err(resp, "profiles")
        return [
            {
                "name":         p.get("Profile Name"),
                "step":         p.get("Step"),
                "frequency":    p.get("Frequency"),
                "hashrate_ths": p.get("Hashrate"),
                "watts":        p.get("Watts"),
                "voltage":      p.get("Voltage"),
                "is_dynamic":   p.get("IsDynamic"),
            }
            for p in resp.get("PROFILES", [])
        ]

    # ── Writes (session required) ────────────────────────────────

    def set_profile(self, profile: str) -> None:
        """Set the active performance profile (all boards) via profileset."""
        sid = self._ensure_session()
        resp = self._send("profileset", f"{sid},{profile}")
        if not self._status_ok(resp):
            raise self._err(resp, f"profileset '{profile}'")

    def start_mining(self) -> None:
        """Wake the miner from sleep/curtailment."""
        sid = self._ensure_session()
        resp = self._send("curtail", f"{sid},wakeup")
        if not self._status_ok(resp):
            msg = resp.get("STATUS", [{}])[0].get("Msg", "unknown")
            if "already" not in msg.lower():  # already awake is fine
                raise LuxOsError(f"wakeup failed: {msg}")

    def stop_mining(self) -> None:
        """Put the miner to sleep."""
        sid = self._ensure_session()
        resp = self._send("curtail", f"{sid},sleep")
        if not self._status_ok(resp):
            msg = resp.get("STATUS", [{}])[0].get("Msg", "unknown")
            if "already" not in msg.lower():  # already sleeping is fine
                raise LuxOsError(f"sleep failed: {msg}")

    def close(self) -> None:
        self.logoff()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class LuxOsPool:
    """One cached, session-holding client per miner IP.

    Timeout is shorter than the client default so an unreachable miner fails
    fast and the control loop falls back to driving the other one instead of
    stalling a whole cycle.
    """

    TIMEOUT = 6.0

    def __init__(self, session_dir: Path = Path("data")):
        self.session_dir = session_dir
        self._clients: Dict[str, LuxOsClient] = {}
        self._lock = Lock()

    # Per-miner session file so each miner keeps its own LuxOS session id
    # across restarts (and a restart can release the session it left behind).
    def _session_file(self, ip: str) -> Path:
        safe = ip.replace(":", "_").replace("/", "_").replace(".", "-")
        return self.session_dir / f"luxos_session_{safe}.txt"

    def _save_session(self, ip: str, sid: Optional[str]) -> None:
        try:
            f = self._session_file(ip)
            f.parent.mkdir(parents=True, exist_ok=True)
            if sid:
                f.write_text(sid)
            elif f.exists():
                f.unlink()
        except Exception:
            pass

    def _load_session(self, ip: str) -> Optional[str]:
        try:
            f = self._session_file(ip)
            if f.exists():
                return f.read_text().strip() or None
        except Exception:
            pass
        return None

    def get(self, ip: str) -> LuxOsClient:
        """Cached client with a live session, creating/logging on as needed.
        Recovers from a stale session left by a previous run."""
        with self._lock:
            client = self._clients.get(ip)
            if client is not None and client._session_id:
                return client

            client = LuxOsClient(ip=ip, timeout=self.TIMEOUT)
            try:
                client.logon()
                self._save_session(ip, client._session_id)
            except LuxOsError as e:
                if "Another session is active" in str(e):
                    saved_sid = self._load_session(ip)
                    if saved_sid:
                        try:
                            tmp = LuxOsClient(ip=ip, timeout=self.TIMEOUT)
                            tmp._session_id = saved_sid
                            tmp.logoff()
                            self._save_session(ip, None)
                        except Exception:
                            pass
                    client.logon()
                    self._save_session(ip, client._session_id)
                else:
                    raise

            self._clients[ip] = client
            return client

    def drop(self, ip: str) -> None:
        """Discard a (likely broken) cached client, releasing its session."""
        with self._lock:
            client = self._clients.pop(ip, None)
            if client is not None:
                try:
                    client.close()
                    self._save_session(ip, None)
                except Exception:
                    pass

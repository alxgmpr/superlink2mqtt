"""One asynchronous extraction at a time; secrets stay in server memory."""
import secrets
import threading
import time

from .build import build_helper
from .decoder import decode_capture
from .ssh import SSH, PREFLIGHT, RecoveryRequired, capture, discard_command, endpoint


class ExtractionJobs:
    def __init__(self, *, demo=False):
        self.demo = demo
        self.lock = threading.RLock()
        self.probe_result = None
        self.job = None
        self.document = None
        self.expires = 0
        self.expiry_timer = None
        self.source = None
        self.recovery_path = None
        self.cancelled = threading.Event()

    def _enabled(self):
        if self.demo:
            raise ValueError("Demo mode cannot connect to Ubiquiti gateways")
        if self.cancelled.is_set():
            raise ValueError("Service is shutting down")

    def probe(self, body):
        self._enabled()
        host, port = endpoint(body.get("host"), body.get("port", 22))
        try:
            ssh = SSH(host, port)
            try:
                result = {"probe_id": secrets.token_urlsafe(24), "host": host, "port": port,
                          "fingerprint": ssh.fingerprint}
            finally:
                ssh.close()
        except Exception:
            raise ValueError("Could not read the gateway's SSH host key; check its IP, port and SSH access") from None
        with self.lock:
            self.probe_result = (result, time.monotonic() + 300)
        return result.copy()

    def start(self, body):
        self._enabled()
        if body.get("confirmed") is not True:
            raise ValueError("Verify the SSH fingerprint and confirm the brief gateway interruption")
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not 1 <= len(username) <= 64 or any(ord(c) < 33 for c in username):
            raise ValueError("Enter the gateway's SSH username")
        if not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise ValueError("Enter the gateway's SSH password")
        with self.lock:
            self._expire()
            if self.job and self.job["state"] not in ("ready", "failed", "expired", "imported"):
                raise ValueError("An extraction is active or source recovery is required; check its status")
            if not self.probe_result or self.probe_result[1] < time.monotonic() or body.get("probe_id") != self.probe_result[0]["probe_id"]:
                raise ValueError("Probe and verify the source gateway again")
            source = self.probe_result[0]
            self.source = source
            if self.expiry_timer:
                self.expiry_timer.cancel()
            self.probe_result = None
            self.document = None
            self.job = {"id": secrets.token_urlsafe(24), "state": "building",
                        "message": "Preparing the capture helper before connecting",
                        "host": source["host"], "fingerprint": source["fingerprint"]}
            result = self.job.copy()
            threading.Thread(target=self._run, args=(source, username, password),
                             name="superlink-extraction", daemon=True).start()
            return result

    def _progress(self, state, message):
        with self.lock:
            self.job.update(state=state, message=message)

    def _run(self, source, username, password):
        ssh = None
        try:
            helper = build_helper()
            if self.cancelled.is_set():
                return
            self._progress("connecting", "Connecting with the verified SSH host key")
            ssh = SSH(source["host"], source["port"])
            ssh.authenticate(source["fingerprint"], username, password)
            password = None
            raw = capture(ssh, helper, self._progress, self.cancelled)
            document, excluded = decode_capture(raw)
            raw = None
            with self.lock:
                if self.cancelled.is_set():
                    return
                self.document = document
                self.expires = time.monotonic() + 600
                self.expiry_timer = threading.Timer(600, self.status)
                self.expiry_timer.daemon = True
                self.expiry_timer.start()
                self.job.update(state="ready", message="Source service restored. Review the acknowledged key pairs; stop the source gateway before importing",
                                count=len(document["devices"]), excluded=excluded,
                                expires_in=600)
        except RecoveryRequired as exc:
            with self.lock:
                self.recovery_path = exc.path
                self.job.update(state="recovery_required", message=str(exc), recovery_command=exc.recovery_command)
        except ValueError as exc:
            self._progress("failed", str(exc))
        except Exception:
            self._progress("failed", "Extraction failed; check SSH credentials and source connectivity, then retry")
        finally:
            password = None
            if ssh:
                ssh.close()

    def recover(self, body):
        self._enabled()
        username, password = body.get("username"), body.get("password")
        if not isinstance(username, str) or not 1 <= len(username) <= 64 or not isinstance(password, str) or not 1 <= len(password) <= 1024:
            raise ValueError("Re-enter SSH credentials to restore and verify the source gateway")
        with self.lock:
            if not self.job or self.job["state"] != "recovery_required" or body.get("job_id") != self.job["id"]:
                raise ValueError("No matching source gateway needs recovery")
            self.job.update(state="recovering", message="Restoring and verifying the source gateway")
            threading.Thread(target=self._recover, args=(username, password),
                             name="superlink-recovery", daemon=True).start()
            return self.job.copy()

    def _recover(self, username, password):
        ssh = None
        try:
            ssh = SSH(self.source["host"], self.source["port"])
            ssh.authenticate(self.source["fingerprint"], username, password)
            password = None
            path = self.recovery_path
            ssh.run(f"if [ -f {path}/changed ] || [ -f {path}/worker ]; then sh {path}/restore.sh {path}; fi", timeout=55)
            if ssh.run(PREFLIGHT) != b"supported":
                raise ValueError()
            ssh.run(f"if [ -d {path} ]; then {discard_command(path)}; fi")
            with self.lock:
                self.job.pop("recovery_command", None)
                self.job.update(state="failed", message="Source service and cleanup verified. You can start a new extraction")
        except Exception:
            self._progress("recovery_required", "Source recovery could not be verified. Check connectivity, host identity and SSH credentials before retrying recovery")
        finally:
            password = None
            if ssh:
                ssh.close()

    def _expire(self):
        if self.document is not None and time.monotonic() >= self.expires:
            self.document = None
            self.job.update(state="expired", message="Extracted keys expired; run extraction again")

    def status(self):
        with self.lock:
            self._expire()
            result = self.job.copy() if self.job else {"state": "idle"}
            if self.document is not None:
                result["expires_in"] = max(0, int(self.expires - time.monotonic()))
            return result

    def use_document(self, job_id, operation, *, consume=False):
        with self.lock:
            self._expire()
            if not self.job or self.job["id"] != job_id or self.job["state"] != "ready" or self.document is None:
                raise ValueError("Extraction is not ready or has expired; extract and review again")
            result = operation(self.document)
            if consume:
                self.document = None
                self.job.update(state="imported", message="Keys imported; waiting for sensors to reconnect")
            return result

    def close(self):
        self.cancelled.set()
        if self.expiry_timer:
            self.expiry_timer.cancel()
        with self.lock:
            self.document = None
            self.probe_result = None

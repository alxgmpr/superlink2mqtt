"""Pinned SSH transport and the firmware-qualified, self-restoring capture job."""
import base64
import hashlib
import ipaddress
import secrets
import socket
import threading
import time

import paramiko

from .build import NATIVE
from .decoder import MAX_CAPTURE


class RecoveryRequired(ValueError):
    def __init__(self, path):
        super().__init__("Could not verify service restoration and capture cleanup. Reconnect to the source gateway before retrying")
        self.path = path
        self.recovery_command = f"sh {path}/restore.sh {path}"


def endpoint(host, port):
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        raise ValueError("Enter the gateway's private LAN IP address") from None
    allowed = (ipaddress.ip_network("10.0.0.0/8"), ipaddress.ip_network("172.16.0.0/12"),
               ipaddress.ip_network("192.168.0.0/16"), ipaddress.ip_network("fc00::/7"))
    if not any(ip.version == net.version and ip in net for net in allowed):
        raise ValueError("Extraction requires a private LAN IP address")
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError("SSH port must be between 1 and 65535")
    return str(ip), port


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


class SSH:
    def __init__(self, host, port):
        self.transport = None
        sock = socket.create_connection((host, port), timeout=3)
        try:
            self.transport = paramiko.Transport(sock)
            self.transport.banner_timeout = 3
            self.transport.auth_timeout = 5
            self.transport.start_client(timeout=3)
            self.fingerprint = fingerprint(self.transport.get_remote_server_key())
        except Exception:
            if self.transport:
                self.transport.close()
            sock.close()
            raise

    def close(self):
        self.transport.close()

    def authenticate(self, expected, username, password):
        # Host identity must match the user's verified probe before sending credentials.
        if self.fingerprint != expected:
            raise ValueError("SSH host key changed since verification; probe and verify the gateway again")
        self.transport.auth_password(username, password, fallback=False)

    def run(self, command, *, data=None, limit=4096, timeout=10):
        channel = self.transport.open_session(timeout=5)
        channel.settimeout(timeout)
        # Channel.exec_command can wait for an SSH acknowledgement independently
        # of socket read timeouts. Closing the channel bounds that wait too.
        deadline = time.monotonic() + timeout
        timer = threading.Timer(timeout, channel.close)
        timer.daemon = True
        timer.start()
        try:
            channel.exec_command(command)
            if data is not None:
                channel.sendall(data)
            channel.shutdown_write()
            result = bytearray()
            while True:
                while channel.recv_ready():
                    result.extend(channel.recv(65536))
                    if len(result) > limit:
                        raise ValueError("Unexpected gateway response size")
                # Never retain stderr; it can contain sensitive source configuration.
                while channel.recv_stderr_ready():
                    channel.recv_stderr(4096)
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError()
                time.sleep(0.02)
            if channel.recv_exit_status() != 0:
                raise ValueError("Gateway command failed; check SSH permissions and supported firmware")
            return bytes(result)
        finally:
            timer.cancel()
            channel.close()


PREFLIGHT = r'''set -eu
[ "$(id -u)" = 0 ]
[ "$(uname -m)" = armv7l ]
[ -x /usr/sbin/lorabrd ]
[ -x /etc/init.d/lorabr ]
[ -f /etc/persistent/cfg/lorabr.json ]
for tool in timeout chrt pidof tr grep sleep; do command -v "$tool" >/dev/null; done
timeout -s KILL 1 true
pids=$(pidof lorabrd)
case "$pids" in ''|*[!0-9]*) exit 1;; esac
[ "$(tr '\000' ' ' < /proc/$pids/cmdline)" = '/usr/sbin/lorabrd --syslog /etc/persistent/cfg/lorabr.json ' ]
grep -q '/libssl.so.3' /proc/$pids/maps
! tr '\000' '\n' < /proc/$pids/environ | grep -q '^LD_PRELOAD='
printf supported
'''


def discard_command(path):
    # path is generated here (fixed prefix + random hex); never a request parameter.
    return (f"set -eu; rm -f {path}/capture.bin {path}/capture.so {path}/pid "
            f"{path}/worker {path}/captured {path}/changed {path}/status {path}/run.sh "
            f"{path}/watchdog.sh {path}/restore.sh {path}/restored; rmdir {path}; "
            f"if [ \"$(cat /tmp/superlink-extraction.lock/owner)\" = {path} ]; then "
            "rm /tmp/superlink-extraction.lock/owner; rmdir /tmp/superlink-extraction.lock; fi; "
            "[ ! -d /tmp/superlink-extraction.lock ]")


def capture(ssh, helper, progress, cancelled):
    """All paths and shell commands are internal constants; no credentials in argv."""
    progress("checking", "Checking the source gateway before restarting anything")
    try:
        if ssh.run(PREFLIGHT) != b"supported":
            raise ValueError()
    except ValueError:
        raise ValueError("Unsupported gateway layout. Requires root SSH, ARMv7 lorabrd, OpenSSL 3 and the lorabr service") from None
    path = "/tmp/superlink-extract-" + secrets.token_hex(12)
    # Lock reservation is one command; no forced removal of another extraction job.
    try:
        ssh.run(f"umask 077; mkdir /tmp/superlink-extraction.lock || exit 1; "
                f"if mkdir {path}; then printf %s {path} > /tmp/superlink-extraction.lock/owner; "
                "else rmdir /tmp/superlink-extraction.lock; exit 1; fi")
    except Exception:
        try:
            ssh.run(f"if [ -d {path} ]; then {discard_command(path)}; fi")
        except Exception:
            raise RecoveryRequired(path) from None
        raise ValueError("Could not reserve a private capture directory; another extraction may already own this gateway") from None
    launched = False
    cleaned = False
    try:
        for name, data in [("capture.so", helper)] + [(name, (NATIVE / name).read_bytes())
                                                     for name in ("restore.sh", "run.sh", "watchdog.sh")]:
            ssh.run(f"umask 077; cat > {path}/{name}", data=data)
        if cancelled.is_set():
            raise ValueError("Extraction cancelled before gateway restart")
        progress("capturing", "Capturing controller provisioning for 45 seconds; leave UniFi connected")
        # If delivery is ambiguous, assume the restart could have started.
        launched = True
        ssh.run(f"(trap '' HUP; sh {path}/watchdog.sh {path} </dev/null >/dev/null 2>&1 & "
                f"watchdog=$!; kill -0 $watchdog || exit 1; "
                f"sh {path}/run.sh {path} </dev/null >/dev/null 2>&1 &)")
        deadline = time.monotonic() + 90
        status = b""
        while time.monotonic() < deadline:
            # Browser disconnection does not cancel the job. Service shutdown does;
            # the remote worker and watchdog still complete their bounded cleanup.
            if cancelled.wait(1):
                raise RecoveryRequired(path)
            status = ssh.run(f"cat {path}/status 2>/dev/null || :").strip()
            if status in (b"ready", b"failed", b"recovery_required"):
                break
            if status == b"restoring":
                progress("restoring", "Restoring the gateway's normal service")
        if status not in (b"ready", b"failed"):
            raise RecoveryRequired(path)
        # Verify again through the ordinary process, not merely a sentinel file.
        if ssh.run(PREFLIGHT) != b"supported":
            raise RecoveryRequired(path)
        raw = ssh.run(f"if [ -f {path}/capture.bin ]; then cat {path}/capture.bin; fi",
                      limit=MAX_CAPTURE + 20) if status == b"ready" else b""
        ssh.run(discard_command(path))
        cleaned = True
        if not raw:
            raise ValueError("Capture produced no TLS traffic; the gateway service was restored. Check the controller connection and retry")
        return raw
    except Exception:
        if not cleaned:
            if not launched:
                try:
                    ssh.run(discard_command(path))
                except Exception:
                    raise RecoveryRequired(path) from None
            else:
                # Do not race an in-flight stop/start by issuing another restore.
                # The independent worker/watchdog owns restoration after launch.
                raise RecoveryRequired(path) from None
        raise

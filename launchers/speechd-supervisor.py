#!/usr/bin/env python3
"""Hung-server watchdog for the snap's speech-dispatcher daemon (T47).

Run as the MAIN process of the speech-dispatcher app (see
bin/run-speech-dispatcher): the snapd-generated socket unit passes the
listening fd here (LISTEN_FDS, systemd socket activation), this script
spawns the real server as a CHILD with LISTEN_PID rewritten to the
child's pid (sd_listen_fds requires it), and babysits it forever:

  - every PROBE_INTERVAL s: connect to the snapd socket and wait for
    the server's protocol greeting, with a short timeout. connect()
    alone is NOT enough to detect a hang: the listening fd lives in
    the unit (and in this process), so the KERNEL completes new
    connections into the backlog even when the server is wedged. Only
    the greeting proves the server's main loop is alive. This is a
    protocol-handshake probe, not a speech round-trip: a healthy
    server accepts + greets immediately even while it is synthesizing
    (dispatch is asynchronous through the output-module child), so it
    is false-positive free under normal speech. Measured on the
    T47 disposable VM (see notes/todo/a11y-work/T47-watchdog-lint.txt).

  - a probe that connects but gets NO greeting bytes within
    PROBE_TIMEOUT = the main loop is hung (the T41/T44-era wedge:
    alive, unresponsive, SIGTERM swallowed, restart-condition:
    on-failure can never see it) -> SIGKILL the child and respawn.
    The listening fd never leaves this process, so connections queued
    during the wedge stay in the backlog and are served by the fresh
    server: the watchdog does not fight the socket-activation unit.

  - an exiting child (crash, external kill) is respawned with
    exponential backoff, mirroring systemd; a fresh server that never
    greets within STARTUP_TIMEOUT is treated as born-hung and killed
    the same way.

The first greeting after each spawn also ARMS the aggressive
PROBE_TIMEOUT for subsequent probes, so slow module init (espeak
starts up in the 1-3 s range, T44 probes) can never trip the
threshold. This is the self-healing primitive T44 proved: a KILLED
server recovers via the socket unit (fresh server serves the next
client, rc=0); a hung one is now guaranteed to be killed.

No new AppArmor surface is needed (checked against the strict
template): the probe connect rides the snap-private runtime dir grant
`owner /run/user/[0-9]*/snap.<snap>/** mrwklix` (template.go:393-394),
and the SIGKILL rides the same-snap signal grant
`signal peer=snap.<snap>.*` (template.go:425-426). No /proc scanning
of foreign pids (only owner @{pid} rules exist) - the child pid is
known by construction.

Stdlib only; the snap already stages python3 for orca.
"""

import os
import select
import signal
import socket
import sys
import time

PROBE_INTERVAL = 15.0    # seconds between probes once armed
PROBE_TIMEOUT = 10.0     # greeting wait: kill threshold (normal <1 s, measured)
STARTUP_TIMEOUT = 30.0   # first-greeting grace after each spawn
STOP_GRACE = 3.0         # SIGTERM -> SIGKILL grace on unit stop
BACKOFF_BASE = 1.0       # respawn backoff for exiting children
BACKOFF_CAP = 30.0
STORM_AFTER = 5          # consecutive kills w/o any greeting -> widen cadence
STORM_INTERVAL = 60.0
TICK = 0.25              # scheduler granularity / stop responsiveness

SOCK_REL = "speech-dispatcher/speechd.sock"
SERVER = os.path.join(os.environ.get("SNAP", "/snap/ubuntu-desktop-a11y/current"),
                      "usr/bin/speech-dispatcher")
SERVER_ARGS = [SERVER, "-s", "-t", "0"]  # the deb's own user-service argv


def log(msg):
    sys.stderr.write("[speechd-supervisor] %s\n" % msg)
    sys.stderr.flush()


def activation_fds():
    """The listening fds systemd passed us (SD_LISTEN_FDS_START=3..)."""
    fds = []
    startdir = "/proc/self/fd"  # own pid: granted by the strict template
    for entry in os.listdir(startdir):
        try:
            fd = int(entry)
        except ValueError:
            continue
        if fd <= 2:
            continue
        try:
            if os.path.islink(os.path.join(startdir, entry)) and \
                    "socket:" in os.readlink(os.path.join(startdir, entry)):
                fds.append(fd)
        except OSError:
            continue
    return sorted(fds)


def spawn_server(sock_path, use_fds):
    """Fork+exec the real server; rewrite LISTEN_PID to the child."""
    pid = os.fork()
    if pid == 0:
        try:
            child_fds = activation_fds()
            if use_fds:
                # PEP 446: make sure the activation fds cross exec()
                # even if the interpreter's startup inherited them
                # non-inheritable.
                for fd in child_fds:
                    os.set_inheritable(fd, True)
            else:
                # Self-bind mode: a previous server instance may have
                # died leaving its bound socket file at the path; the
                # deb's server needs a clean bind.
                try:
                    if os.path.exists(sock_path):
                        os.unlink(sock_path)
                except OSError:
                    pass
            os.environ["LISTEN_PID"] = str(os.getpid())
            os.execv(SERVER, SERVER_ARGS)
        except Exception as exc:  # pragma: no cover
            log("exec %s failed: %r" % (SERVER, exc))
            os._exit(127)
    log("spawned server pid=%d (%s)" %
        (pid, "LISTEN_FDS passed through" if use_fds else "self-bind mode"))
    return pid


def probe(timeout, sock_path):
    """Connect + greeting read. Returns (ok, detail, elapsed)."""
    t0 = time.monotonic()
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        s.settimeout(timeout)
        s.connect(sock_path)  # kernel-level: succeeds even vs a hung server
        # Read the greeting in TICK slices so a stop request is honored
        # promptly and the deadline is exact (PEP 475 would resume a
        # plain timed recv through the handler run).
        deadline = time.monotonic() + timeout
        data = b""
        while time.monotonic() < deadline:
            wait = min(TICK, deadline - time.monotonic())
            r, _, _ = select.select([s], [], [], max(wait, 0.01))
            if r:
                chunk = s.recv(256)
                if chunk:
                    # Any greeting bytes = the server main loop is alive.
                    return True, chunk[:64], time.monotonic() - t0
                return False, "EOF (server closed without greeting)", \
                    time.monotonic() - t0
        return False, "no greeting within %.1fs" % timeout, \
            time.monotonic() - t0
    except OSError as exc:
        return False, "%s" % exc, time.monotonic() - t0
    finally:
        try:
            s.close()
        except OSError:
            pass


class Supervisor:
    def __init__(self, sock_path, use_fds=True):
        self.sock_path = sock_path
        self.use_fds = use_fds
        self.child = None
        self.stopping = False
        self.greeted_ever = False
        self.consecutive_kills = 0
        self.spawn_backoff = 0.0
        signal.signal(signal.SIGTERM, self._req_stop)
        signal.signal(signal.SIGINT, self._req_stop)
        signal.signal(signal.SIGHUP, self._req_stop)

    def _req_stop(self, *_):
        self.stopping = True

    def reap(self):
        """Non-blocking reap; True if the child exited on its own."""
        if self.child is None:
            return False
        try:
            pid, status = os.waitpid(self.child, os.WNOHANG)
        except ChildProcessError:
            self.child = None
            return True
        if pid == self.child:
            log("server pid=%d exited (status=0x%x)" % (pid, status))
            self.child = None
            return True
        return False

    def kill(self, why):
        pid, self.child = self.child, None
        if pid is None:
            return
        log("KILL pid=%d (%s) - hung-server watchdog" % (pid, why))
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        self.consecutive_kills += 1

    def wait_stopped_child(self):
        """Unit stop: forward SIGTERM, grace, SIGKILL."""
        pid, self.child = self.child, None
        if pid is None:
            return
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + STOP_GRACE
        while time.monotonic() < deadline:
            try:
                wpid, _ = os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                break
            if wpid == pid:
                break
            time.sleep(0.1)
        else:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            os.waitpid(pid, 0)
        except ChildProcessError:
            pass
        log("stopped cleanly (server pid=%d reaped)" % pid)

    def cycle(self, timeout, label):
        """One probe against the current child. Returns success."""
        ok, detail, elapsed = probe(timeout, self.sock_path)
        if ok:
            if not self.greeted_ever:
                log("first greeting after spawn in %.2fs: %r"
                    % (elapsed, detail))
                self.greeted_ever = True
            self.consecutive_kills = 0
            self.spawn_backoff = 0.0
            return True
        log("probe FAILED (%s): %s after %.2fs" % (label, detail, elapsed))
        self.kill(label)
        return False

    def run(self):
        log("watchdog up: socket=%s server=%s probe_interval=%.0fs "
            "probe_timeout=%.0fs startup_timeout=%.0fs"
            % (self.sock_path, SERVER, PROBE_INTERVAL, PROBE_TIMEOUT,
               STARTUP_TIMEOUT))
        while not self.stopping:
            if self.child is None:
                # (Re)spawn: exponential backoff for exiting children;
                # a widened cadence once kills repeat without ANY greeting.
                delay = self.spawn_backoff
                if self.consecutive_kills >= STORM_AFTER:
                    delay = max(delay, 30.0)
                if delay:
                    log("respawn in %.0fs" % delay)
                    deadline = time.monotonic() + delay
                    while time.monotonic() < deadline and not self.stopping:
                        time.sleep(TICK)
                    if self.stopping:
                        break
                self.child = spawn_server(self.sock_path, self.use_fds)
                # Startup grace probe (also arms PROBE_TIMEOUT on success).
                self.cycle(STARTUP_TIMEOUT, "startup")
                continue
            # Armed cadence (widened under the kill-storm guard).
            interval = PROBE_INTERVAL
            if self.consecutive_kills >= STORM_AFTER:
                interval = STORM_INTERVAL
            deadline = time.monotonic() + interval
            while time.monotonic() < deadline and not self.stopping:
                self.reap()
                time.sleep(TICK)
            if self.stopping:
                break
            if self.reap():
                self.spawn_backoff = \
                    min(max(self.spawn_backoff, BACKOFF_BASE) * 2,
                        BACKOFF_CAP)
                continue
            self.cycle(PROBE_TIMEOUT, "hang")
        self.wait_stopped_child()


def main():
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if not xdg:
        log("FATAL: XDG_RUNTIME_DIR unset")
        return 1
    n_fds = os.environ.get("LISTEN_FDS", "")
    fds = activation_fds()
    if n_fds == "1" and len(fds) < 1:
        # systemd SAID it passed fds but none arrived as sockets:
        # contradictory state, refuse (a spawn here serves nobody).
        log("FATAL: LISTEN_FDS=1 but no socket fd inherited; the unit "
            "must be started by systemd socket activation")
        return 1
    if not os.path.exists(SERVER):
        log("FATAL: server binary missing: %s" % SERVER)
        return 1
    sock_path = os.path.join(xdg, SOCK_REL)
    if n_fds != "1":
        # Started WITHOUT the socket-activation fd. Observed live on the
        # T47 VM: a snapd refresh race can leave the .socket unit failed
        # (the endured old server holds the socket path while snapd
        # regenerates units), and a manual `snap run` never has fds.
        # Degrade to the pre-T47 behaviour with a loud warning instead
        # of failing the unit: speech must not die because snapd's unit
        # generation hiccuped. The server binds the socket path itself
        # (spawn_server unlinks the stale file per spawn - in this mode
        # nothing else owns the path); the watchdog probe works
        # identically against a self-bound socket, so self-healing is
        # preserved either way. The RIGHT state is the socket unit
        # active (fd held by systemd across kills; queued connections
        # served by the fresh server): check
        # snap.<snap>.speech-dispatcher.speechd-socket.socket.
        log("WARNING: running WITHOUT the socket-activation fd "
            "(LISTEN_FDS=%r) - the server will bind %s itself; check "
            "the speechd-socket.socket unit" % (n_fds, sock_path))
        Supervisor(sock_path, use_fds=False).run()
        return 0
    Supervisor(sock_path).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

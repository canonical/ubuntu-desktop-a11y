# ubuntu-desktop-a11y

Snapped accessibility provider for Ubuntu Core Desktop — **Spike 1**
(build + pack + static verification only; no VM work).

Implements the T11 design review
(`notes/todo/a11y-work/T11-thin-a11y-design-review.txt`, sections 2–4):
one strictly confined snap on the **stock `core26` base** (that is the
point — NOT `core26-desktop`) carrying the whole accessibility payload
that the fat base used to stage, split into three apps in one snap:

| App | Payload | Wiring |
|---|---|---|
| `bus` | `at-spi2-core`: `at-spi-bus-launcher`, `at-spi2-registryd`, `accessibility.conf`, `accessibility-services/` | `daemon: dbus`, `daemon-scope: user`, `activates-on: [dbus-a11y]`; snapd generates the session-bus activation file for `org.a11y.Bus` (no base-rootfs service file) |
| `orca` | `orca` + python3/gi + typelibs + brlapi/louis bindings (no `brltty` daemon — separate privilege domain per T11 §2) | plain app; `a11y-bus-client` dbus plug to our own `org.a11y.Bus` slot; `SPEECHD_ADDRESS` points at the same-snap speechd socket |
| `speech-dispatcher` | `speech-dispatcher` + `speech-dispatcher-espeak-ng` + `espeak-ng-data` | `daemon: simple`, `daemon-scope: user`, snapd socket-activated (`$XDG_RUNTIME_DIR/speech-dispatcher/speechd.sock`, matching the deb's own user socket unit); `audio-playback` plug for the pipewire snap |

## Why stage-packages is fine here

Staging regular debs is **normal snap packaging**. The "fat/thin"
staging restriction this project enforces (T10) is **base-snap
specific**: `core-base-desktop`'s `desktop-payload-debs` part exists to
keep the *base* rootfs thin and must not grow a dependency closure.
A strictly confined application snap like this one is a consumer, not
the base; its staged closure is confined to the snap's mount namespace
and does not touch the base rootfs contract.

## Build

    snapcraft pack

(Uses the project's snapcraft snap and its LXD build instance, as in
the other desktop repos. No `go-build` wrapper for this spike per
tasking; add one following `notes/library/dev-branch-conventions.txt`
only if this graduates past spike status.)

## Layout choices (compiled-in path surgery, no patches)

`at-spi-bus-launcher` (2.60.4, verified via `strings` on the deb):
- passes `--config-file=/usr/share/defaults/at-spi2/accessibility.conf`
  (after checking `/etc/at-spi2/`, which neither base nor snap ship) →
  **layout bind** `/usr/share/defaults/at-spi2` → `$SNAP/...`;
- the config's `<servicedir>` is
  `/usr/share/dbus-1/accessibility-services` → **layout bind**;
- registry activation execs `/usr/libexec/at-spi2-registryd` →
  **layout symlink** (no conflict: core26 rev 750 ships none of these
  paths);
- the private bus daemon is `/usr/bin/dbus-daemon` (the launcher's
  compiled-in fallback; the preferred `/usr/bin/dbus-broker-launch` is
  NOT in core26 and dbus-broker has no AppArmor mediation) → **no
  layout**: core26 ships the binary and the strict AppArmor template
  already grants it `ixr`
  (`snapd/interfaces/apparmor/template.go:566`); a symlink layout would
  fail anyway — `snap-update-ns` refuses to replace an existing base
  file (`cmd/snap-update-ns/change.go:187`).

`libespeak-ng` resolves its data at
`/usr/lib/<triplet>/espeak-ng-data` → **layout bind** + `ESPEAK_DATA_PATH`
env fallback in the speechd wrapper.

## Socket placement deviation (spike question, not a decision)

The launcher derives the a11y socket dir from `g_get_user_runtime_dir()`;
inside a strict snap that is the **snap-private** runtime dir
`/run/user/<uid>/snap.ubuntu-desktop-a11y/at-spi` (granted to same-snap
apps by `template.go:393-394`), not the conventional
`/run/user/<uid>/at-spi`. Per tasking and T11 §3 this spike **does not
fight it**: same-snap clients (orca) discover the address dynamically
via `GetAddress` and the private path is inside their own runtime dir.
External-client policy is gate work regardless. T13 must record what
the launcher actually did at runtime.

## Known spike questions for T13 (runtime)

1. Does `GetAddress` activation actually start
   `snap.ubuntu-desktop-a11y.bus.service`, and does the launcher
   register `org.a11y.Bus` on the session bus under its AppArmor label?
2. Does the private bus come up as `<type>accessibility</type>` with
   registry activation succeeding inside the namespace (layouts +
   `ixr` base dbus-daemon)?
3. Do same-snap clients (orca) connect to the private bus at
   `$XDG_RUNTIME_DIR/at-spi/bus` and to speechd at
   `$XDG_RUNTIME_DIR/speech-dispatcher/speechd.sock`?
4. Does speechd's socket activation work under snapd's generated units
   (sd_listen_fds path), and does `PULSE_SERVER` at
   `/run/user/<uid>/snap.pipewire/pulse/native` produce audible output
   with `audio-playback` connected to the pipewire snap?
5. Where does the launcher actually place the socket, and what does the
   session-bus-side client policy need for external (other-snap)
   readers?

## Not in scope (post-spike, per T11)

Lifecycle integration (gsd-a11y-settings targeting
`snap.ubuntu-desktop-a11y.orca.service`), braille hardware, external
client policy, image seeding guards, migration of the fat fallback.

# benchymark: getting a new Qt6 app through the AsteroidOS build — upstream findings

Date: 2026-07-28
Branch: `onboard-discovery-rework`
Scope: build-host side only. Nothing here is a claim about watch behaviour.

## Why this exists

`benchymark` is the FPS benchmark, pivoted out of the `nutty-benchy`
watchface because a watchface could not hold the screen awake. Building the
first new app in this workspace surfaced three build-system problems that are
not specific to benchymark: any contributor adding a new app on a modern
distro host hits at least two of them. They are written up here so they can be
raised with co-maintainers, with the local workaround recorded beside each.

Everything below was reproduced on the w541 (Arch bare metal, native build, no
container). Confidence is stated per finding.

## Method

Iterative `devtool modify` / `devtool build` cycles in a workspace recipe,
sources rsynced in from the git-managed clone (no git work in the build tree).
Failures were read from the actual `log.do_*` files rather than from bitbake's
summary line, and each fix was verified by re-running the failing task rather
than by inspecting the variable that was supposed to change it.

## F1 — cmake-native picks up the host's libidn2

**Confidence: confirmed** (reproduced, fixed, rebuild verified).

cmake-native vendors curl, which does an unconditional `set(USE_LIBIDN2 ON)`
followed by `find_package(Libidn2)`. On a host that has libidn2 installed —
Arch does by default — this finds the *system* library. Yocto correctly hides
host *includes*, so configure succeeds and the compile then dies on the
missing header.

This is the general shape worth naming: **Yocto's host isolation covers
include paths more thoroughly than linker search paths**, so a `find_package`
that probes for a library can succeed against the host while the matching
header is invisible. The failure surfaces at compile time, far from the cause.

Local fix, in `build/conf/local.conf`:

```
CMAKE_EXTRACONF:append:pn-cmake-native = " -DCMAKE_DISABLE_FIND_PACKAGE_Libidn2=ON"
```

Note for anyone reproducing: it must be `CMAKE_EXTRACONF`, **not**
`EXTRA_OECMAKE`. cmake-native bootstraps itself and does not read
`EXTRA_OECMAKE`; setting that variable changes nothing and looks like the fix
failed.

## F2 — systemd-systemctl-native picks up the host's PAM

**Confidence: confirmed** (same family as F1, same verification).

Meson finds `/usr/lib/libpam.so` on the host and enables PAM; the compile then
fails on `security/pam_ext.h`. Identical mechanism to F1.

```
EXTRA_OEMESON:append:pn-systemd-systemctl-native = " -Dpam=disabled"
```

## F3 — asteroid-generate-desktop fails opaquely on a missing i18n header

**Confidence: confirmed** (script read, failure reproduced, fix verified by
running the generator by hand and then through a clean build).

`asteroid-generate-desktop` requires **two** inputs:

- `<src>/<app>.desktop.template`
- `<src>/i18n/<app>.desktop.h`, containing the default name as `//% "Name"`

It reads the display name out of the header with
`grep -oP '//% "\K[^"]+(?=")'` and `exit 2`s if either input is missing.

Three things make this cost more time than it should:

1. **The failure lands somewhere else.** With no header, no `.desktop` is
   produced, and the build fails later in `do_install` with a bare
   `CMake Error at cmake_install.cmake:61 (file): No such file or directory` —
   no filename, no mention of the generator. Nothing points at the real cause.
2. **The requirement is undocumented** in the app template as far as this
   workspace shows. A new app that ships only a `.desktop.template` looks
   complete and is not.
3. **It prints "aborting" and exits 0** when the `i18n/*.ts` glob matches
   nothing (a new app has no translations yet):
   `Couldn't find a corresponding language id, aborting`. A new contributor
   reasonably reads that as the error, when it is in fact harmless and the
   generator has succeeded.

Suggested upstream: fail loudly at *configure* time with the missing path
named, and downgrade the empty-`.ts` message so it does not read as an abort.

Local fix: add `i18n/benchymark.desktop.h`.

## F4 — the process trap this arc kept falling into

**Confidence: confirmed** (three separate occurrences this session).

`generate_desktop()` runs at CMake **configure** time via `execute_process`.
Adding the missing header therefore does nothing until `do_configure` actually
re-runs — and devtool reuses a cached configure. The rebuild reproduced the
*identical* error, which reads as "the fix was wrong" when the fix had simply
never executed.

This was the third variant of the same trap in one session (stale CMake cache,
a silently-ineffective `cleansstate`, then this). The reliable tell in all
three: **the error cites an unchanged task-log ID.** Checking that first is
cheaper than re-deriving the fix.

Two practices came out of it, both now in the build script:

- clean the recipe (`bitbake -c cleansstate <app>`) before a rebuild that is
  meant to prove a configure-time fix;
- verify a configure-time generator by **running it by hand** against the
  staged source before spending a build cycle on it.

## F5 — devtool build does not produce an installable package

**Confidence: confirmed.**

`devtool build <app>` stops after `do_packagedata`. It reports full success
and leaves `deploy/ipk` empty, which reads as a silent build failure. The ipk
needs an explicit `bitbake -c package_write_ipk <app>`.

Minor, but it cost a cycle, and a-d-b's install op looks for the ipk by path —
so "build succeeded, install says no ipk found" is a confusing pair of
messages that is nobody's bug.

## Outcome

The package builds clean and its payload is correct (verified by unpacking the
ipk, not by inference):

```
./usr/bin/benchymark
./usr/lib/benchymark.so
./usr/share/applications/benchymark.desktop     <- the file that was failing
./usr/share/benchymark/benchy-shader.frag
./usr/share/benchymark/benchy-shader.frag.qsb
./usr/share/icons/asteroid/benchymark.svg
```

with the generated entry carrying `Name=Benchymark` and `Icon=benchymark`.

## Not verified

Installation on nemo has **not** happened: at the end of this arc the unit was
unreachable on both transports (no adb device, SSH to 192.168.2.15 timed out).
So nothing is claimed about the app running, the icon rendering in the
launcher, or the results round-trip. Those remain open.

## Dead ends, recorded

- Chased `cmake_install.cmake:61` as a CMake bug before reading the generator
  script. The line number was accurate and the message was useless; reading
  the *generator's* preconditions was what solved it.
- Assumed the first rebuild had disproved the header fix. It had not run the
  fix at all (F4).

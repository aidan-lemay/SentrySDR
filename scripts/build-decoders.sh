#!/usr/bin/env bash
# Build the third-party decoders SentrySDR supervises into a private prefix.
# Nothing is installed system-wide. Usage: build-decoders.sh [PREFIX]
#
# Each decoder builds independently, so one failure does not block the others.
# The final summary lists what built; the exit code is non-zero if any failed.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX="${1:-$ROOT/.vendor}"
SRC="$PREFIX/src"
JOBS="$(nproc 2>/dev/null || echo 2)"
mkdir -p "$SRC" "$PREFIX/bin" "$PREFIX/lib" "$PREFIX/include" "$PREFIX/share"

export PKG_CONFIG_PATH="$PREFIX/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export CMAKE_PREFIX_PATH="$PREFIX"
export LD_LIBRARY_PATH="$PREFIX/lib:${LD_LIBRARY_PATH:-}"

fetch() { # name url
  if [ -d "$SRC/$1/.git" ]; then
    git -C "$SRC/$1" pull --ff-only --quiet || true
  else
    git clone --depth 1 --quiet "$2" "$SRC/$1"
  fi
}

declare -A RESULT

build_readsb() {
  fetch readsb https://github.com/wiedehopf/readsb.git &&
  make -C "$SRC/readsb" -j"$JOBS" RTLSDR=yes &&
  install -m755 "$SRC/readsb/readsb" "$PREFIX/bin/readsb"
}

build_ais() {
  fetch AIS-catcher https://github.com/jvde-github/AIS-catcher.git &&
  cmake -S "$SRC/AIS-catcher" -B "$SRC/AIS-catcher/build" -DCMAKE_BUILD_TYPE=Release &&
  cmake --build "$SRC/AIS-catcher/build" -j"$JOBS" &&
  install -m755 "$SRC/AIS-catcher/build/AIS-catcher" "$PREFIX/bin/AIS-catcher"
}

build_tar1090() {
  # Static web UI only; served by SentrySDR behind its own authentication.
  fetch tar1090 https://github.com/wiedehopf/tar1090.git &&
  # The aircraft database fills in registrations and types. It's data, not code.
  fetch tar1090-db https://github.com/wiedehopf/tar1090-db.git &&
  rm -rf "$PREFIX/share/tar1090" &&
  cp -r "$SRC/tar1090/html" "$PREFIX/share/tar1090" &&
  cp -r "$SRC/tar1090-db/db" "$PREFIX/share/tar1090/db2"
}

build_dsd() {
  fetch mbelib https://github.com/lwvmobile/mbelib.git &&
  cmake -S "$SRC/mbelib" -B "$SRC/mbelib/build" -DCMAKE_INSTALL_PREFIX="$PREFIX" \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 &&
  cmake --build "$SRC/mbelib/build" -j"$JOBS" --target install &&
  if ! pkg-config --exists itpp; then
    echo "dsd-fme needs IT++: install your distro's package (libitpp-dev on Debian/Raspberry Pi OS)" >&2
    return 1
  fi &&
  fetch dsd-fme https://github.com/lwvmobile/dsd-fme.git &&
  cmake -S "$SRC/dsd-fme" -B "$SRC/dsd-fme/build" -DCMAKE_INSTALL_PREFIX="$PREFIX" \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 &&
  cmake --build "$SRC/dsd-fme/build" -j"$JOBS" &&
  install -m755 "$SRC/dsd-fme/build/dsd-fme" "$PREFIX/bin/dsd-fme"
}

status=0
for name in readsb ais tar1090 dsd; do
  echo "=== building $name ==="
  if ( "build_$name" ); then
    RESULT[$name]=ok
  else
    RESULT[$name]=FAILED
    status=1
  fi
done

echo "=== summary ==="
for name in readsb ais tar1090 dsd; do
  printf '%-8s %s\n' "$name" "${RESULT[$name]}"
done
exit "$status"

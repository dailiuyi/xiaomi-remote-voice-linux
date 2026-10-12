#!/usr/bin/env bash
set -euo pipefail
task_root=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
export RUSTUP_HOME="$task_root/artifacts/receiver-build/rustup"
export CARGO_HOME="$task_root/artifacts/receiver-build/cargo"
export CARGO_TARGET_DIR="$task_root/artifacts/receiver-build/target"
export PATH="$CARGO_HOME/bin:$PATH"
export PKG_CONFIG_SYSROOT_DIR="$task_root/artifacts/receiver-build/sysroot"
export PKG_CONFIG_LIBDIR="$PKG_CONFIG_SYSROOT_DIR/usr/lib/x86_64-linux-gnu/pkgconfig:$PKG_CONFIG_SYSROOT_DIR/usr/share/pkgconfig"
export LIBCLANG_PATH=/usr/lib/x86_64-linux-gnu
export BINDGEN_EXTRA_CLANG_ARGS="-I$(gcc -print-file-name=include)"
cd "$task_root/receiver/atvvoice"
exec cargo "$@"

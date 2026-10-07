#!/bin/sh
#
# mnOS build script
#   rootfs (this directory)  ->  boot/initramfs.cpio.gz  ->  mnOSv<ver>.iso
#
# usage:  ./build.sh
#
# deps:   grub-mkrescue, cpio, gzip, and the kernel's gen_init_cpio.
#         override the latter with GEN_INIT_CPIO=/path/to/gen_init_cpio
#
set -e

ROOT=$(cd "$(dirname "$0")" && pwd)
VERSION=1.3
OUT=${OUT:-$(dirname "$ROOT")/mnOSv${VERSION}.iso}
KERNEL_SRC=${KERNEL_SRC:-$HOME/kernel/linux-6.13.3}
GEN_INIT_CPIO=${GEN_INIT_CPIO:-$KERNEL_SRC/usr/gen_init_cpio}

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

for f in "$ROOT/boot/bzImage"; do
	[ -f "$f" ] || { echo "error: missing $f" >&2; exit 1; }
done
[ -x "$GEN_INIT_CPIO" ] || {
	echo "error: gen_init_cpio not found: $GEN_INIT_CPIO" >&2
	echo "       set GEN_INIT_CPIO=/path/to/gen_init_cpio" >&2
	exit 1
}

echo "==> device nodes (gen_init_cpio, no root needed)"
cat > "$WORK/devspec" <<'SPEC'
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
nod /dev/tty     0666 0 0 c 5 0
nod /dev/tty0    0600 0 0 c 4 0
nod /dev/tty1    0600 0 0 c 4 1
nod /dev/tty2    0600 0 0 c 4 2
nod /dev/tty3    0600 0 0 c 4 3
nod /dev/ttyS0   0666 0 0 c 4 64
nod /dev/null    0666 0 0 c 1 3
nod /dev/zero    0666 0 0 c 1 5
nod /dev/random  0666 0 0 c 1 8
nod /dev/urandom 0666 0 0 c 1 9
nod /dev/ptmx    0666 0 0 c 5 2
nod /dev/kmsg    0644 0 0 c 1 11
SPEC
"$GEN_INIT_CPIO" "$WORK/devspec" > "$WORK/dev.cpio"

echo "==> root filesystem archive"
( cd "$ROOT"
  find . \
	-path ./boot -prune -o \
	-path ./.git -prune -o \
	-path ./assets -prune -o \
	-name build.sh -prune -o \
	-name 'README*' -prune -o \
	-print | cpio --owner 0:0 -H newc -o --quiet ) > "$WORK/tree.cpio"

echo "==> initramfs (device cpio + rootfs, concatenated)"
cat "$WORK/dev.cpio" "$WORK/tree.cpio" | gzip -9 > "$ROOT/boot/initramfs.cpio.gz"

echo "==> squash staging tree"
mkdir -p "$WORK/iso/boot/grub"
cp "$ROOT/boot/bzImage" "$ROOT/boot/initramfs.cpio.gz" "$WORK/iso/boot/"
cat > "$WORK/iso/boot/grub/grub.cfg" <<EOF
insmod all_video
if loadfont unicode; then
    set gfxmode=1280x720
    set gfxpayload=1280x720
    terminal_output gfxterm
fi

set timeout=5
set default=0

menuentry "mnOS v${VERSION}" {
    linux /boot/bzImage video=Virtual-1:1280x720 console=tty0 console=ttyS0,115200
    initrd /boot/initramfs.cpio.gz
}
EOF

echo "==> building ISO"
grub-mkrescue -o "$OUT" "$WORK/iso"

echo "done: $OUT"

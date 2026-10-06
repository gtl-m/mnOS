# mnOS

一个从零构建的最小 Linux 发行版：**Linux 6.13.3 + busybox + GRUB**，完整的 FHS 目录树，打包为可直接启动的 ISO（BIOS + UEFI 双引导），开机会显示一张彩色的系统信息卡片。

```text
   _____   ____        mnOS
  /\  __`\/\  _`\      ---------------------------
 /\ \/\ \ \,\L\_\      OS:      mnOS 1.0 (GNU/Linux)
 \ \_\ \_\ \_____\     Kernel:  6.13.3
  \/_/\/_/\/_____/     Shell:   busybox sh
                       Memory:  31M / 467M

[~]#
```

## 特性

- 完整 FHS 目录结构（`/bin /etc /usr /var /proc /sys ...`）
- busybox 用户空间，400+ applet（sh、vi、top、grep、awk...）
- 开机自动进入 shell（无登录），启动日志结束后**清屏并显示系统信息卡片**
- 彩色提示符 `[\W]#`，以及快捷命令 `off`（关机）`rb`（重启）`ll`（列文件）
- GRUB 菜单与内核 framebuffer 同为 **1280×720（16:9）**
- 串口 / VGA 双控制台输出，`ls`、`mneofetch` 等命令两边都能正常显示
- 静态根文件系统打包，启动快（约 3 秒到 shell）

## 运行

```bash
# 图形模式（GRUB 菜单 + VGA 控制台）
qemu-system-x86_64 -enable-kvm -m 512 -cdrom mnOS.iso

# 串口模式（终端里直接交互）
qemu-system-x86_64 -enable-kvm -m 512 -cdrom mnOS.iso -nographic
```

## 构建

### 1. 内核

```bash
git clone --depth=1 -b v6.13.3 https://github.com/torvalds/linux.git
cd linux
make olddefconfig          # 关键项: BLK_DEV_INITRD, DEVTMPFS, VT, DRM_BOCHS,
                            # FRAMEBUFFER_CONSOLE, SERIAL_8250_CONSOLE, ISO9660_FS
make -j$(nproc)
cp arch/x86/boot/bzImage ../
```

### 2. busybox

```bash
git clone https://git.busybox.net/busybox
cd busybox
make defconfig             # 动态链接（CONFIG_STATIC is not set）
make -j$(nproc)
make CONFIG_PREFIX=../mnOS install
```

根文件系统需要带上 busybox 依赖的动态库（glibc）：

```bash
mkdir -p mnOS/lib/x86_64-linux-gnu
cp -a /lib/x86_64-linux-gnu/{libc,libm,libresolv,libnss_files,libnss_dns}-2.31.so* \
      /lib/x86_64-linux-gnu/ld-2.31.so mnOS/lib/x86_64-linux-gnu/
ln -sf /lib/x86_64-linux-gnu/ld-2.31.so mnOS/lib64/ld-linux-x86-64.so.2
printf '/lib/x86_64-linux-gnu\n/usr/lib/x86_64-linux-gnu\n' > mnOS/etc/ld.so.conf
ldconfig -r mnOS
```

### 3. initramfs（无需 root）

设备节点不用 `mknod`，用内核自带的 `gen_init_cpio` 写进 cpio 头即可：

```bash
cat > devspec <<'EOF'
dir /dev 0755 0 0
nod /dev/console 0600 0 0 c 5 1
nod /dev/tty      0666 0 0 c 5 0
nod /dev/tty0     0600 0 0 c 4 0
nod /dev/tty1     0600 0 0 c 4 1
nod /dev/ttyS0    0666 0 0 c 4 64
nod /dev/null     0666 0 0 c 1 3
nod /dev/zero     0666 0 0 c 1 5
nod /dev/random   0666 0 0 c 1 8
nod /dev/urandom  0666 0 0 c 1 9
nod /dev/ptmx     0666 0 0 c 5 2
EOF
linux/usr/gen_init_cpio devspec > dev.cpio

# 两个归档拼接：设备节点在前，文件系统在后（内核支持拼接 cpio）
cd mnOS
find . -path ./boot -prune -o -print | cpio --owner 0:0 -H newc -o > ../tree.cpio
cd ..
cat dev.cpio tree.cpio | gzip -9 > mnOS/boot/initramfs.cpio.gz
```

### 4. 打包 ISO

```bash
mkdir -p staging/boot/grub
cp mnOS/boot/bzImage        staging/boot/
cp mnOS/boot/initramfs.cpio.gz staging/boot/

cat > staging/boot/grub/grub.cfg <<'EOF'
insmod all_video
if loadfont unicode; then
    set gfxmode=1280x720
    set gfxpayload=1280x720
    terminal_output gfxterm
fi
set timeout=5
set default=0
menuentry "mnOS" {
    linux /boot/bzImage video=Virtual-1:1280x720 console=tty0 console=ttyS0,115200
    initrd /boot/initramfs.cpio.gz
}
EOF

grub-mkrescue -o mnOS.iso staging/
```

## 目录结构

```text
mnOS/
├── init            # PID 1：挂载 proc/sys/devtmpfs/pts 后 exec /sbin/init
├── bin/            # busybox 及 applet 符号链接（含 mneofetch 信息卡片）
├── sbin/           # init、getty 等系统命令
├── etc/
│   ├── inittab     # sysinit + 双控制台 shell
│   ├── init.d/rcS  # 挂载文件系统 → 等待日志静默 → 清屏 → 输出信息卡片
│   ├── profile     # 彩色 PS1、快捷命令 alias
│   └── ...
├── boot/           # bzImage + initramfs.cpio.gz
├── lib/, lib64/    # glibc 运行库与动态链接器
└── proc/ sys/ dev/ ...  # 标准 FHS 空目录（运行时挂载）
```

## 技术要点

| 问题 | 解决方式 |
|------|----------|
| 非 root 无法 `mknod` | 用内核的 `gen_init_cpio` 生成含设备节点的 cpio |
| 提供 `/init` 后内核不再自动挂 devtmpfs | 在 `/init` 里手动挂载 proc/sysfs/devtmpfs |
| `/dev/console` 指向哪个控制台 | 取决于 `console=` 参数顺序，`ttyS0` 放最后 |
| DRM 忽略 GRUB 的 `gfxpayload` 分辨率 | 用 `video=Virtual-1:1280x720` 强制指定 |
| GRUB 主题整段失效 | 剔除 `terminal-*` 字段（会导致主题解析失败） |
| 拼接 cpio 被当作普通文件打进归档 | 分别生成归档后 `cat` 拼接，不要混入 `find` 管道 |

## License

[GPL-3.0](LICENSE)

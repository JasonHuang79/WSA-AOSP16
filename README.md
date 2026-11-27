# WSA-AOSP16

**简体中文** | [English](README_EN.md)

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> 将 **Android 16（AOSP 16 / API 36）** 完整移植至 **Windows Subsystem for Android（WSA 2407）** 的工程化补丁集、镜像打包流水线与技术文档。
>
> 无需从零重编完整 AOSP 源码，通过精准的 ELF/DEX 二进制补丁、模块嫁接、原创 `libhoudini_a16_shim.so` 翻译桥接层与 `debugfs` + VHDX 打包流水线，在 Windows 11 原生独立窗口中稳定运行 AOSP 16、原厂联网、官方 x86_64 Google Play（GApps 16）以及高性能 Intel Houdini 14.0.0（全 ABI `arm64-v8a` + `armeabi-v7a`）。

---

## 核心特性与技术突破

| 模块 | 实现内容 | 解决的核心难题 |
| :--- | :--- | :--- |
| **原生多窗口合成（HWC 2.4 + SF）** | [`patch_hwc_taskid.py`](tools/patches/patch_hwc_taskid.py) + [`patch_services_taskid.py`](tools/patches/patch_services_taskid.py) | 打通 AOSP 16 `SurfaceFlinger` → `hwcomposer.windows.so` 的 `org.chromium.arc.V1_0.TaskId`（`METADATA_TASK_ID = 3`）像素层透传，实现 Windows 11 主桌面原生独立自由窗口渲染与双向焦点同步。 |
| **WSA Shell 与通知桥接** | [`build_systemui_wsa.py`](tools/patches/build_systemui_wsa.py) | 将 `WsaTaskManager`、`Shell` 与 `Notification` RCM 通道嫁接至原生 A16 `SystemUI.apk`（`com/google/protobuf/` 原地重命名隔离为 `com/google/protobuw/`），全量 **5 个 Binder 服务** 与 **25 条 RCM 通道** 在线。 |
| **消除 A16 双标题栏** | [`patch_systemui_nocaption.py`](tools/patches/patch_systemui_nocaption.py) | 禁用 A16 `CaptionWindowDecorViewModel.shouldShowWindowDecor$1`，保留原生 Windows 11 标题栏，彻底消除自由窗口多余的 Android 内层标题栏。 |
| **A16 原生联网与系统兼容** | [`build_services_wsa.py`](tools/patches/build_services_wsa.py) + [`patch_systemapp.py`](tools/patches/patch_systemapp.py) + [`patch_userapp.py`](tools/patches/patch_userapp.py) | 重写纯 A16 `WsaLaunchParamsModifier`；`SystemApp` P5 精准改为 NOP `setNetworkCapabilities`（保留 `EthernetManager.updateConfiguration()`，绕过 A16 `EthernetServiceImpl.enforceAutomotiveDevice()` 校验，恢复 `eth0` 原生 IPv4/DNS 与 `VALIDATED` 联网）；修复 A16 6 参数 `OnOpNotedListener` 与原生桌面 `ACTION_MAIN` 兜底。 |
| **Intel Houdini 14.0.0 A16 兼容桥（推荐）** | [`houdini_a16_shim.c`](tools/nativebridge/houdini_a16_shim.c) + [`pack_houdini_shim.sh`](tools/pack/pack_houdini_shim.sh) | 原创 11 KB 纯 C **NativeBridge v8 → v6 兼容层**（零改动 `libart.so` / `libhoudini.so`）：① 补齐 A16 ART `getTrampolineForFunctionPointer`（v7）与 `isNativeBridgeFunctionPointer`（v8），根治 `CHECK(native_method != nullptr)` `SIGABRT`；② 运行时动态解析 A16 ThinLTO `linker64` 中被加上 `.llvm.<hash>` 后缀的 `__dl__ZL6solist` 与 `__dl__ZL12g_cfi_shadow`，根治 guest `mprotect()` `SIGSEGV`。完整保留原厂 Houdini 14.0.0 高性能与 64/32 位 ARM（`arm64-v8a` + `armeabi-v7a`）全兼容。 |
| **可选：原生 A16 Berberis 翻译层** | [`pack_ndk_translation.sh`](tools/pack/pack_ndk_translation.sh) | 作为备选方案，也可从官方 Android 16 SDK `system.img` 注入原生 `libndk_translation.so`（Berberis，仅 64 位 `arm64-v8a`）。 |
| **官方 A16 x86_64 GApps** | [`pack_gapps16.sh`](tools/pack/pack_gapps16.sh) | 直接从官方 Android 16 SDK `google_apis_playstore/x86_64/system.img` 提取原生 x86_64 API 36 GApps 7 件套（`GmsCore`、`Phonesky`、`GSF` 等）与完整权限/sysconfig/default-permissions。 |
| **`fuse-bpf` 跨代内核修复** | [`post_boot_fixes.ps1`](tools/deploy/post_boot_fixes.ps1) | 通过持久化属性 `persist.sys.fuse.bpf.override=false` 解决 WSA A13 内核与 A16 `vold`/`FuseDaemon` 冲突，恢复所有应用对自有 `/Android/data` 与 `/Android/obb` 的正常读写。 |
| **开始菜单直达原生桌面** | [`create_desktop_shortcut.ps1`](tools/deploy/create_desktop_shortcut.ps1) | 配合 `UserApp` `ACTION_MAIN` 补丁与禁用 `SecondaryDisplayLauncher`，在 Windows 开始菜单一键直达 AOSP 16 原生 Launcher3 桌面。 |
| **零拷贝 VHDX 工具集** | [`tools/vhdx/`](tools/vhdx/) | 纯 Python 实现动态/固定 VHDX 解包（`vhdx_extract.py`）、16 MiB 块重打包与反解 MD5 校验（`raw2vhdx.py`），以及支持动态 VHDX `chunk_ratio` 的零拷贝 FUSE3 只读挂载器（`vhdx_fuse_ro.py`）。 |

---

## 仓库目录结构

```text
WSA-AOSP16/
├── LICENSE                                  # MIT 开源协议
├── README.md                                # 简体中文说明（当前文档）
├── README_EN.md                             # English README
├── docs/
│   └── ARCHITECTURE.md                      # 架构解析、二进制/DEX 补丁偏移与验证指标
└── tools/
    ├── vhdx/
    │   ├── vhdx_extract.py                  # VHDX -> raw ext4 只读解包器
    │   ├── raw2vhdx.py                      # raw ext4 -> 动态 VHDX（含 CRC32C 与反解 MD5 校验）
    │   └── vhdx_fuse_ro.py                  # 纯 Python FUSE3 零拷贝 VHDX 只读挂载器
    ├── patches/
    │   ├── patch_hwc_taskid.py              # hwcomposer.windows.so ELF 补丁（TaskId 透传）
    │   ├── build_services_wsa.py            # A16 services.jar + WsaLaunchParamsModifier 构建
    │   ├── patch_services_taskid.py         # WindowStateAnimator.createSurfaceLocked TaskId 补丁
    │   ├── build_systemui_wsa.py            # A16 SystemUI.apk 嫁接 WsaTaskManager/Shell/Notification
    │   ├── patch_systemui_nocaption.py      # 禁用 A16 自由窗口内层双标题栏
    │   ├── patch_systemapp.py               # SystemApp.apk A16 兼容补丁（P1–P6）
    │   └── patch_userapp.py                 # UserApp.apk 权限修复 + 原生桌面 ACTION_MAIN 兜底
    ├── nativebridge/
    │   └── houdini_a16_shim.c               # 原创 A16 NativeBridge v8->v6 + ThinLTO linker64 兼容层
    ├── pack/
    │   ├── pack_gapps16.sh                  # 从官方 Android 16 SDK 镜像注入 x86_64 GApps 16
    │   ├── pack_houdini_shim.sh             # 注入 libhoudini_a16_shim.so（保留原厂 Intel Houdini 14.0.0）
    │   └── pack_ndk_translation.sh          # 可选：注入 Berberis / libndk_translation（纯 64 位）
    └── deploy/
        ├── deploy_wsa_images.ps1            # 安全部署 system.vhdx / vendor.vhdx（自动备份 + MD5 校验）
        ├── post_boot_fixes.ps1              # 首次开机后应用 fuse-bpf 修复与禁用 SecondaryDisplayLauncher
        └── create_desktop_shortcut.ps1      # 在 Windows 开始菜单创建原生 Android 桌面快捷方式
```

---

## 环境要求

- **Windows 11（x64）**，已安装 WSA `2407.40000.4.0`（x64）
- **WSL2（Ubuntu / Debian）**，安装 `e2fsprogs`（`debugfs`、`tune2fs`、`e2fsck`）、`python3`、`libfuse3`（可选，用于 `vhdx_fuse_ro.py`）
- **JDK 17+** 与 **Android SDK**（`build-tools 36.x`、`platforms/android-36/android.jar`）
- **官方 Android 16 x86_64 系统镜像**：通过 Android Studio SDK Manager 下载 `system-images/android-36/google_apis_playstore/x86_64/system.img`
- **AOSP 平台测试签名密钥**：与目标 AOSP 16 `framework-res.apk` 匹配的 `platform.pk8` 与 `platform.x509.pem`（可从 AOSP `build/target/product/security/` 获取）

---

## 快速复现流程

详细的底层原理、偏移表与校验命令请参见 [**技术架构文档（docs/ARCHITECTURE.md）**](docs/ARCHITECTURE.md)。

### 1. 构建组件补丁

从原厂 WSA 2407 镜像及目标 AOSP 16 系统镜像提取原始 `hwcomposer.windows.so`、`services.jar`、`SystemUI.apk`、`SystemApp.apk`、`UserApp.apk` 后，按顺序执行：

```bash
# 1) 修补 vendor hwcomposer.windows.so（HWC 2.4 TaskId 透传）
python3 tools/patches/patch_hwc_taskid.py \
  --input hwcomposer.windows.so \
  --output hwcomposer.windows.patched.so

# 2) 构建并修补 A16 services.jar（WsaLaunchParamsModifier + METADATA_TASK_ID）
python3 tools/patches/build_services_wsa.py \
  --a16-services a16_services.jar \
  --wsa-services-dex wsa-services.dex \
  --output services.step1.jar \
  --build-tools "$ANDROID_SDK/build-tools/36.1.0" \
  --android-jar "$ANDROID_SDK/platforms/android-36/android.jar"

python3 tools/patches/patch_services_taskid.py \
  --input services.step1.jar \
  --output services.patched.jar \
  --dexdump "$ANDROID_SDK/build-tools/36.1.0/dexdump"

# 3) 构建并修补 A16 SystemUI.apk（嫁接 WSA Shell/Notification + 消除双标题栏）
python3 tools/patches/build_systemui_wsa.py \
  --a16-systemui a16_SystemUI.apk \
  --a13-c24-dex sysui_c24.dex \
  --output SystemUI.step1.apk \
  --build-tools "$ANDROID_SDK/build-tools/36.1.0" \
  --android-jar "$ANDROID_SDK/platforms/android-36/android.jar" \
  --key platform.pk8 --cert platform.x509.pem

python3 tools/patches/patch_systemui_nocaption.py \
  --input SystemUI.step1.apk \
  --output SystemUI.patched.apk \
  --build-tools "$ANDROID_SDK/build-tools/36.1.0" \
  --key platform.pk8 --cert platform.x509.pem

# 4) 修补 SystemApp.apk 与 UserApp.apk
python3 tools/patches/patch_systemapp.py \
  --input SystemApp.apk --output SystemApp.patched.apk \
  --build-tools "$ANDROID_SDK/build-tools/36.1.0" \
  --key platform.pk8 --cert platform.x509.pem

python3 tools/patches/patch_userapp.py \
  --input UserApp.apk --output UserApp.patched.apk \
  --build-tools "$ANDROID_SDK/build-tools/36.1.0" \
  --key platform.pk8 --cert platform.x509.pem
```

### 2. 编译 `libhoudini_a16_shim.so` 并打包镜像（推荐方案）

使用 Android NDK（r27+）交叉编译 64 位与 32 位兼容桥（纯 C，无外部依赖，体积 ~11 KB）：

```bash
"$ANDROID_NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/clang" \
  --target=x86_64-linux-android35 -shared -fPIC -O2 -Wall -Wextra -fvisibility=hidden \
  -Wl,-soname,libhoudini_a16_shim.so -o libhoudini_a16_shim_64.so \
  tools/nativebridge/houdini_a16_shim.c -ldl -llog

"$ANDROID_NDK/toolchains/llvm/prebuilt/linux-x86_64/bin/clang" \
  --target=i686-linux-android35 -shared -fPIC -O2 -Wall -Wextra -fvisibility=hidden \
  -Wl,-soname,libhoudini_a16_shim.so -o libhoudini_a16_shim_32.so \
  tools/nativebridge/houdini_a16_shim.c -ldl -llog
```

随后在 WSL2 中注入官方 GApps 16 与 `libhoudini_a16_shim.so`（如需改用 Berberis，可执行 `pack_ndk_translation.sh`）：

```bash
# 1) 注入官方 Android 16 x86_64 GApps 7 件套 + 权限配置
sudo bash tools/pack/pack_gapps16.sh \
  --in-system system-aosp16-base.vhdx \
  --sdk-image "/mnt/c/.../Android/Sdk/system-images/android-36/google_apis_playstore/x86_64/system.img" \
  --out-system system-aosp16-gapps.vhdx \
  --userapp UserApp.patched.apk

# 2) 注入 Intel Houdini 14.0.0 NativeBridge v8 Shim + SystemApp 联网修复补丁（推荐）
sudo bash tools/pack/pack_houdini_shim.sh \
  --in-system system-aosp16-gapps.vhdx \
  --in-vendor vendor-hwc-patched.vhdx \
  --shim64 libhoudini_a16_shim_64.so \
  --shim32 libhoudini_a16_shim_32.so \
  --systemapp SystemApp.patched.apk \
  --out-system system-aosp16-r43.vhdx \
  --out-vendor vendor-aosp16-r43.vhdx
```

### 3. 部署、首次开机修复与开始菜单快捷方式（Windows PowerShell）

```powershell
# 1) 安全部署（自动备份原镜像并校验 MD5）
.\tools\deploy\deploy_wsa_images.ps1 `
  -WsaDir "D:\Path\To\WSA" `
  -SystemVhdx ".\system-aosp16-ndk.vhdx" `
  -VendorVhdx ".\vendor-aosp16-ndk.vhdx"

# 2) 启动 WSA 后执行一次 fuse-bpf 修复与桌面直达配置（随后重启 WSA 生效）
.\tools\deploy\post_boot_fixes.ps1 -Serial "127.0.0.1:58526"

# 3) 可选：在 Windows 开始菜单创建原生 Android 桌面快捷方式
.\tools\deploy\create_desktop_shortcut.ps1
```

---

## 实机验收摘要

- **冷启动速度**：约 `18s – 26s` 到达 `sys.boot_completed=1`，零 `HeartbeatTimeout`、零 `FATAL EXCEPTION` / `Fatal signal`
- **WSA 集成完整度**：5/5 WSA Binder 服务在线、25/25 RCM 通道建链（含 `Composition`、`Shell`、`Notification`、`TSF` 中文输入法、`Clipboard`、`Audio` 等）
- **窗口与输入体验**：Windows 11 主桌面原生独立自由窗口，单标题栏，支持 Windows 宿主中文输入法（TSF）与开始菜单一键直达原生桌面
- **原生网络与网页**：`eth0` 开机自动下发 IPv4/网关/DNS，`ConnectivityService` 1 秒内 `VALIDATED`，`WebView` 网页浏览与 Google Play 联网正常
- **应用生态与兼容**：官方 A16 x86_64 Google Play 服务就绪；Intel Houdini 14.0.0 经 `libhoudini_a16_shim.so` 桥接后完整支持 `arm64-v8a` 与 `armeabi-v7a`，大型 Unity ARM64 游戏（含腾讯 `libtersafe2.so`/`libtprt.so` 与数美 `libsmsdk.so` 反作弊组件）稳定运行，`0 SIGABRT` / `0 SIGSEGV`；`Android/data` 与 `Android/obb` 权限正常（`FuseDaemon: Rejected = 0`）

---

## 已知限制与注意事项

1. **正确启动方式**：请始终通过 Windows 开始菜单快捷方式或 MSIX `AppExecutionAlias`（`WsaClient.exe /launch wsa://<package>`）启动；勿直接双击安装目录内 `WsaClient\WsaClient.exe`（缺少 MSIX 依赖上下文）。
2. **ARM 翻译层选择**：默认推荐 **`libhoudini_a16_shim.so` + 原厂 Intel Houdini 14.0.0**（同时支持 `arm64-v8a` 与 `armeabi-v7a`，性能与反作弊兼容性最佳）；`pack_ndk_translation.sh`（Berberis）仅保留作为可选纯 64 位替代方案。
3. **补丁字节偏移绑定**：`tools/patches/` 中的绝对偏移均带有严格的字节前置断言（`assert`），针对 WSA `2407.40000.4.0`（x64）与 AOSP 16 `BP2A.250605.031.A2` 校验；若用于其他版本，需重新核对对应符号偏移。
4. **中文输入法提示**：请在 Android 系统设置中启用 WSA 宿主键盘/输入法通道（`TSF` RCM 通道），即可直接复用 Windows 11 原生中文输入法。

---

## 免责声明与开源协议

- 本仓库**仅包含原创构建脚本、补丁生成代码与技术文档**，**不包含也不分发**任何专有或第三方闭源二进制文件（如 Microsoft WSA 镜像/APK、Google GApps/Play 服务 APK、签名私钥等）。使用者需自行合法获取基础镜像与 Android SDK 资源。
- 本项目基于 [**MIT License**](LICENSE) 开源。

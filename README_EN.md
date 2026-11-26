# WSA-AOSP16

[简体中文](README.md) | **English**

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

> Engineering patchset, image packing pipeline, and technical documentation for porting **Android 16 (AOSP 16 / API 36)** onto **Windows Subsystem for Android (WSA 2407)**.
>
> Without rebuilding AOSP from scratch, this toolkit uses verified byte-level ELF/DEX patches, targeted module grafting, and a `debugfs` + VHDX packing pipeline to run AOSP 16 with native Windows 11 independent app windows, official x86_64 Google Play Services (GApps 16), and Android 16 native `libndk_translation` (Berberis) ARM64 binary translation.

---

## Key Highlights & Breakthroughs

| Area | Tools | Problem Solved |
| :--- | :--- | :--- |
| **Native Multi-Window Composition (HWC 2.4 + SF)** | [`patch_hwc_taskid.py`](tools/patches/patch_hwc_taskid.py) + [`patch_services_taskid.py`](tools/patches/patch_services_taskid.py) | Bridges AOSP 16 `SurfaceFlinger` → `hwcomposer.windows.so` via HWC 2.4 GenericMetadata `"org.chromium.arc.V1_0.TaskId"` (`METADATA_TASK_ID = 3`), restoring native Windows 11 independent freeform windows and bidirectional focus sync. |
| **WSA Shell & Notification Bridge** | [`build_systemui_wsa.py`](tools/patches/build_systemui_wsa.py) | Grafts `WsaTaskManager`, `Shell`, and `Notification` RCM channels onto stock A16 `SystemUI.apk` (isolating bundled `com/google/protobuf/` in-place to `com/google/protobuw/`), bringing **5/5 Binder services** and **25/25 RCM channels** online. |
| **Duplicate Caption Bar Removal** | [`patch_systemui_nocaption.py`](tools/patches/patch_systemui_nocaption.py) | Patches `CaptionWindowDecorViewModel.shouldShowWindowDecor$1` to return `false`, eliminating the redundant AOSP 16 inner freeform caption bar beneath the native Windows 11 title bar. |
| **A16 Freeform Layout & System Compatibility** | [`build_services_wsa.py`](tools/patches/build_services_wsa.py) + [`patch_systemapp.py`](tools/patches/patch_systemapp.py) + [`patch_userapp.py`](tools/patches/patch_userapp.py) | Clean-room A16 `WsaLaunchParamsModifier`; `SystemApp` P1–P6 NOPs (fixing A16's 6-arg `OnOpNotedListener`); `UserApp` cache-cleanup fix + `ACTION_MAIN` fallback for direct desktop launching. |
| **Official A16 x86_64 GApps** | [`pack_gapps16.sh`](tools/pack/pack_gapps16.sh) | Extracts native x86_64 API 36 Google priv-apps (7 packages incl. `PrebuiltGmsCore`, `Phonesky`, `GoogleServicesFramework`) plus `permissions`/`sysconfig`/`default-permissions` from the official Android 16 SDK `google_apis_playstore` image. |
| **Native A16 ARM64 Translation (Berberis)** | [`pack_ndk_translation.sh`](tools/pack/pack_ndk_translation.sh) | Replaces cross-generation A13 Intel Houdini with Android 16's native `libndk_translation.so` / Berberis + `binfmt_misc`, eliminating the `ClassLinker::RegisterNative` `CHECK(native_method != nullptr)` `SIGABRT` crash on arm64-v8a apps. |
| **Cross-Gen `fuse-bpf` Kernel Fix** | [`post_boot_fixes.ps1`](tools/deploy/post_boot_fixes.ps1) | Sets `persist.sys.fuse.bpf.override=false` so A16 `vold` bind-mounts `/Android/data` and `/Android/obb` cleanly on WSA's A13 kernel, fixing `FuseDaemon: Rejected` errors across all apps. |
| **Start Menu Native Android Desktop Shortcut** | [`create_desktop_shortcut.ps1`](tools/deploy/create_desktop_shortcut.ps1) | Combined with the `UserApp` `ACTION_MAIN` fallback and disabling `SecondaryDisplayLauncher`, launches AOSP 16 Launcher3 directly from the Windows Start Menu. |
| **Zero-Copy VHDX Toolkit** | [`tools/vhdx/`](tools/vhdx/) | Pure-Python dynamic/fixed VHDX extractor (`vhdx_extract.py`), 16 MiB block packer with CRC32C & readback MD5 verification (`raw2vhdx.py`), and a zero-copy FUSE3 read-only VHDX mounter supporting dynamic BAT `chunk_ratio` (`vhdx_fuse_ro.py`). |

---

## Repository Structure

```text
WSA-AOSP16/
├── LICENSE                                  # MIT License
├── README.md                                # Simplified Chinese README
├── README_EN.md                             # English README (this file)
├── docs/
│   └── ARCHITECTURE.md                      # Deep-dive architecture, binary/DEX offsets & verification
└── tools/
    ├── vhdx/
    │   ├── vhdx_extract.py                  # Read-only VHDX -> raw ext4 extractor
    │   ├── raw2vhdx.py                      # raw ext4 -> dynamic VHDX packer with MD5 verify
    │   └── vhdx_fuse_ro.py                  # Pure-Python FUSE3 zero-copy read-only VHDX mounter
    ├── patches/
    │   ├── patch_hwc_taskid.py              # hwcomposer.windows.so ELF patch (TaskId passthrough)
    │   ├── build_services_wsa.py            # A16 services.jar + WsaLaunchParamsModifier builder
    │   ├── patch_services_taskid.py         # WindowStateAnimator.createSurfaceLocked TaskId patch
    │   ├── build_systemui_wsa.py            # A16 SystemUI.apk WSA Shell/Notification grafter
    │   ├── patch_systemui_nocaption.py      # Disable duplicate A16 freeform caption bar
    │   ├── patch_systemapp.py               # SystemApp.apk A16 compatibility patches (P1-P6)
    │   └── patch_userapp.py                 # UserApp.apk permission fix + ACTION_MAIN fallback
    ├── pack/
    │   ├── pack_gapps16.sh                  # Inject official Android 16 x86_64 GApps into system.vhdx
    │   └── pack_ndk_translation.sh          # Inject official Android 16 Berberis / libndk_translation
    └── deploy/
        ├── deploy_wsa_images.ps1            # Deploy system.vhdx / vendor.vhdx with backup & MD5 check
        ├── post_boot_fixes.ps1              # Apply fuse-bpf override & disable SecondaryDisplayLauncher
        └── create_desktop_shortcut.ps1      # Create Windows Start Menu shortcut to native desktop
```

---

## Prerequisites

- **Windows 11 (x64)** with WSA `2407.40000.4.0` (x64) installed
- **WSL2 (Ubuntu / Debian)** with `e2fsprogs` (`debugfs`, `tune2fs`, `e2fsck`), `python3`, and optionally `libfuse3` (for `vhdx_fuse_ro.py`)
- **JDK 17+** and **Android SDK** (`build-tools 36.x`, `platforms/android-36/android.jar`)
- **Official Android 16 x86_64 system image** from Android SDK Manager (`system-images/android-36/google_apis_playstore/x86_64/system.img`)
- **AOSP platform signing key** (`platform.pk8` & `platform.x509.pem` matching the target AOSP 16 `framework-res.apk`, available in AOSP `build/target/product/security/`)

---

## Quick Start

For full technical details, byte offsets, and verification steps, see [**docs/ARCHITECTURE.md**](docs/ARCHITECTURE.md).

### 1. Build Component Patches

Extract stock `hwcomposer.windows.so`, `services.jar`, `SystemUI.apk`, `SystemApp.apk`, and `UserApp.apk` from your local WSA 2407 and AOSP 16 images, then run:

```bash
# 1) Patch vendor hwcomposer.windows.so (HWC 2.4 TaskId passthrough)
python3 tools/patches/patch_hwc_taskid.py \
  --input hwcomposer.windows.so \
  --output hwcomposer.windows.patched.so

# 2) Build & patch A16 services.jar (WsaLaunchParamsModifier + METADATA_TASK_ID)
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

# 3) Build & patch A16 SystemUI.apk (WSA Shell/Notification bridge + remove double caption bar)
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

# 4) Patch SystemApp.apk & UserApp.apk
python3 tools/patches/patch_systemapp.py \
  --input SystemApp.apk --output SystemApp.patched.apk \
  --build-tools "$ANDROID_SDK/build-tools/36.1.0" \
  --key platform.pk8 --cert platform.x509.pem

python3 tools/patches/patch_userapp.py \
  --input UserApp.apk --output UserApp.patched.apk \
  --build-tools "$ANDROID_SDK/build-tools/36.1.0" \
  --key platform.pk8 --cert platform.x509.pem
```

### 2. Pack Official GApps 16 & Native ARM64 Translation (WSL2)

After injecting the patched system/vendor components into your base AOSP 16 images:

```bash
# Inject official Android 16 x86_64 GApps (7 packages + permissions/sysconfig)
sudo bash tools/pack/pack_gapps16.sh \
  --in-system system-aosp16-base.vhdx \
  --sdk-image "/mnt/c/.../Android/Sdk/system-images/android-36/google_apis_playstore/x86_64/system.img" \
  --out-system system-aosp16-gapps.vhdx \
  --userapp UserApp.patched.apk

# Replace A13 Houdini with official Android 16 libndk_translation (Berberis)
sudo bash tools/pack/pack_ndk_translation.sh \
  --in-system system-aosp16-gapps.vhdx \
  --in-vendor vendor-hwc-patched.vhdx \
  --sdk-image "/mnt/c/.../Android/Sdk/system-images/android-36/google_apis_playstore/x86_64/system.img" \
  --out-system system-aosp16-ndk.vhdx \
  --out-vendor vendor-aosp16-ndk.vhdx
```

### 3. Deploy & Apply Post-Boot Fixes (Windows PowerShell)

```powershell
# 1) Deploy with automatic backup & MD5 verification
.\tools\deploy\deploy_wsa_images.ps1 `
  -WsaDir "D:\Path\To\WSA" `
  -SystemVhdx ".\system-aosp16-ndk.vhdx" `
  -VendorVhdx ".\vendor-aosp16-ndk.vhdx"

# 2) After booting WSA, apply fuse-bpf override & disable SecondaryDisplayLauncher (reboot WSA once)
.\tools\deploy\post_boot_fixes.ps1 -Serial "127.0.0.1:58526"

# 3) Optional: create a Start Menu shortcut to the native Android desktop
.\tools\deploy\create_desktop_shortcut.ps1 -ShortcutName "WSA Android Desktop"
```

---

## Verification Summary

- **Cold boot**: ~`18s – 26s` to `sys.boot_completed=1`, zero `HeartbeatTimeout`, zero `FATAL EXCEPTION` / `Fatal signal`
- **WSA host integration**: 5/5 Binder services online, 25/25 RCM channels connected (`Composition`, `Shell`, `Notification`, `TSF` IME, `Clipboard`, `Audio`, etc.)
- **Windowing & IME**: Native Windows 11 independent freeform windows with single title bar, native host IME (TSF), and one-click Start Menu desktop launcher
- **App ecosystem**: Official API 36 x86_64 Google Play Services ready; 64-bit `arm64-v8a` apps run stably on Berberis (`0 SIGABRT`); full `/Android/data` & `/Android/obb` access (`FuseDaemon: Rejected = 0`)

---

## Known Limitations & Notes

1. **Launch Entry Point**: Always launch WSA via Start Menu shortcuts or the MSIX `AppExecutionAlias` (`WsaClient.exe /launch wsa://<package>`), never by double-clicking `WsaClient\WsaClient.exe` directly inside the installation directory.
2. **ARM ABI Scope**: `pack_ndk_translation.sh` configures 64-bit **`arm64-v8a`** translation only (`x86_64,x86,arm64-v8a`); 32-bit `armeabi-v7a` translation is not included.
3. **Target Build Offsets**: Byte offsets in `tools/patches/` include strict pre-patch assertions verified against WSA `2407.40000.4.0` (x64) and AOSP 16 `BP2A.250605.031.A2`. Other builds require updating offsets accordingly.

---

## Disclaimer & License

- This repository **only contains original build scripts, patch generators, and technical documentation**. It **does not contain or distribute** any proprietary or third-party binaries (such as Microsoft WSA images/APKs, Google GApps APKs, or signing keys). Users must obtain base images and Android SDK components legally on their own.
- Licensed under the [**MIT License**](LICENSE).

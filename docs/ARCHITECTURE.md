# WSA-AOSP16 技术架构与补丁原理（Architecture & Patch Notes）

> 目标基线：WSA `2407.40000.4.0`（x64 / Android 13 宿主机框架与内核）+ AOSP 16（`BP2A.250605.031.A2`，SDK 36，x86_64）

---

## 1. 渲染与独立窗口合成链路（HWC 2.4 + SurfaceFlinger + WindowManager）

WSA 在 Windows 11 上为每个 Android 应用渲染原生独立桌面窗口，核心依赖 `WSAHwc`（`hwcomposer.windows.so`）通过每个像素图层的 `Layer::mTaskId` 将帧分发到宿主机 `Composition` RCM 通道对应的窗口。

在升级至 AOSP 16 后，原厂链路存在两处断裂：

1. **HWC 2.4 GenericMetadata Key 变更与探测门控**
   - AOSP 16 上游 `SurfaceFlinger` 已内置 ARC++ 的 `METADATA_TASK_ID` 透传，但发送的 HWC 2.4 key 为 `"org.chromium.arc.V1_0.TaskId"`（28 字节），且发送前会先调用 `getLayerGenericMetadataKey()` 探测 HWC 声明支持的 key 列表。
   - 原厂 WSA `hwcomposer.windows.so` 在 `Device::getFunction(70)` 返回空列表（0 个 key），且内部只匹配 `"com.microsoft.wsa.taskid"` / `"com.microsoft.wsa.windowtype"`。结果导致所有图层的 `mTaskId` 保持为 `0` 并被 `Layer::validate()` 丢弃。
   - **修复方案（`tools/patches/patch_hwc_taskid.py`）**：
     - `.rodata @ 0xdeaa`：将 28 字节的 `"com.microsoft.wsa.windowtype"` 原地替换为等长的 `"org.chromium.arc.V1_0.TaskId"`；
     - `Layer::setLayerGenericMetadata @ 0x2954b` & `0x2955c`：将 28 字节匹配分支写入目标偏移从 `+0x90`（`mWindowType`）改为 `+0x8c`（`mTaskId`）；
     - `@ 0x29700`（复用未使用的 `Layer::isValidWindowType` 48 字节空间）：手写 `HWC2_PFN_GET_LAYER_GENERIC_METADATA_KEY` x86_64 机器码，对 `keyIndex == 0` 返回 `"org.chromium.arc.V1_0.TaskId"`；
     - `Device::getFunction(70) @ 0x195b6`：将返回函数指针重定向至 `0x29700`。

2. **AOSP 16 `SurfaceFlinger` 不向下继承容器 `METADATA_TASK_ID`**
   - AOSP 16 仅在父容器 `Task` 上挂载 `METADATA_TASK_ID`，不再自动继承到子像素 `WindowSurfaceController` 层。
   - **修复方案（`tools/patches/patch_services_taskid.py`）**：
     - 在 `services.jar` `classes3.dex` 的 `WindowStateAnimator.createSurfaceLocked @ 0x156340` 原地替换 30 字节 Dalvik 字节码：调用 `WindowState.getTask()` 读取 `Task.mTaskId`，并通过 `SurfaceControl.Builder.setMetadata(3 /* METADATA_TASK_ID */, taskId)` 直接将 `taskId` 写入每个像素层。

---

## 2. SystemUI 嫁接与双标题栏消除

1. **WSA Shell / Notification 嫁接（`tools/patches/build_systemui_wsa.py`）**
   - 以原生 A16 `SystemUI.apk` 为基底：
     - `classes.dex`：仅移除空壳类 `PhoneSystemUIAppComponentFactory`；
     - `classes2.dex`：保留原生 A16 SystemUI 核心代码；
     - `classes3.dex`：从原厂 WSA `SystemUIApp.apk` 的 `classes24.dex` 裁剪出 `WsaTaskManager*`、`ShellRedirectionHandler`、`NotificationRedirectionHandler`、`windows.services.*` 及 protobuf 依赖，将所有 `com/google/protobuf/` 等长原地重命名为 `com/google/protobuw/` 实现物理零冲突隔离，同时对 `WsaTaskManager.<init>` 中的 `Timer.schedule(CacheCleanupTimerTask) @ 0x2553fa` 施加 6B NOP（消除 A16 每 30 秒触发的 `KILL_ALL_BACKGROUND_PROCESSES` `SecurityException`）；
     - `classes4.dex`：编译注入 `PhoneSystemUIAppComponentFactory` 钩子、`WsaShellBridge`、`WsaTaskListenerProxy`（适配 A16 `reparentChildSurfaceToTask(int, Transaction, SurfaceControl)` 与 A13 参数顺序反转）与 `WsaPipController` stub。
2. **消除自由窗口双标题栏（`tools/patches/patch_systemui_nocaption.py`）**
   - AOSP 16 为 freeform 窗口默认启用了 `CaptionWindowDecorViewModel`，会在 Windows 原生标题栏下方再画一层 Android 标题栏。
   - 在 `SystemUI.apk` `classes2.dex @ 0x573ca4` 将 `CaptionWindowDecorViewModel.shouldShowWindowDecor$1` 入口改为 `const/4 v0, 0; return v0`（`12 00 0f 00`），彻底恢复干净单标题栏。

---

## 3. SystemApp / UserApp / services.jar 适配

- **`services.jar`（`tools/patches/build_services_wsa.py`）**：剔除跨代不兼容的 A13 `WsaLaunchParamsModifier*`，编译注入纯 A16 `WsaLaunchParamsModifier`（标准应用默认 `WINDOWING_MODE_FREEFORM = 5` 与居中初始窗口边界）。
- **`SystemApp.apk`（`tools/patches/patch_systemapp.py`）**：
  - 对 `classes.dex` 施加 P1–P6 补丁，重点包括：
    - **P5 `@ 0x09ef98`（8B NOP，原生联网修复）**：AOSP 16 `EthernetServiceImpl.updateConfiguration()` 仅在 `request.getNetworkCapabilities() != null` 时调用 `enforceAutomotiveDevice()` 抛异常；若 `capabilities == null`，则只校验常规网络管理权限（`SystemApp` 已持有）。早期粗暴 NOP 掉整个 `EthernetManager.updateConfiguration()` 会导致 `eth0` 无 IPv4 断网；改为仅 NOP `setNetworkCapabilities` + `move-result-object v8`（8 字节 `@ 0x09ef98`）后，`eth0` 立即正常获得宿主机分配的静态 IPv4、网关与 DNS，`ConnectivityService` 1 秒内进入 `VALIDATED`。
    - **P6 `@ 0x0df780`（6B NOP）**：消除 A16 `AppOpsManager.OnOpNotedListener.onOpNoted` 签名扩展至 6 参数导致的 `AbstractMethodError`。
- **`UserApp.apk`（`tools/patches/patch_userapp.py`）**：
  - `classes3.dex @ 0x1a4240`：将 `persist.sys.disable_cache_cleanup` 默认值从 `false` 改为 `true`，避免 `/launch` 时 `killAllBackgroundProcesses` 权限异常；
  - `classes3.dex @ 0x1d7224`：在 `IntentRedirectionHandler.handleLaunchPackage` 中为无 `CATEGORY_LAUNCHER` 的应用（如 `com.android.launcher3`）补齐 `new Intent(Intent.ACTION_MAIN).setPackage(pkg)` 兜底，使 Windows 开始菜单快捷方式可直接唤醒原生 Android 桌面。

---

## 4. 官方 A16 x86_64 GApps 与 Intel Houdini 14.0.0 A16 兼容桥

1. **GApps 16（`tools/pack/pack_gapps16.sh`）**
   - 从官方 Android 16 SDK `google_apis_playstore/x86_64/system.img` 的 `super` 分区只读挂载 `product` 与 `system_ext` EROFS，仅提取 7 个原生 x86_64 API 36 特权 APK（跳过与目标 `bootclasspath` 不匹配的模拟器 `oat/` 预编译产物）及完整 `permissions`、`sysconfig`、`default-permissions`。
2. **Intel Houdini 14.0.0 原生兼容桥 `libhoudini_a16_shim.so`（推荐，`tools/nativebridge/houdini_a16_shim.c`）**
   - 经深度逆向证实，原厂 WSA 的 Intel Houdini `14.0.0_z.GoogleGame_com1.3 RELEASE` 在 AOSP 16 上存在**两处精确跨代不兼容**，均可在**零改动 `com.android.art.capex` / `libart.so` / `libhoudini.so`**的前提下由一个 ~11 KB 纯 C Shim 彻底解决：
     1. **NativeBridge v6 → v8 ABI 缺口（`RegisterNatives` `SIGABRT`）**：
        - AOSP 16 `art::JNI::RegisterNatives` 对所有 native-bridge 进程无条件调用 v7 `NativeBridgeGetTrampolineForFunctionPointer`（槽位 `+0x98`）并探测 v8 `NativeBridgeIsNativeBridgeFunctionPointer`（槽位 `+0xa0`）。
        - Houdini 14.0.0 仅导出 v6 `NativeBridgeItf`，导致 `libnativebridge.so` 报错 `not compatible with version 7` 并返回 `nullptr`，覆盖掉 Houdini 自己早已在 `JNIEnv->RegisterNatives` 代理中包装好的合法 x86_64 host trampoline 指针，进而触发 `ClassLinker::RegisterNative` 的 `CHECK(native_method != nullptr)` 崩溃。
        - **解法**：`libhoudini_a16_shim.so` 导出 v8 `NativeBridgeItf`（`0xa8` 字节），透传 v1–v6 全部 17 个回调至原厂 `libhoudini.so`，将 v7 `getTrampolineWithJNICallType`（`+0x90`）转发至 `real->getTrampoline`，在 v7 `getTrampolineForFunctionPointer`（`+0x98`）中恒等返回 `method`，在 v8 `isNativeBridgeFunctionPointer`（`+0xa0`）返回 `false`。
     2. **AOSP 16 x86_64 `linker64` ThinLTO 符号后缀（`mprotect()` `SIGSEGV (SI_KERNEL)`）**：
        - Houdini 14.0.0 在处理 guest `mprotect()` 系统调用时，会检查目标地址是否落在宿主机 x86_64 动态库内，为此需要读取 `/apex/com.android.runtime/bin/linker64` 的 `.symtab` 查找 `__dl__ZL6solist` 与 `__dl__ZL12g_cfi_shadow`。
        - AOSP 16 x86_64 `linker64` 启用了 ThinLTO，这两个静态符号被重命名为 `__dl__ZL6solist.llvm.<hash>` 与 `__dl__ZL12g_cfi_shadow.llvm.<hash>`（而 32 位 `linker` 无此后缀）。Houdini 精确匹配失败返回 `0`，随后在 `mprotect()` 时解引用 `*(linker64_base + 0)` 读到 ELF 魔数 `0x00010102464c457f`（`\x7fELF...`）引发非规范地址 `SIGSEGV (SI_KERNEL)`。
        - **解法**：`libhoudini_a16_shim.so` 在加载 `libhoudini.so` 后，经严格 rodata 字符串与指令字节签名校验，动态解析当前系统 `linker64` 的 `.symtab`（兼容精确名与 `.llvm.*` 后缀），将 Houdini 内部两处 5 字节 `callq` 符号查询指令在内存中原地替换为返回真实偏移的 5 字节 `movl $imm32, %eax`（且 AOSP 16 `soinfo` 的 `base(+0x10)` / `size(+0x18)` / `next(+0x28)` 布局与 A13 完全一致）。
3. **备选：A16 原生 `libndk_translation` / Berberis（`tools/pack/pack_ndk_translation.sh`）**
   - 如需纯 AOSP 16 官方开源翻译层，可使用 `pack_ndk_translation.sh` 从 Android 16 SDK `system.img` 注入 Berberis（仅支持 64 位 `arm64-v8a`，部分第三方反作弊/风控 SDK 如数美 `libsmsdk.so` 在 Berberis 下可能存在线程栈边界兼容性问题）。

---

## 5. `fuse-bpf` 跨代内核不匹配修复

- **现象**：WSA 沿用 Android 13 内核，向用户空间上报 `fuse_bpf=supported`，导致 A16 `vold` 在挂载 `/mnt/user/0/emulated` 时跳过对 `/data/media/0/Android/{data,obb}` 的底层 ext4 bind-mount；而 A16 `FuseDaemon` 在 FUSE 路径下默认拒绝所有应用访问自身 `Android/data`（`FuseDaemon: Rejected access to app-private dir on FUSE`），引发外部资源解压或读取失败。
- **修复（`tools/deploy/post_boot_fixes.ps1`）**：执行 `setprop persist.sys.fuse.bpf.override false` 并重启 WSA 一次。重启后 `ro.fuse.bpf.is_running=false`，`vold` 正常建立 ext4 bind-mount，`FuseDaemon: Rejected` 降为 `0`。

---

## 6. 5 个 WSA Binder 服务与 25 条 RCM 通道清单

- **Binder 服务（5/5）**：`IWsaSystemServer`、`IWsaNotifications`、`IWsaSystemApp`、`IGSKServerConnection/default`、`IRemoteConnectionManager/default`
- **RCM 通道（25/25）**：
  - Native（6）：`Camera`、`Composition`、`UserInputNative`、`AudioControl`、`AudioIn`、`AudioOut`
  - SystemUI（2）：`Shell`、`Notification`
  - SystemApp（8）：`Telemetry`、`Settings`、`Geolocation`、`Amazon`、`Apps`、`Lifetime`、`Geocode`、`PermissionUsage`
  - UserApp（9）：`UserInput`、`Clipboard`、`SystemMediaControlsChannel`、`TextToSpeech`、`TSF`、`FileSystem`、`Wns`、`UIABridge`、`Intents`

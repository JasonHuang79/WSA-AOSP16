#!/usr/bin/env python3
"""Build AOSP 16 WSA services.jar with clean A16 WsaLaunchParamsModifier.

Outputs services.jar containing:
  - classes..classes3.dex : stock AOSP 16 services.jar DEX files untouched
  - classes4.dex          : trimmed WSA service classes (old A13 WsaLaunchParamsModifier* removed)
  - classes5.dex          : freshly compiled AOSP 16 WsaLaunchParamsModifier (freeform mode + centered bounds)

Follow up with `patch_services_taskid.py` to add METADATA_TASK_ID propagation in WindowStateAnimator.

Usage:
    python3 build_services_wsa.py \
      --a16-services a16_services.jar \
      --wsa-services-dex wsa-services.dex \
      --output services.wsa.jar \
      --build-tools /path/to/android-sdk/build-tools/36.1.0 \
      --android-jar /path/to/android-sdk/platforms/android-36/android.jar
"""
import argparse
import hashlib
import os
import pathlib
import shutil
import struct
import subprocess
import tempfile
import zipfile
import zlib

T_CLASS_DEF = 0x0006
T_CLASS_DATA = 0x2000


def u4(b, o):
    return struct.unpack_from("<I", b, o)[0]


def p4(b, o, v):
    struct.pack_into("<I", b, o, v)


def read_uleb(b, o):
    r, s = 0, 0
    while True:
        x = b[o]
        o += 1
        r |= (x & 0x7F) << s
        if not (x & 0x80):
            return r, o
        s += 7


def read_string(b, off):
    _, o = read_uleb(b, off)
    return b[o : b.index(0, o)].decode("utf-8", "replace")


def class_data_len(b, off):
    o = off
    sf, o = read_uleb(b, o)
    inf, o = read_uleb(b, o)
    dm, o = read_uleb(b, o)
    vm, o = read_uleb(b, o)
    for _ in range(sf + inf):
        _, o = read_uleb(b, o)
        _, o = read_uleb(b, o)
    for _ in range(dm + vm):
        _, o = read_uleb(b, o)
        _, o = read_uleb(b, o)
        _, o = read_uleb(b, o)
    return o - off


def map_sections(b):
    map_off = u4(b, 0x34)
    n = u4(b, map_off)
    return [
        (
            map_off + 4 + k * 12,
            struct.unpack_from("<H", b, map_off + 4 + k * 12)[0],
            u4(b, map_off + 8 + k * 12),
            u4(b, map_off + 12 + k * 12),
        )
        for k in range(n)
    ]


def trim_dex(raw_bytes, keep_pred):
    data = bytearray(raw_bytes)
    str_off = u4(data, 0x3C)
    type_ids_off = u4(data, 0x44)
    cds_size, cds_off = u4(data, 0x60), u4(data, 0x64)

    kept = []
    all_cd_end = 0
    for i in range(cds_size):
        d_off = cds_off + i * 32
        cidx = u4(data, d_off)
        tidx = u4(data, type_ids_off + cidx * 4)
        nm = read_string(data, u4(data, str_off + tidx * 4))
        cdo = u4(data, d_off + 24)
        ln = class_data_len(data, cdo) if cdo else 0
        if cdo:
            all_cd_end = max(all_cd_end, cdo + ln)
        if keep_pred(nm):
            kept.append((d_off, nm, cdo, ln))

    secs = map_sections(data)
    _, _, _, cd_off = next(s for s in secs if s[1] == T_CLASS_DATA)
    blobs = [(d_off, nm, cdo, ln, bytes(data[cdo : cdo + ln]) if cdo else b"") for d_off, nm, cdo, ln in kept]
    cursor = cd_off
    non_empty = 0
    for d_off, nm, cdo, ln, blob in blobs:
        if cdo:
            data[cursor : cursor + ln] = blob
            p4(data, d_off + 24, cursor)
            cursor += ln
            non_empty += 1
        else:
            p4(data, d_off + 24, 0)
    if all_cd_end > cursor:
        data[cursor:all_cd_end] = b"\x00" * (all_cd_end - cursor)
    for e, typ, size, off in secs:
        if typ == T_CLASS_DATA:
            p4(data, e + 4, non_empty)
        elif typ == T_CLASS_DEF:
            p4(data, e + 4, len(kept))
    def_blob = b"".join(bytes(data[d : d + 32]) for d, _, _, _ in kept)
    data[cds_off : cds_off + len(def_blob)] = def_blob
    p4(data, 0x60, len(kept))
    data_off = u4(data, 0x6C)
    hole = cds_off + len(def_blob)
    if data_off > hole:
        data[hole:data_off] = b"\x00" * (data_off - hole)
    p4(data, 0x08, 0)
    data[0x0C:0x20] = hashlib.sha1(bytes(data[32:])).digest()
    p4(data, 0x08, zlib.adler32(bytes(data[12:])) & 0xFFFFFFFF)
    return bytes(data), [nm for _, nm, _, _ in kept]


def tool_exe(bt: pathlib.Path, name: str) -> str:
    if os.name == "nt":
        for ext in (".exe", ".bat"):
            p = bt / f"{name}{ext}"
            if p.exists():
                return str(p)
    return str(bt / name)


def wf(root: pathlib.Path, rel: str, txt: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(txt.lstrip("\n"), encoding="utf-8")


WLPM_JAVA = r"""
package com.android.server.wm;

import android.app.ActivityOptions;
import android.content.Context;
import android.content.pm.ActivityInfo;
import android.graphics.Rect;
import android.util.Log;
import com.microsoft.windows.service.Bounds;
import java.lang.reflect.Method;
import java.util.concurrent.ConcurrentHashMap;

public class WsaLaunchParamsModifier implements LaunchParamsController.LaunchParamsModifier {
    private static final String TAG = "WsaLaunchParamsModifier";
    private static final int WINDOWING_MODE_UNDEFINED = 0;
    private static final int WINDOWING_MODE_FULLSCREEN = 1;
    private static final int WINDOWING_MODE_PINNED = 2;
    private static final int WINDOWING_MODE_FREEFORM = 5;

    private static final int ACTIVITY_TYPE_UNDEFINED = 0;
    private static final int ACTIVITY_TYPE_STANDARD = 1;

    private final ActivityTaskSupervisor mSupervisor;
    private final ConcurrentHashMap<Integer, Bounds> mDisplayStableBoundsByDisplayId = new ConcurrentHashMap<>();
    private final ConcurrentHashMap<Integer, Bounds> mDisplayMaximizedBoundsByDisplayId = new ConcurrentHashMap<>();

    public WsaLaunchParamsModifier(ActivityTaskSupervisor supervisor, Context context) {
        this.mSupervisor = supervisor;
        Log.i(TAG, "Registering A16 WsaLaunchParamsModifier");
        if (supervisor != null && supervisor.getLaunchParamsController() != null) {
            supervisor.getLaunchParamsController().registerModifier(this);
        }
    }

    public static WsaLaunchParamsModifier CreateWsaModifier(Context context) {
        Log.i(TAG, "WSA System Service started (A16 WsaLaunchParamsModifier)");
        try {
            Class<?> atmCls = Class.forName("android.app.ActivityTaskManager");
            Object svc = atmCls.getMethod("getService").invoke(null);
            ActivityTaskManagerService atms = (ActivityTaskManagerService) svc;
            return new WsaLaunchParamsModifier(atms.mTaskSupervisor, context);
        } catch (Throwable t) {
            Log.e(TAG, "CreateWsaModifier failed", t);
            return new WsaLaunchParamsModifier(null, context);
        }
    }

    private static int safeDisplayId(Object obj) {
        if (obj == null) return 0;
        try {
            Object v = obj.getClass().getMethod("getDisplayId").invoke(obj);
            if (v instanceof Integer) return (Integer) v;
        } catch (Throwable ignored) {}
        return 0;
    }

    public void updateDisplayBounds(int displayId, Bounds stableBounds, Bounds maximizedBounds) {
        if (stableBounds != null) mDisplayStableBoundsByDisplayId.put(displayId, stableBounds);
        if (maximizedBounds != null) mDisplayMaximizedBoundsByDisplayId.put(displayId, maximizedBounds);
    }

    private static int getOptionsWindowingMode(ActivityOptions options) {
        if (options == null) return WINDOWING_MODE_UNDEFINED;
        try {
            Method m = options.getClass().getMethod("getLaunchWindowingMode");
            Object v = m.invoke(options);
            if (v instanceof Integer) return (Integer) v;
        } catch (Throwable ignored) {}
        return WINDOWING_MODE_UNDEFINED;
    }

    private static boolean isSystemShellOrHome(ActivityRecord activity, Task task) {
        if (activity != null) {
            int at = activity.getActivityType();
            if (at != ACTIVITY_TYPE_UNDEFINED && at != ACTIVITY_TYPE_STANDARD) return true;
            if (activity.isActivityTypeHome()) return true;
            String pkg = activity.packageName;
            if (pkg == null && activity.info != null) pkg = activity.info.packageName;
            if ("com.microsoft.windows.homeapp".equals(pkg)
                    || "com.android.launcher3".equals(pkg)
                    || "com.android.systemui".equals(pkg)) {
                return true;
            }
        }
        if (task != null) {
            int at = task.getActivityType();
            if (at != ACTIVITY_TYPE_UNDEFINED && at != ACTIVITY_TYPE_STANDARD) return true;
            if (task.isActivityTypeHome()) return true;
        }
        return false;
    }

    @Override
    public int onCalculate(Task task, ActivityInfo.WindowLayout layout, ActivityRecord activity,
            ActivityRecord source, ActivityOptions options, ActivityStarter.Request request,
            int phase, LaunchParamsController.LaunchParams currentParams,
            LaunchParamsController.LaunchParams outParams) {
        try {
            outParams.set(currentParams);
            if (task == null && activity == null) return RESULT_SKIP;
            if (phase == PHASE_DISPLAY || phase == PHASE_DISPLAY_AREA) return RESULT_SKIP;
            if (isSystemShellOrHome(activity, task)) return RESULT_SKIP;

            int reqMode = getOptionsWindowingMode(options);
            if (reqMode == WINDOWING_MODE_PINNED) return RESULT_SKIP;

            int targetMode = (reqMode == WINDOWING_MODE_FULLSCREEN)
                    ? WINDOWING_MODE_FULLSCREEN
                    : WINDOWING_MODE_FREEFORM;
            outParams.mWindowingMode = targetMode;
            if (phase == PHASE_WINDOWING_MODE) return RESULT_DONE;

            if (targetMode == WINDOWING_MODE_FREEFORM && outParams.mBounds.isEmpty()) {
                int displayId = 0;
                if (outParams.mPreferredTaskDisplayArea != null) displayId = safeDisplayId(outParams.mPreferredTaskDisplayArea);
                else if (activity != null) displayId = safeDisplayId(activity);
                else if (task != null) displayId = safeDisplayId(task);

                Rect stable = new Rect(0, 0, 1920, 1080);
                Bounds sb = mDisplayStableBoundsByDisplayId.get(displayId);
                if (sb != null && sb.right > sb.left && sb.bottom > sb.top) {
                    stable.set(sb.left, sb.top, sb.right, sb.bottom);
                } else if (outParams.mPreferredTaskDisplayArea != null) {
                    Rect tdaRect = new Rect();
                    outParams.mPreferredTaskDisplayArea.getBounds(tdaRect);
                    if (!tdaRect.isEmpty()) stable.set(tdaRect);
                }

                int sw = Math.max(800, stable.width());
                int sh = Math.max(600, stable.height());
                int w = Math.min(1280, (sw * 3) / 4);
                int h = Math.min(840, (sh * 4) / 5);
                if (layout != null) {
                    if (layout.width > 0) w = Math.min(sw, Math.max(360, layout.width));
                    if (layout.height > 0) h = Math.min(sh, Math.max(360, layout.height));
                }

                int left = stable.left + (sw - w) / 2;
                int top = stable.top + (sh - h) / 2;
                outParams.mBounds.set(left, top, left + w, top + h);
            }
            return RESULT_DONE;
        } catch (Throwable t) {
            Log.e(TAG, "onCalculate error (fallback RESULT_SKIP)", t);
            return RESULT_SKIP;
        }
    }
}
"""


def main():
    ap = argparse.ArgumentParser(description="Build AOSP 16 WSA services.jar")
    ap.add_argument("--a16-services", required=True, type=pathlib.Path)
    ap.add_argument("--wsa-services-dex", required=True, type=pathlib.Path)
    ap.add_argument("--output", required=True, type=pathlib.Path)
    ap.add_argument("--build-tools", required=True, type=pathlib.Path)
    ap.add_argument("--android-jar", required=True, type=pathlib.Path)
    ap.add_argument("--javac", default="javac", help="Path to javac (JDK 17+)")
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as td:
        build = pathlib.Path(td)
        stubs, src, cls, d8_out = build / "stubs", build / "src", build / "cls", build / "d8_out"
        for d in (stubs, src, cls, d8_out):
            d.mkdir(parents=True)

        c4_bytes, kept_names = trim_dex(args.wsa_services_dex.read_bytes(), lambda nm: "WsaLaunchParamsModifier" not in nm)
        c4_path = build / "classes4.dex"
        c4_path.write_bytes(c4_bytes)
        print(f"[OK] Trimmed classes4.dex ({len(kept_names)} classes)")

        wf(stubs, "com/microsoft/windows/service/Bounds.java", "package com.microsoft.windows.service;\npublic class Bounds { public int left, top, right, bottom; }\n")
        wf(stubs, "android/app/ActivityTaskManager.java", "package android.app;\npublic class ActivityTaskManager { public static Object getService() { return null; } }\n")
        wf(stubs, "com/android/server/wm/ConfigurationContainer.java", "package com.android.server.wm;\nimport android.graphics.Rect;\npublic abstract class ConfigurationContainer { public int getActivityType() { return 0; } public boolean isActivityTypeHome() { return false; } public boolean isActivityTypeStandard() { return false; } public void getBounds(Rect outBounds) {} }\n")
        wf(stubs, "com/android/server/wm/WindowContainer.java", "package com.android.server.wm;\npublic class WindowContainer extends ConfigurationContainer {}\n")
        wf(stubs, "com/android/server/wm/TaskDisplayArea.java", "package com.android.server.wm;\npublic class TaskDisplayArea extends WindowContainer { public int getDisplayId() { return 0; } }\n")
        wf(stubs, "com/android/server/wm/Task.java", "package com.android.server.wm;\npublic class Task extends WindowContainer { public int getDisplayId() { return 0; } }\n")
        wf(stubs, "com/android/server/wm/ActivityRecord.java", "package com.android.server.wm;\nimport android.content.ComponentName;\nimport android.content.pm.ActivityInfo;\npublic class ActivityRecord extends WindowContainer { public ActivityInfo info; public ComponentName mActivityComponent; public String packageName; public int getDisplayId() { return 0; } }\n")
        wf(stubs, "com/android/server/wm/ActivityStarter.java", "package com.android.server.wm;\npublic class ActivityStarter { public static class Request {} }\n")
        wf(stubs, "com/android/server/wm/LaunchParamsController.java", """
package com.android.server.wm;
import android.app.ActivityOptions;
import android.content.pm.ActivityInfo;
import android.graphics.Rect;
public class LaunchParamsController {
    public static class LaunchParams {
        public final Rect mBounds = new Rect();
        public TaskDisplayArea mPreferredTaskDisplayArea;
        public int mWindowingMode;
        public void set(LaunchParams params) {}
    }
    public interface LaunchParamsModifier {
        int RESULT_SKIP = 0, RESULT_DONE = 1, RESULT_CONTINUE = 2;
        int PHASE_DISPLAY = 0, PHASE_WINDOWING_MODE = 1, PHASE_DISPLAY_AREA = 2, PHASE_BOUNDS = 3;
        int onCalculate(Task task, ActivityInfo.WindowLayout layout, ActivityRecord activity,
                ActivityRecord source, ActivityOptions options, ActivityStarter.Request request,
                int phase, LaunchParams currentParams, LaunchParams outParams);
    }
    public void registerModifier(LaunchParamsModifier modifier) {}
}
""")
        wf(stubs, "com/android/server/wm/ActivityTaskSupervisor.java", "package com.android.server.wm;\npublic class ActivityTaskSupervisor { public LaunchParamsController getLaunchParamsController() { return null; } }\n")
        wf(stubs, "com/android/server/wm/ActivityTaskManagerService.java", "package com.android.server.wm;\npublic class ActivityTaskManagerService { public ActivityTaskSupervisor mTaskSupervisor; }\n")
        wf(src, "com/android/server/wm/WsaLaunchParamsModifier.java", WLPM_JAVA)

        java_files = [str(p) for p in list(stubs.rglob("*.java")) + list(src.rglob("*.java"))]
        subprocess.run([args.javac, "--release", "17", "-cp", str(args.android_jar), "-d", str(cls)] + java_files, check=True)
        wlpm_class = cls / "com" / "android" / "server" / "wm" / "WsaLaunchParamsModifier.class"
        subprocess.run([
            tool_exe(args.build_tools, "d8"), "--min-api", "33", "--output", str(d8_out),
            "--lib", str(args.android_jar), "--classpath", str(cls), str(wlpm_class),
        ], check=True)
        c5_path = d8_out / "classes.dex"

        for p in (c4_path, c5_path):
            subprocess.run([tool_exe(args.build_tools, "dexdump"), "-f", str(p)], check=True, stdout=subprocess.DEVNULL)

        with zipfile.ZipFile(args.a16_services, "r") as zin, zipfile.ZipFile(args.output, "w") as zout:
            for info in zin.infolist():
                if info.filename in ("classes4.dex", "classes5.dex"):
                    continue
                ni = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                ni.compress_type = info.compress_type
                ni.external_attr = info.external_attr
                zout.writestr(ni, zin.read(info.filename))
            for name, fpath in [("classes4.dex", c4_path), ("classes5.dex", c5_path)]:
                ni = zipfile.ZipInfo(name, date_time=(2009, 1, 1, 0, 0, 0))
                ni.compress_type = zipfile.ZIP_DEFLATED
                zout.writestr(ni, fpath.read_bytes())

    md5 = hashlib.md5(args.output.read_bytes()).hexdigest()
    print(f"[OK] Built {args.output} ({args.output.stat().st_size:,} B, md5={md5})")


if __name__ == "__main__":
    main()

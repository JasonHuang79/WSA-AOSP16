#!/usr/bin/env python3
"""Graft WSA Shell (WsaTaskManager, Shell & Notification RCM channels) onto AOSP 16 SystemUI.apk.

Produces a platform-signed SystemUI.apk with:
  - classes.dex  : AOSP 16 SystemUI classes.dex with PhoneSystemUIAppComponentFactory removed
  - classes2.dex : AOSP 16 SystemUI classes2.dex untouched (apply patch_systemui_nocaption.py afterwards)
  - classes3.dex : Trimmed WSA Shell/Notification/Protobuf classes from A13 SystemUIApp classes24.dex,
                   with `com/google/protobuf/` isolated in-place to `com/google/protobuw/` and
                   WsaTaskManager's 30s CacheCleanupTimerTask NOP'd
  - classes4.dex : Compiled WsaShellBridge + WsaTaskListenerProxy (A16 reparentChildSurfaceToTask arg-order adapter)
                   + WsaPipController stub + hooked PhoneSystemUIAppComponentFactory

Usage:
    python3 build_systemui_wsa.py \
      --a16-systemui a16_SystemUI.apk \
      --a13-c24-dex sysui_c24.dex \
      --output SystemUI.wsa.apk \
      --build-tools /path/to/android-sdk/build-tools/36.1.0 \
      --android-jar /path/to/android-sdk/platforms/android-36/android.jar \
      --key platform.pk8 --cert platform.x509.pem
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


def trim_dex(raw_bytes, keep_pred, rename_protobuf=False):
    data = bytearray(raw_bytes)
    str_cnt, str_off = u4(data, 0x38), u4(data, 0x3C)
    type_ids_off = u4(data, 0x44)
    cds_size, cds_off = u4(data, 0x60), u4(data, 0x64)

    if rename_protobuf:
        renamed = 0
        for i in range(str_cnt):
            s_off = u4(data, str_off + i * 4)
            _, p = read_uleb(data, s_off)
            end = data.index(0, p)
            raw_s = bytes(data[p:end])
            if b"com/google/protobuf/" in raw_s:
                data[p:end] = raw_s.replace(b"com/google/protobuf/", b"com/google/protobuw/")
                renamed += 1
            elif b"com.google.protobuf." in raw_s:
                data[p:end] = raw_s.replace(b"com.google.protobuf.", b"com.google.protobuw.")
                renamed += 1
        print(f"  [PROTOBUF] isolated {renamed} string_data items: protobuf -> protobuw")

    kept, all_cd_end = [], 0
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
    cursor, non_empty = cd_off, 0
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
    return bytes(data), len(kept)


def tool_exe(bt: pathlib.Path, name: str) -> str:
    if os.name == "nt":
        for ext in (".exe", ".bat"):
            p = bt / f"{name}{ext}"
            if p.exists():
                return str(p)
    return str(bt / name)


def wfile(root: pathlib.Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.lstrip("\n"), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description="Build AOSP 16 SystemUI.apk with grafted WSA Shell/Notification bridge")
    ap.add_argument("--a16-systemui", required=True, type=pathlib.Path)
    ap.add_argument("--a13-c24-dex", required=True, type=pathlib.Path)
    ap.add_argument("--output", required=True, type=pathlib.Path)
    ap.add_argument("--build-tools", required=True, type=pathlib.Path)
    ap.add_argument("--android-jar", required=True, type=pathlib.Path)
    ap.add_argument("--key", required=True, type=pathlib.Path)
    ap.add_argument("--cert", required=True, type=pathlib.Path)
    ap.add_argument("--javac", default="javac")
    args = ap.parse_args()

    def keep_wsa_c24(nm: str) -> bool:
        if nm.startswith("Lwindows/services/"):
            return True
        if nm.startswith("Lcom/google/protobuw/") and not nm.startswith("Lcom/google/protobuw/nano/"):
            return True
        if nm.startswith(("Lcom/microsoft/windows/redirection/", "Lcom/microsoft/windows/service/", "Lcom/microsoft/windows/systemapp/")):
            return True
        if nm.startswith("Lcom/microsoft/windows/CommonHelper") or nm in ("Lcom/microsoft/windows/LogUtils;", "Lcom/microsoft/windows/SharedMemoryBuffer;"):
            return True
        return nm.startswith("Lcom/microsoft/windows/shell/WsaTaskManager")

    with tempfile.TemporaryDirectory() as td:
        build = pathlib.Path(td)
        stubs_dir, src_dir, cls_dir, d8_in, d8_out = (
            build / "stubs", build / "src", build / "cls", build / "d8_in", build / "d8_out"
        )
        for d in (stubs_dir, src_dir, cls_dir, d8_in, d8_out):
            d.mkdir(parents=True)

        with zipfile.ZipFile(args.a16_systemui) as z:
            orig_c1 = z.read("classes.dex")

        c1_bytes, k1 = trim_dex(orig_c1, lambda nm: nm != "Lcom/android/systemui/PhoneSystemUIAppComponentFactory;")
        c1_path = build / "classes.dex"
        c1_path.write_bytes(c1_bytes)
        print(f"[OK] Trimmed classes.dex ({k1} classes)")

        c3_bytes, k3 = trim_dex(args.a13_c24_dex.read_bytes(), keep_wsa_c24, rename_protobuf=True)
        c3_ba = bytearray(c3_bytes)
        p_off = 0x2553FA
        assert bytes(c3_ba[p_off : p_off + 6]) == bytes.fromhex("740653650200"), "Timer.schedule opcode mismatch"
        c3_ba[p_off : p_off + 6] = b"\x00" * 6
        c3_ba[0x0C:0x20] = hashlib.sha1(bytes(c3_ba[32:])).digest()
        struct.pack_into("<I", c3_ba, 0x08, zlib.adler32(bytes(c3_ba[12:])) & 0xFFFFFFFF)
        c3_path = build / "classes3.dex"
        c3_path.write_bytes(bytes(c3_ba))
        print(f"[OK] Built classes3.dex ({k3} classes, Timer.schedule NOP'd)")

        # Compile-time stubs
        wfile(stubs_dir, "com/android/systemui/SystemUIAppComponentFactoryBase.java", "package com.android.systemui;\nimport android.app.AppComponentFactory;\npublic abstract class SystemUIAppComponentFactoryBase extends AppComponentFactory { public static Object systemUIInitializer; }\n")
        wfile(stubs_dir, "com/android/wm/shell/ShellTaskOrganizer.java", """
package com.android.wm.shell;
import android.app.ActivityManager;
import android.view.SurfaceControl;
import java.io.PrintWriter;
import java.util.concurrent.Executor;
public class ShellTaskOrganizer {
    public interface TaskListener {
        default void onTaskAppeared(ActivityManager.RunningTaskInfo taskInfo, SurfaceControl leash) {}
        default void onTaskInfoChanged(ActivityManager.RunningTaskInfo taskInfo) {}
        default void onTaskVanished(ActivityManager.RunningTaskInfo taskInfo) {}
        default void onBackPressedOnTaskRoot(ActivityManager.RunningTaskInfo taskInfo) {}
        default void attachChildSurfaceToTask(int taskId, SurfaceControl.Builder b) {}
        default void reparentChildSurfaceToTask(int taskId, SurfaceControl.Transaction t, SurfaceControl sc) {}
        default boolean supportCompatUI() { return true; }
        default void dump$2(PrintWriter pw, String prefix) {}
    }
    public final void addListenerForType(TaskListener listener, int... listenerTypes) {}
    public Executor getExecutor() { return null; }
}
""")
        wfile(stubs_dir, "com/microsoft/windows/shell/WsaTaskManager.java", """
package com.microsoft.windows.shell;
import android.app.ActivityManager;
import android.content.Context;
import android.view.SurfaceControl;
import com.android.wm.shell.ShellTaskOrganizer;
public class WsaTaskManager {
    public static WsaTaskManager getInstance(Context ctx, ShellTaskOrganizer org, WsaPipController pip) { return null; }
    public void onTaskAppeared(ActivityManager.RunningTaskInfo taskInfo, SurfaceControl leash) {}
    public void onTaskInfoChanged(ActivityManager.RunningTaskInfo taskInfo) {}
    public void onTaskVanished(ActivityManager.RunningTaskInfo taskInfo) {}
    public void onBackPressedOnTaskRoot(ActivityManager.RunningTaskInfo taskInfo) {}
    public void attachChildSurfaceToTask(int taskId, SurfaceControl.Builder b) {}
    public void reparentChildSurfaceToTask(int taskId, SurfaceControl sc, SurfaceControl.Transaction t) {}
}
""")
        wfile(stubs_dir, "com/microsoft/windows/redirection/NotificationRedirectionHandler.java", "package com.microsoft.windows.redirection;\nimport android.content.Context;\npublic class NotificationRedirectionHandler { public NotificationRedirectionHandler(Context ctx) {} }\n")

        # Runtime classes for classes4.dex
        wfile(src_dir, "com/android/wm/shell/pip/phone/PhonePipMenuController.java", """
package com.android.wm.shell.pip.phone;
public class PhonePipMenuController {
    public interface Listener {
        default void onPipMenuStateChangeStart(int menuState, boolean resize, Runnable callback) {}
        default void onPipMenuStateChangeFinish(int menuState) {}
        default void onPipExpand() {}
        default void onPipDismiss() {}
        default void onPipShowMenu() {}
    }
}
""")
        wfile(src_dir, "com/microsoft/windows/shell/WsaPipController.java", """
package com.microsoft.windows.shell;
import android.app.ActivityManager;
import android.app.PictureInPictureParams;
import android.graphics.Rect;
import com.android.wm.shell.pip.phone.PhonePipMenuController;
public class WsaPipController {
    public WsaPipController() {}
    public void addPipMenuListener(PhonePipMenuController.Listener listener) {}
    public float getDefaultPipAspectRatio() { return 1.7777778f; }
    public Rect getPipBoundsForDisplay(int displayId, int width, int height) {
        int w = Math.max(320, width / 4);
        int h = Math.max(180, (int) (w / 1.7777778f));
        return new Rect(0, 0, w, h);
    }
    public void attachAndUpdatePipMenu(ActivityManager.RunningTaskInfo taskInfo, Rect bounds, boolean show) {}
    public void updatePipMenuDisplayId(int displayId) {}
    public void movePipMenu(Rect bounds) {}
    public void resetPipMenu() {}
    public void updatePipMenuActions(PictureInPictureParams params) {}
    public Integer getPipTaskId() { return null; }
}
""")
        wfile(src_dir, "com/microsoft/windows/shell/WsaTaskListenerProxy.java", """
package com.microsoft.windows.shell;
import android.app.ActivityManager;
import android.util.Log;
import android.view.SurfaceControl;
import com.android.wm.shell.ShellTaskOrganizer;
public final class WsaTaskListenerProxy implements ShellTaskOrganizer.TaskListener {
    private static final String TAG = "WsaShellBridge";
    private final WsaTaskManager mDelegate;
    public WsaTaskListenerProxy(WsaTaskManager delegate) { this.mDelegate = delegate; }
    @Override public void onTaskAppeared(ActivityManager.RunningTaskInfo taskInfo, SurfaceControl leash) {
        try { mDelegate.onTaskAppeared(taskInfo, leash); } catch (Throwable t) { Log.e(TAG, "onTaskAppeared error", t); }
    }
    @Override public void onTaskInfoChanged(ActivityManager.RunningTaskInfo taskInfo) {
        try { mDelegate.onTaskInfoChanged(taskInfo); } catch (Throwable t) { Log.e(TAG, "onTaskInfoChanged error", t); }
    }
    @Override public void onTaskVanished(ActivityManager.RunningTaskInfo taskInfo) {
        try { mDelegate.onTaskVanished(taskInfo); } catch (Throwable t) { Log.e(TAG, "onTaskVanished error", t); }
    }
    @Override public void onBackPressedOnTaskRoot(ActivityManager.RunningTaskInfo taskInfo) {
        try { mDelegate.onBackPressedOnTaskRoot(taskInfo); } catch (Throwable t) { Log.e(TAG, "onBackPressedOnTaskRoot error", t); }
    }
    @Override public void attachChildSurfaceToTask(int taskId, SurfaceControl.Builder b) {
        try { mDelegate.attachChildSurfaceToTask(taskId, b); } catch (Throwable t) { Log.e(TAG, "attachChildSurfaceToTask error", t); }
    }
    @Override public void reparentChildSurfaceToTask(int taskId, SurfaceControl.Transaction t, SurfaceControl sc) {
        try { mDelegate.reparentChildSurfaceToTask(taskId, sc, t); } catch (Throwable ex) { Log.e(TAG, "reparentChildSurfaceToTask error", ex); }
    }
    @Override public boolean supportCompatUI() { return false; }
}
""")
        wfile(src_dir, "com/microsoft/windows/shell/WsaShellBridge.java", """
package com.microsoft.windows.shell;
import android.content.Context;
import android.util.Log;
import android.util.SparseArray;
import com.android.systemui.SystemUIAppComponentFactoryBase;
import com.android.wm.shell.ShellTaskOrganizer;
import com.microsoft.windows.redirection.NotificationRedirectionHandler;
import java.lang.reflect.Field;
import java.lang.reflect.Method;
import java.util.concurrent.Executor;
import java.util.concurrent.atomic.AtomicBoolean;
public final class WsaShellBridge {
    private static final String TAG = "WsaShellBridge";
    private static final AtomicBoolean sStarted = new AtomicBoolean(false);
    private static volatile NotificationRedirectionHandler sNotificationHandler;
    public static void startAsync() {
        if (!sStarted.compareAndSet(false, true)) return;
        Thread t = new Thread(WsaShellBridge::initLoop, "WsaShellBridge-Init");
        t.setDaemon(true);
        t.start();
    }
    private static Field findField(Class<?> cls, String name) {
        for (Class<?> c = cls; c != null; c = c.getSuperclass()) {
            try { Field f = c.getDeclaredField(name); f.setAccessible(true); return f; } catch (NoSuchFieldException ignored) {}
        }
        return null;
    }
    private static Context resolveContext(Object initializer) {
        try {
            Field f = findField(initializer.getClass(), "mContext");
            if (f != null && f.get(initializer) instanceof Context) return (Context) f.get(initializer);
        } catch (Throwable ignored) {}
        try {
            Object app = Class.forName("android.app.ActivityThread").getMethod("currentApplication").invoke(null);
            if (app instanceof Context) return (Context) app;
        } catch (Throwable ignored) {}
        return null;
    }
    private static ShellTaskOrganizer resolveOrganizer(Object wmComponent) {
        try {
            Field f = findField(wmComponent.getClass(), "provideShellTaskOrganizerProvider");
            if (f != null) {
                Object provider = f.get(wmComponent);
                if (provider != null) {
                    Object org = provider.getClass().getMethod("get").invoke(provider);
                    if (org instanceof ShellTaskOrganizer) return (ShellTaskOrganizer) org;
                }
            }
        } catch (Throwable t) { Log.e(TAG, "resolveOrganizer failed", t); }
        return null;
    }
    private static void initLoop() {
        try {
            Object initializer = null, wmComponent = null;
            Context ctx = null;
            ShellTaskOrganizer organizer = null;
            Field initField = findField(SystemUIAppComponentFactoryBase.class, "systemUIInitializer");
            for (int i = 0; i < 600; i++) {
                if (initializer == null && initField != null) initializer = initField.get(null);
                if (initializer != null && wmComponent == null) wmComponent = initializer.getClass().getMethod("getWMComponent").invoke(initializer);
                if (initializer != null && ctx == null) ctx = resolveContext(initializer);
                if (wmComponent != null && organizer == null) organizer = resolveOrganizer(wmComponent);
                if (ctx != null && organizer != null) break;
                Thread.sleep(100);
            }
            if (ctx == null || organizer == null) return;
            Field lockF = findField(organizer.getClass(), "mLock");
            Field listenersF = findField(organizer.getClass(), "mTaskListeners");
            for (int i = 0; i < 50; i++) {
                if (listenersF != null) {
                    SparseArray<?> sa = (SparseArray<?>) listenersF.get(organizer);
                    if (sa != null && sa.size() > 0) break;
                }
                Thread.sleep(100);
            }
            Thread.sleep(300);
            final Context finalCtx = ctx;
            final ShellTaskOrganizer finalOrg = organizer;
            Executor exec = null;
            try {
                Object exObj = finalOrg.getClass().getMethod("getExecutor").invoke(finalOrg);
                if (exObj instanceof Executor) exec = (Executor) exObj;
            } catch (Throwable ignored) {}
            Runnable task = () -> installOnShellThread(finalCtx, finalOrg, lockF, listenersF);
            if (exec != null) exec.execute(task); else task.run();
        } catch (Throwable t) { Log.e(TAG, "initLoop fatal", t); }
    }
    private static void installOnShellThread(Context ctx, ShellTaskOrganizer organizer, Field lockF, Field listenersF) {
        try {
            WsaTaskManager mgr = WsaTaskManager.getInstance(ctx, organizer, new WsaPipController());
            WsaTaskListenerProxy proxy = new WsaTaskListenerProxy(mgr);
            int[] types = new int[] { -1, -5, -2, -4 };
            Object lock = lockF != null ? lockF.get(organizer) : organizer;
            synchronized (lock) {
                if (listenersF != null) {
                    SparseArray<?> sa = (SparseArray<?>) listenersF.get(organizer);
                    if (sa != null) for (int tp : types) sa.remove(tp);
                }
                organizer.addListenerForType(proxy, types);
            }
            Log.i(TAG, "WsaTaskListenerProxy registered on ShellTaskOrganizer");
        } catch (Throwable t) { Log.e(TAG, "installOnShellThread failed", t); }
        try {
            sNotificationHandler = new NotificationRedirectionHandler(ctx);
        } catch (Throwable t) { Log.w(TAG, "NotificationRedirectionHandler init warning", t); }
    }
}
""")
        wfile(src_dir, "com/android/systemui/PhoneSystemUIAppComponentFactory.java", """
package com.android.systemui;
import com.microsoft.windows.shell.WsaShellBridge;
public final class PhoneSystemUIAppComponentFactory extends SystemUIAppComponentFactoryBase {
    public PhoneSystemUIAppComponentFactory() {
        super();
        WsaShellBridge.startAsync();
    }
}
""")

        java_files = [str(p) for p in list(stubs_dir.rglob("*.java")) + list(src_dir.rglob("*.java"))]
        subprocess.run([args.javac, "--release", "17", "-cp", str(args.android_jar), "-d", str(cls_dir)] + java_files, check=True)

        keep_rel = [
            pathlib.Path("com/android/wm/shell/pip/phone/PhonePipMenuController$Listener.class"),
            pathlib.Path("com/microsoft/windows/shell/WsaPipController.class"),
            pathlib.Path("com/microsoft/windows/shell/WsaTaskListenerProxy.class"),
            pathlib.Path("com/microsoft/windows/shell/WsaShellBridge.class"),
            pathlib.Path("com/android/systemui/PhoneSystemUIAppComponentFactory.class"),
        ]
        d8_files = []
        for rel in keep_rel:
            p = cls_dir / rel
            dst = d8_in / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dst)
            d8_files.append(str(dst))
        for extra in (cls_dir / "com/microsoft/windows/shell").glob("WsaShellBridge$*.class"):
            dst = d8_in / extra.relative_to(cls_dir)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(extra, dst)
            d8_files.append(str(dst))

        subprocess.run([
            tool_exe(args.build_tools, "d8"), "--min-api", "33", "--output", str(d8_out),
            "--lib", str(args.android_jar), "--classpath", str(cls_dir),
        ] + d8_files, check=True)
        c4_path = d8_out / "classes.dex"

        for p in (c1_path, c3_path, c4_path):
            subprocess.run([tool_exe(args.build_tools, "dexdump"), "-f", str(p)], check=True, stdout=subprocess.DEVNULL)

        unsigned_apk = build / "unsigned.apk"
        aligned_apk = build / "aligned.apk"
        with zipfile.ZipFile(args.a16_systemui, "r") as zin, zipfile.ZipFile(unsigned_apk, "w") as zout:
            for info in zin.infolist():
                fn = info.filename
                if fn.startswith("META-INF/") and (fn.endswith((".SF", ".RSA", ".DSA", ".EC")) or fn == "META-INF/MANIFEST.MF"):
                    continue
                ni = zipfile.ZipInfo(fn, date_time=info.date_time)
                ni.compress_type = info.compress_type
                ni.external_attr = info.external_attr
                zout.writestr(ni, c1_path.read_bytes() if fn == "classes.dex" else zin.read(fn))
            for dex_name, dex_file in [("classes3.dex", c3_path), ("classes4.dex", c4_path)]:
                ni = zipfile.ZipInfo(dex_name, date_time=(2009, 1, 1, 0, 0, 0))
                ni.compress_type = zipfile.ZIP_STORED
                zout.writestr(ni, dex_file.read_bytes())

        subprocess.run([tool_exe(args.build_tools, "zipalign"), "-f", "-p", "4", str(unsigned_apk), str(aligned_apk)], check=True)
        subprocess.run([
            tool_exe(args.build_tools, "apksigner"), "sign",
            "--key", str(args.key), "--cert", str(args.cert),
            "--min-sdk-version", "33",
            "--v1-signing-enabled", "true", "--v2-signing-enabled", "true", "--v3-signing-enabled", "true",
            "--out", str(args.output), str(aligned_apk),
        ], check=True)
        subprocess.run([tool_exe(args.build_tools, "apksigner"), "verify", str(args.output)], check=True)

    md5 = hashlib.md5(args.output.read_bytes()).hexdigest()
    print(f"[OK] Built & signed {args.output} ({args.output.stat().st_size:,} B, md5={md5})")


if __name__ == "__main__":
    main()

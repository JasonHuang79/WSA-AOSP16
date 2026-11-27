/*
 * libhoudini_a16_shim.c
 *
 * Minimal Android 16 (API 36) NativeBridge v8 -> v6 compatibility shim
 * for Intel Houdini 14.0.0 (NativeBridge v6) on WSA AOSP 16.
 *
 * SPDX-License-Identifier: MIT
 *
 * Solves two AOSP 16 incompatibilities without modifying ART or libhoudini.so:
 *
 * 1. NativeBridge v6 -> v8 ABI gap (ART RegisterNatives SIGABRT):
 *    In AOSP 16, ART's RegisterNatives unconditionally invokes the v7 callback
 *    `getTrampolineForFunctionPointer` for native-bridge processes and checks
 *    `isNativeBridgeFunctionPointer` (v8). Because Houdini 14.0.0 exports v6,
 *    AOSP 16's `libnativebridge.so` returns NULL, triggering
 *    `Check failed: native_method != nullptr`. Since Houdini already wraps
 *    guest ARM/ARM64 `fnPtr` into a host x86/x86_64 trampoline inside its
 *    `JNIEnv->RegisterNatives` proxy before calling ART, returning `method`
 *    unchanged in `getTrampolineForFunctionPointer` and forwarding
 *    `getTrampolineWithJNICallType` to Houdini's `getTrampoline` restores full
 *    JNI registration compatibility.
 *
 * 2. AOSP 16 ThinLTO `.llvm.<hash>` symbol suffix in `/apex/.../bin/linker64`:
 *    On AOSP 16 x86_64, `linker64` is built with ThinLTO, renaming static
 *    symbols `__dl__ZL6solist` and `__dl__ZL12g_cfi_shadow` to
 *    `__dl__ZL6solist.llvm.<hash>` and `__dl__ZL12g_cfi_shadow.llvm.<hash>`.
 *    Houdini 14.0.0 looks up the unsuffixed names via exact match, gets 0, and
 *    later dereferences `*(linker64_base + 0)` (`\x7fELF...`) on the first
 *    guest `mprotect()` syscall, crashing with `SIGSEGV (SI_KERNEL)`.
 *    After `real->initialize()`, this shim dynamically scans `linker64`'s
 *    `.symtab` for the ThinLTO-suffixed symbols and populates Houdini's two
 *    unresolved `.bss` offset slots in memory (guarded by exact rodata string
 *    signature checks).
 */

#define _GNU_SOURCE
#include <android/log.h>
#include <dlfcn.h>
#include <elf.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <unistd.h>

#define LOG_TAG "houdini_a16_shim"
#define ALOGI(...) __android_log_print(ANDROID_LOG_INFO, LOG_TAG, __VA_ARGS__)
#define ALOGE(...) __android_log_print(ANDROID_LOG_ERROR, LOG_TAG, __VA_ARGS__)

#if defined(__LP64__)
#define HOUDINI_FALLBACK_PATH "/vendor/lib64/libhoudini.so"
#else
#define HOUDINI_FALLBACK_PATH "/vendor/lib/libhoudini.so"
#endif

/* Opaque forward declarations matching AOSP libnativebridge ABI */
struct NativeBridgeRuntimeCallbacks;
struct NativeBridgeRuntimeValues;
struct native_bridge_namespace_t;
typedef bool (*NativeBridgeSignalHandlerFn)(int, void*, void*);

/*
 * Exact AOSP 16 NativeBridgeCallbacks v8 layout.
 * Verified against AOSP 16 x86_64 libnativebridge.so:
 *   +0x00 version
 *   +0x08 initialize
 *   +0x10 loadLibrary
 *   +0x18 getTrampoline
 *   +0x20 isSupported
 *   +0x28 getAppEnv
 *   +0x30 isCompatibleWith
 *   +0x38 getSignalHandler
 *   +0x40 unloadLibrary
 *   +0x48 getError
 *   +0x50 isPathSupported
 *   +0x58 initAnonymousNamespace
 *   +0x60 createNamespace
 *   +0x68 linkNamespaces
 *   +0x70 loadLibraryExt
 *   +0x78 getVendorNamespace
 *   +0x80 getExportedNamespace
 *   +0x88 preZygoteFork
 *   +0x90 getTrampolineWithJNICallType        (v7)
 *   +0x98 getTrampolineForFunctionPointer     (v7)
 *   +0xa0 isNativeBridgeFunctionPointer       (v8)
 * Total size: 0xa8 (64-bit) / 0x54 (32-bit).
 */
struct NativeBridgeCallbacksV6 {
    uint32_t version;
    bool (*initialize)(const struct NativeBridgeRuntimeCallbacks* runtime_cbs,
                       const char* private_dir,
                       const char* instruction_set);
    void* (*loadLibrary)(const char* libpath, int flag);
    void* (*getTrampoline)(void* handle,
                           const char* name,
                           const char* shorty,
                           uint32_t len);
    bool (*isSupported)(const char* libpath);
    const struct NativeBridgeRuntimeValues* (*getAppEnv)(const char* instruction_set);
    bool (*isCompatibleWith)(uint32_t bridge_version);
    NativeBridgeSignalHandlerFn (*getSignalHandler)(int signal);
    int (*unloadLibrary)(void* handle);
    const char* (*getError)(void);
    bool (*isPathSupported)(const char* library_path);
    bool (*initAnonymousNamespace)(const char* public_ns_sonames,
                                   const char* anon_ns_library_path);
    struct native_bridge_namespace_t* (*createNamespace)(
        const char* name,
        const char* ld_library_path,
        const char* default_library_path,
        uint64_t type,
        const char* permitted_when_isolated_path,
        struct native_bridge_namespace_t* parent_ns);
    bool (*linkNamespaces)(struct native_bridge_namespace_t* from,
                           struct native_bridge_namespace_t* to,
                           const char* shared_libs_sonames);
    void* (*loadLibraryExt)(const char* libpath,
                            int flag,
                            struct native_bridge_namespace_t* ns);
    struct native_bridge_namespace_t* (*getVendorNamespace)(void);
    struct native_bridge_namespace_t* (*getExportedNamespace)(const char* name);
    void (*preZygoteFork)(void);
};

struct NativeBridgeCallbacksV8 {
    uint32_t version;
    bool (*initialize)(const struct NativeBridgeRuntimeCallbacks* runtime_cbs,
                       const char* private_dir,
                       const char* instruction_set);
    void* (*loadLibrary)(const char* libpath, int flag);
    void* (*getTrampoline)(void* handle,
                           const char* name,
                           const char* shorty,
                           uint32_t len);
    bool (*isSupported)(const char* libpath);
    const struct NativeBridgeRuntimeValues* (*getAppEnv)(const char* instruction_set);
    bool (*isCompatibleWith)(uint32_t bridge_version);
    NativeBridgeSignalHandlerFn (*getSignalHandler)(int signal);
    int (*unloadLibrary)(void* handle);
    const char* (*getError)(void);
    bool (*isPathSupported)(const char* library_path);
    bool (*initAnonymousNamespace)(const char* public_ns_sonames,
                                   const char* anon_ns_library_path);
    struct native_bridge_namespace_t* (*createNamespace)(
        const char* name,
        const char* ld_library_path,
        const char* default_library_path,
        uint64_t type,
        const char* permitted_when_isolated_path,
        struct native_bridge_namespace_t* parent_ns);
    bool (*linkNamespaces)(struct native_bridge_namespace_t* from,
                           struct native_bridge_namespace_t* to,
                           const char* shared_libs_sonames);
    void* (*loadLibraryExt)(const char* libpath,
                            int flag,
                            struct native_bridge_namespace_t* ns);
    struct native_bridge_namespace_t* (*getVendorNamespace)(void);
    struct native_bridge_namespace_t* (*getExportedNamespace)(const char* name);
    void (*preZygoteFork)(void);
    /* Version 7 */
    void* (*getTrampolineWithJNICallType)(void* handle,
                                          const char* name,
                                          const char* shorty,
                                          uint32_t len,
                                          uint32_t jni_call_type);
    void* (*getTrampolineForFunctionPointer)(const void* method,
                                             const char* shorty,
                                             uint32_t len,
                                             uint32_t jni_call_type);
    /* Version 8 */
    bool (*isNativeBridgeFunctionPointer)(const void* method);
};

#if defined(__LP64__)
_Static_assert(sizeof(struct NativeBridgeCallbacksV8) == 0xa8,
               "NativeBridgeCallbacksV8 size mismatch on 64-bit");
_Static_assert(offsetof(struct NativeBridgeCallbacksV8, isCompatibleWith) == 0x30,
               "isCompatibleWith offset mismatch");
_Static_assert(offsetof(struct NativeBridgeCallbacksV8, preZygoteFork) == 0x88,
               "preZygoteFork offset mismatch");
_Static_assert(offsetof(struct NativeBridgeCallbacksV8, getTrampolineWithJNICallType) == 0x90,
               "getTrampolineWithJNICallType offset mismatch");
_Static_assert(offsetof(struct NativeBridgeCallbacksV8, getTrampolineForFunctionPointer) == 0x98,
               "getTrampolineForFunctionPointer offset mismatch");
_Static_assert(offsetof(struct NativeBridgeCallbacksV8, isNativeBridgeFunctionPointer) == 0xa0,
               "isNativeBridgeFunctionPointer offset mismatch");
#else
_Static_assert(sizeof(struct NativeBridgeCallbacksV8) == 0x54,
               "NativeBridgeCallbacksV8 size mismatch on 32-bit");
_Static_assert(offsetof(struct NativeBridgeCallbacksV8, isNativeBridgeFunctionPointer) == 0x50,
               "isNativeBridgeFunctionPointer offset mismatch on 32-bit");
#endif

static pthread_once_t g_init_once = PTHREAD_ONCE_INIT;
static void* g_houdini_handle = NULL;
static const struct NativeBridgeCallbacksV6* g_real = NULL;
static atomic_int g_logged_fnptr = 0;

#if defined(__LP64__)
/* Offsets verified for Intel Houdini 14.0.0_z.GoogleGame_com1.3 RELEASE (x86_64) */
#define HOUDINI14_STR_LINKER_PATH  0x6843a4u
#define HOUDINI14_STR_CFI_SHADOW   0x6843ccu
#define HOUDINI14_STR_SOLIST       0x6843e4u
#define HOUDINI14_CALL_CFI_LOOKUP  0x3057a7u /* e8 94 18 08 00 : callq 0x387040 */
#define HOUDINI14_CALL_SOL_LOOKUP  0x3057c1u /* e8 7a 18 08 00 : callq 0x387040 */
#define HOUDINI14_BSS_SOLIST_OFF   0x974f28u
#define HOUDINI14_BSS_CFI_OFF      0x974f30u

static bool sym_matches_prefix_or_llvm(const char* name, const char* base) {
    size_t len = strlen(base);
    if (strncmp(name, base, len) != 0) {
        return false;
    }
    return name[len] == '\0' || strncmp(name + len, ".llvm.", 6) == 0;
}

static bool find_linker64_thinlto_syms(const char* elf_path,
                                       uint64_t* out_solist,
                                       uint64_t* out_cfi) {
    *out_solist = 0;
    *out_cfi = 0;

    int fd = open(elf_path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) {
        return false;
    }

    Elf64_Ehdr ehdr;
    if (pread(fd, &ehdr, sizeof(ehdr), 0) != (ssize_t)sizeof(ehdr) ||
        memcmp(ehdr.e_ident, ELFMAG, SELFMAG) != 0 ||
        ehdr.e_ident[EI_CLASS] != ELFCLASS64 ||
        ehdr.e_shoff == 0 || ehdr.e_shnum == 0) {
        close(fd);
        return false;
    }

    size_t sh_bytes = (size_t)ehdr.e_shnum * sizeof(Elf64_Shdr);
    Elf64_Shdr* shdrs = (Elf64_Shdr*)malloc(sh_bytes);
    if (!shdrs) {
        close(fd);
        return false;
    }
    if (pread(fd, shdrs, sh_bytes, (off_t)ehdr.e_shoff) != (ssize_t)sh_bytes) {
        free(shdrs);
        close(fd);
        return false;
    }

    for (uint16_t i = 0; i < ehdr.e_shnum; ++i) {
        if (shdrs[i].sh_type != SHT_SYMTAB || shdrs[i].sh_link >= ehdr.e_shnum) {
            continue;
        }
        const Elf64_Shdr* sym_sh = &shdrs[i];
        const Elf64_Shdr* str_sh = &shdrs[sym_sh->sh_link];
        if (sym_sh->sh_entsize != sizeof(Elf64_Sym) || sym_sh->sh_size == 0 ||
            str_sh->sh_size == 0 || str_sh->sh_size > (32u << 20)) {
            continue;
        }

        Elf64_Sym* syms = (Elf64_Sym*)malloc((size_t)sym_sh->sh_size);
        char* strs = (char*)malloc((size_t)str_sh->sh_size + 1);
        if (!syms || !strs) {
            free(syms);
            free(strs);
            continue;
        }
        if (pread(fd, syms, (size_t)sym_sh->sh_size, (off_t)sym_sh->sh_offset) ==
                (ssize_t)sym_sh->sh_size &&
            pread(fd, strs, (size_t)str_sh->sh_size, (off_t)str_sh->sh_offset) ==
                (ssize_t)str_sh->sh_size) {
            strs[str_sh->sh_size] = '\0';
            size_t count = (size_t)(sym_sh->sh_size / sizeof(Elf64_Sym));
            for (size_t j = 0; j < count; ++j) {
                uint32_t noff = syms[j].st_name;
                if (noff >= str_sh->sh_size) {
                    continue;
                }
                const char* sname = strs + noff;
                if (*out_solist == 0 && sym_matches_prefix_or_llvm(sname, "__dl__ZL6solist")) {
                    *out_solist = syms[j].st_value;
                } else if (*out_cfi == 0 &&
                           sym_matches_prefix_or_llvm(sname, "__dl__ZL12g_cfi_shadow")) {
                    *out_cfi = syms[j].st_value;
                }
                if (*out_solist && *out_cfi) {
                    break;
                }
            }
        }
        free(syms);
        free(strs);
        if (*out_solist && *out_cfi) {
            break;
        }
    }

    free(shdrs);
    close(fd);
    return (*out_solist != 0);
}

static void patch_houdini_linker64_offsets(const struct NativeBridgeCallbacksV6* real) {
    Dl_info dli;
    if (!dladdr((const void*)real, &dli) || !dli.dli_fbase) {
        return;
    }
    uintptr_t base = (uintptr_t)dli.dli_fbase;

    /* Strict signature check for Intel Houdini 14.0.0_z.GoogleGame_com1.3 */
    const char* s_path = (const char*)(base + HOUDINI14_STR_LINKER_PATH);
    const char* s_cfi = (const char*)(base + HOUDINI14_STR_CFI_SHADOW);
    const char* s_solist = (const char*)(base + HOUDINI14_STR_SOLIST);
    if (strcmp(s_path, "/apex/com.android.runtime/bin/linker64") != 0 ||
        strcmp(s_cfi, "__dl__ZL12g_cfi_shadow") != 0 ||
        strcmp(s_solist, "__dl__ZL6solist") != 0) {
        return;
    }

    static const uint8_t exp_call_cfi[5] = {0xe8, 0x94, 0x18, 0x08, 0x00};
    static const uint8_t exp_call_sol[5] = {0xe8, 0x7a, 0x18, 0x08, 0x00};
    uint8_t* p_cfi_call = (uint8_t*)(base + HOUDINI14_CALL_CFI_LOOKUP);
    uint8_t* p_sol_call = (uint8_t*)(base + HOUDINI14_CALL_SOL_LOOKUP);
    if (memcmp(p_cfi_call, exp_call_cfi, 5) != 0 ||
        memcmp(p_sol_call, exp_call_sol, 5) != 0) {
        return;
    }

    uint64_t solist_off = 0;
    uint64_t cfi_off = 0;
    if (!find_linker64_thinlto_syms(s_path, &solist_off, &cfi_off) ||
        solist_off > 0xffffffffu || cfi_off > 0xffffffffu) {
        ALOGE("Failed to resolve ThinLTO linker64 symbols in %s", s_path);
        return;
    }

    /* Replace both 5-byte `callq 0x387040` sites with 5-byte `movl $imm32, %eax`
     * (zero-extended to %rax) so Houdini's lazy init_x86_linker_info() receives
     * the exact AOSP 16 ThinLTO offsets and returns success (1). */
    uintptr_t page = (uintptr_t)p_cfi_call & ~(uintptr_t)0xfffu;
    if (mprotect((void*)page, 0x1000u, PROT_READ | PROT_WRITE) != 0) {
        ALOGE("mprotect(RW) failed for Houdini linker64 lookup patch");
        return;
    }
    uint32_t cfi32 = (uint32_t)cfi_off;
    uint32_t sol32 = (uint32_t)solist_off;
    p_cfi_call[0] = 0xb8;
    memcpy(p_cfi_call + 1, &cfi32, 4);
    p_sol_call[0] = 0xb8;
    memcpy(p_sol_call + 1, &sol32, 4);
    __builtin___clear_cache((char*)p_cfi_call, (char*)(p_sol_call + 5));
    mprotect((void*)page, 0x1000u, PROT_READ | PROT_EXEC);

    /* Also pre-populate .bss in case init_x86_linker_info already ran */
    uint64_t* p_solist_bss = (uint64_t*)(base + HOUDINI14_BSS_SOLIST_OFF);
    uint64_t* p_cfi_bss = (uint64_t*)(base + HOUDINI14_BSS_CFI_OFF);
    if (*p_solist_bss == 0) *p_solist_bss = solist_off;
    if (*p_cfi_bss == 0) *p_cfi_bss = cfi_off;

    ALOGI("Patched Houdini linker64 ThinLTO lookup: solist=0x%x g_cfi_shadow=0x%x",
          sol32, cfi32);
}
#endif /* __LP64__ */

static void load_houdini_once(void) {
    void* h = dlopen("libhoudini.so", RTLD_NOW | RTLD_LOCAL);
    if (!h) {
        const char* err1 = dlerror();
        h = dlopen(HOUDINI_FALLBACK_PATH, RTLD_NOW | RTLD_LOCAL);
        if (!h) {
            const char* err2 = dlerror();
            ALOGE("Failed to load libhoudini.so (%s) and fallback %s (%s)",
                  err1 ? err1 : "unknown", HOUDINI_FALLBACK_PATH,
                  err2 ? err2 : "unknown");
            return;
        }
    }

    const struct NativeBridgeCallbacksV6* itf =
        (const struct NativeBridgeCallbacksV6*)dlsym(h, "NativeBridgeItf");
    if (!itf) {
        ALOGE("NativeBridgeItf symbol not found in libhoudini.so: %s",
              dlerror() ? dlerror() : "unknown");
        dlclose(h);
        return;
    }

    g_houdini_handle = h;
    g_real = itf;
    ALOGI("Loaded underlying libhoudini.so (NativeBridge v%u -> shimmed to v8)",
          itf->version);
#if defined(__LP64__)
    patch_houdini_linker64_offsets(itf);
#endif
}

static inline const struct NativeBridgeCallbacksV6* get_real(void) {
    pthread_once(&g_init_once, load_houdini_once);
    return g_real;
}

__attribute__((destructor))
static void unload_houdini_dtor(void) {
    if (g_houdini_handle) {
        dlclose(g_houdini_handle);
        g_houdini_handle = NULL;
        g_real = NULL;
    }
}

/* ---- v1..v6 forwarding wrappers ---- */

static bool shim_initialize(const struct NativeBridgeRuntimeCallbacks* runtime_cbs,
                            const char* private_dir,
                            const char* instruction_set) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    if (!r || !r->initialize) {
        return false;
    }
    bool ok = r->initialize(runtime_cbs, private_dir, instruction_set);
#if defined(__LP64__)
    if (ok) {
        patch_houdini_linker64_offsets(r);
    }
#endif
    return ok;
}

static void* shim_load_library(const char* libpath, int flag) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->loadLibrary) ? r->loadLibrary(libpath, flag) : NULL;
}

static void* shim_get_trampoline(void* handle,
                                 const char* name,
                                 const char* shorty,
                                 uint32_t len) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->getTrampoline) ? r->getTrampoline(handle, name, shorty, len) : NULL;
}

static bool shim_is_supported(const char* libpath) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->isSupported) ? r->isSupported(libpath) : false;
}

static const struct NativeBridgeRuntimeValues* shim_get_app_env(const char* instruction_set) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->getAppEnv) ? r->getAppEnv(instruction_set) : NULL;
}

static bool shim_is_compatible_with(uint32_t bridge_version) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    if (!r || !r->isCompatibleWith) {
        return false;
    }
    if (bridge_version > 8) {
        return false;
    }
    uint32_t target = (bridge_version > 6) ? 6 : bridge_version;
    return r->isCompatibleWith(target);
}

static NativeBridgeSignalHandlerFn shim_get_signal_handler(int signal) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->getSignalHandler) ? r->getSignalHandler(signal) : NULL;
}

static int shim_unload_library(void* handle) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->unloadLibrary) ? r->unloadLibrary(handle) : -1;
}

static const char* shim_get_error(void) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->getError) ? r->getError() : "houdini_a16_shim: underlying bridge unavailable";
}

static bool shim_is_path_supported(const char* library_path) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->isPathSupported) ? r->isPathSupported(library_path) : false;
}

static bool shim_init_anonymous_namespace(const char* public_ns_sonames,
                                          const char* anon_ns_library_path) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->initAnonymousNamespace)
               ? r->initAnonymousNamespace(public_ns_sonames, anon_ns_library_path)
               : false;
}

static struct native_bridge_namespace_t* shim_create_namespace(
    const char* name,
    const char* ld_library_path,
    const char* default_library_path,
    uint64_t type,
    const char* permitted_when_isolated_path,
    struct native_bridge_namespace_t* parent_ns) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->createNamespace)
               ? r->createNamespace(name, ld_library_path, default_library_path, type,
                                    permitted_when_isolated_path, parent_ns)
               : NULL;
}

static bool shim_link_namespaces(struct native_bridge_namespace_t* from,
                                 struct native_bridge_namespace_t* to,
                                 const char* shared_libs_sonames) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->linkNamespaces) ? r->linkNamespaces(from, to, shared_libs_sonames) : false;
}

static void* shim_load_library_ext(const char* libpath,
                                   int flag,
                                   struct native_bridge_namespace_t* ns) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->loadLibraryExt) ? r->loadLibraryExt(libpath, flag, ns) : NULL;
}

static struct native_bridge_namespace_t* shim_get_vendor_namespace(void) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->getVendorNamespace) ? r->getVendorNamespace() : NULL;
}

static struct native_bridge_namespace_t* shim_get_exported_namespace(const char* name) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->getExportedNamespace) ? r->getExportedNamespace(name) : NULL;
}

static void shim_pre_zygote_fork(void) {
    const struct NativeBridgeCallbacksV6* r = get_real();
    if (r && r->preZygoteFork) {
        r->preZygoteFork();
    }
}

/* ---- v7 & v8 compatibility layer for AOSP 16 ART ---- */

static void* shim_get_trampoline_with_jni_call_type(void* handle,
                                                    const char* name,
                                                    const char* shorty,
                                                    uint32_t len,
                                                    uint32_t jni_call_type) {
    (void)jni_call_type;
    const struct NativeBridgeCallbacksV6* r = get_real();
    return (r && r->getTrampoline) ? r->getTrampoline(handle, name, shorty, len) : NULL;
}

static void* shim_get_trampoline_for_function_pointer(const void* method,
                                                      const char* shorty,
                                                      uint32_t len,
                                                      uint32_t jni_call_type) {
    (void)shorty;
    (void)len;
    (void)jni_call_type;
    if (atomic_exchange(&g_logged_fnptr, 1) == 0) {
        ALOGI("getTrampolineForFunctionPointer active (pass-through Houdini host trampoline %p)",
              method);
    }
    /* Houdini already wrapped guest fnPtr into a host x86/x86_64 trampoline
     * inside its JNIEnv->RegisterNatives proxy before calling ART. */
    return (void*)method;
}

static bool shim_is_native_bridge_function_pointer(const void* method) {
    (void)method;
    return false;
}

/* Exported NativeBridge v8 descriptor */
__attribute__((visibility("default")))
struct NativeBridgeCallbacksV8 NativeBridgeItf = {
    .version = 8,
    .initialize = shim_initialize,
    .loadLibrary = shim_load_library,
    .getTrampoline = shim_get_trampoline,
    .isSupported = shim_is_supported,
    .getAppEnv = shim_get_app_env,
    .isCompatibleWith = shim_is_compatible_with,
    .getSignalHandler = shim_get_signal_handler,
    .unloadLibrary = shim_unload_library,
    .getError = shim_get_error,
    .isPathSupported = shim_is_path_supported,
    .initAnonymousNamespace = shim_init_anonymous_namespace,
    .createNamespace = shim_create_namespace,
    .linkNamespaces = shim_link_namespaces,
    .loadLibraryExt = shim_load_library_ext,
    .getVendorNamespace = shim_get_vendor_namespace,
    .getExportedNamespace = shim_get_exported_namespace,
    .preZygoteFork = shim_pre_zygote_fork,
    .getTrampolineWithJNICallType = shim_get_trampoline_with_jni_call_type,
    .getTrampolineForFunctionPointer = shim_get_trampoline_for_function_pointer,
    .isNativeBridgeFunctionPointer = shim_is_native_bridge_function_pointer,
};

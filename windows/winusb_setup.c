/* ELEVATED boundary. Fixed Microsoft inbox WinUSB only, for one verified present
 * 18d1:2d01 MI_00 child. Never load application code or accept a path to an INF,
 * DLL, executable, report or registry key. No matching-ID/global update.
 * API: https://learn.microsoft.com/windows-hardware/drivers/usbcon/winusb-installation
 */
#define UNICODE
#define _UNICODE
#define WIN32_LEAN_AND_MEAN
#define INITGUID
#include <windows.h>
#include <setupapi.h>
#include <newdev.h>
#include <cfgmgr32.h>
#include <devpkey.h>
#include <shellapi.h>
#include "winusb_setup_policy.h"
/* Not declared in MinGW-w64 v13 setupapi.h; documented Win32 signature. */
extern BOOL WINAPI SetupGetInfDriverStoreLocationW(PCWSTR, PSP_ALTPLATFORM_INFO, PCWSTR, PWSTR, DWORD,
                                                   PDWORD);

#define APP_GUID L"{6A84F459-3DD8-4692-B430-C4993595BB6B}"
#define MARK L"CarLifeHeadUnitWinUSBSetupV1"
#define OWN_GUID 1u
#define OWN_BIND 2u
#define HAD_BIND 4u
#define NEW_MULTI 8u
#define BIND_INTENT 16u
#define OK 0
#define READY 10
#define RECONNECT 11
#define REBOOT 12
#define CANCELLED 13
#define BAD_TARGET 20
#define NO_INBOX 21
#define DENIED 22
#define POST_FAILED 23
#define NO_ROLLBACK 24
#define WCOUNT(a) ((unsigned)(sizeof(a) / sizeof((a)[0])))

/* Freestanding x64 binary. The only static imports are Kernel32. */
void *memset(void *p, int v, size_t n) {
    volatile unsigned char *q = p;
    while (n--)
        *q++ = (unsigned char)v;
    return p;
}
void *memcpy(void *p, const void *v, size_t n) {
    volatile unsigned char *q = p;
    const unsigned char *s = v;
    while (n--)
        *q++ = *s++;
    return p;
}
#define CM_FNS(X)                                                                                       \
    X(CM_Get_Device_IDW)                                                                                \
    X(CM_Locate_DevNodeW)                                                                               \
    X(CM_Get_Parent)                                                                                    \
    X(CM_Get_Child) X(CM_Get_Sibling) X(CM_Get_DevNode_Registry_PropertyW) X(CM_Get_DevNode_PropertyW)  \
        X(CM_Get_DevNode_Status)
#define SP_FNS(X)                                                                                       \
    X(SetupDiCreateDeviceInfoList)                                                                      \
    X(SetupDiDestroyDeviceInfoList)                                                                     \
    X(SetupDiGetDeviceInstallParamsW)                                                                   \
    X(SetupDiSetDeviceInstallParamsW) X(SetupDiBuildDriverInfoList) X(SetupDiOpenDeviceInfoW)           \
        X(SetupDiEnumDriverInfoW) X(SetupDiGetDriverInfoDetailW) X(SetupDiOpenDevRegKey)                \
            X(SetupGetInfDriverStoreLocationW) X(SetupVerifyInfFileW) X(SetupOpenInfFileW)              \
                X(SetupCloseInfFile) X(SetupDiGetActualSectionToInstallW) X(SetupFindFirstLineW)        \
                    X(SetupGetStringFieldW) X(SetupGetIntField)
#define ND_FNS(X) X(DiInstallDevice) X(DiRollbackDriver)
#define REG_FNS(X) X(RegQueryValueExW) X(RegSetValueExW) X(RegDeleteValueW) X(RegCloseKey)
#define SHELL_FNS(X) X(CommandLineToArgvW)
#define DECL(name) static __typeof__(&name) p_##name;
CM_FNS(DECL)
SP_FNS(DECL)
ND_FNS(DECL)
REG_FNS(DECL) SHELL_FNS(DECL) static int join(WCHAR *dst, unsigned capacity, const WCHAR *left,
                                              const WCHAR *right) {
    unsigned l = wlen(left), r = wlen(right);
    if (l + r >= capacity)
        return 0;
    for (unsigned i = 0; i < l; i++)
        dst[i] = left[i];
    for (unsigned i = 0; i <= r; i++)
        dst[l + i] = right[i];
    return 1;
}
static HMODULE system_dll(const WCHAR *name) {
    WCHAR dir[MAX_PATH], file[MAX_PATH];
    unsigned n = GetSystemDirectoryW(dir, WCOUNT(dir));
    if (!n || n >= WCOUNT(dir) || !join(file, WCOUNT(file), dir, name))
        return NULL;
    return LoadLibraryExW(file, NULL, LOAD_LIBRARY_SEARCH_SYSTEM32);
}
static int bind_apis(void) {
    HMODULE lib;
#define BIND(name)                                                                                      \
    p_##name = (__typeof__(&name))(void *)GetProcAddress(lib, #name);                                   \
    if (!p_##name)                                                                                      \
        return 0;
#define DLL(name, funcs)                                                                                \
    lib = system_dll(L"\\" name);                                                                       \
    if (!lib)                                                                                           \
        return 0;                                                                                       \
    funcs(BIND)
    DLL(L"cfgmgr32.dll", CM_FNS)
    DLL(L"setupapi.dll", SP_FNS)
    DLL(L"newdev.dll", ND_FNS)
    DLL(L"advapi32.dll", REG_FNS) DLL(L"shell32.dll", SHELL_FNS)
#undef DLL
#undef BIND
        return 1;
}

static struct {
    DEVINST parent, child, sibling;
    unsigned ports[7], nports;
    WCHAR parent_id[MAX_DEVICE_ID_LEN], child_id[MAX_DEVICE_ID_LEN], sibling_id[MAX_DEVICE_ID_LEN];
} target;
static WCHAR propbuf[8192], idbuf[MAX_DEVICE_ID_LEN], servicebuf[256];
/* Verify MULTI_SZ termination, even when a provider returns malformed data. */
static int prop(DEVINST dev, ULONG key, ULONG expected, WCHAR *out, unsigned capacity, int optional) {
    ULONG n = capacity * sizeof(WCHAR), type = 0;
    CONFIGRET rc = p_CM_Get_DevNode_Registry_PropertyW(dev, key, &type, out, &n, 0);
    if (optional && rc == CR_NO_SUCH_VALUE) {
        out[0] = out[1] = 0;
        return 1;
    }
    if (rc != CR_SUCCESS || type != expected || n < sizeof(WCHAR) || n % sizeof(WCHAR) ||
        n > capacity * sizeof(WCHAR))
        return 0;
    n /= sizeof(WCHAR);
    return expected == REG_MULTI_SZ ? multi_ok(out, n) : out[n - 1] == 0 && wlen(out) + 1 == n;
}
static int live_id(DEVINST dev, WCHAR *out) {
    DEVINST found = 0;
    ULONG status, problem;
    if (p_CM_Get_Device_IDW(dev, out, MAX_DEVICE_ID_LEN, 0) != CR_SUCCESS || !out[0])
        return 0;
    return p_CM_Locate_DevNodeW(&found, out, CM_LOCATE_DEVNODE_NORMAL) == CR_SUCCESS && found == dev &&
           p_CM_Get_DevNode_Status(&status, &problem, dev, 0) == CR_SUCCESS;
}
static int hardware(DEVINST node, int mi) {
    if (!prop(node, CM_DRP_HARDWAREID, REG_MULTI_SZ, propbuf, WCOUNT(propbuf), 0) || !propbuf[0])
        return 0;
    for (const WCHAR *s = propbuf; *s; s += wlen(s) + 1)
        if (!hw(s, wlen(s), mi))
            return 0;
    return 1;
}
static int profile(DEVINST node, int adb) {
    const WCHAR *full =
        adb ? L"USB\\Class_FF&SubClass_42&Prot_01" : L"USB\\Class_FF&SubClass_FF&Prot_00";
    const WCHAR *sub = adb ? L"USB\\Class_FF&SubClass_42" : L"USB\\Class_FF&SubClass_FF";
    int found = 0;
    if (!prop(node, CM_DRP_COMPATIBLEIDS, REG_MULTI_SZ, propbuf, WCOUNT(propbuf), 0))
        return 0;
    for (const WCHAR *s = propbuf; *s; s += wlen(s) + 1) {
        if (eq(s, full))
            found = 1;
        else if (begins(s, L"USB\\Class_") && !eq(s, sub) && !eq(s, L"USB\\Class_FF"))
            return 0;
    }
    return found;
}
static int adb_child(DEVINST node, const WCHAR *instance) {
    if (!profile(node, 1))
        return 0;
    if (instance_hw(instance, 1) && hardware(node, 1))
        return 1;
    /* Explicit Samsung ADB function, never the installation target. */
    if (!begins(instance, L"USB\\VID_18D1&PID_2D01&ADB\\"))
        return 0;
    if (!prop(node, CM_DRP_HARDWAREID, REG_MULTI_SZ, propbuf, WCOUNT(propbuf), 0) || !propbuf[0])
        return 0;
    for (const WCHAR *s = propbuf; *s; s += wlen(s) + 1)
        if (!eq(s, L"USB\\VID_18D1&PID_2D01&ADB"))
            return 0;
    return 1;
}
static int ports_correct(DEVINST dev) {
    int found = 0;
    if (!prop(dev, CM_DRP_LOCATION_PATHS, REG_MULTI_SZ, propbuf, WCOUNT(propbuf), 0))
        return 0;
    for (const WCHAR *s = propbuf; *s; s += wlen(s) + 1) {
        int contains = 0;
        for (const WCHAR *t = s; *t; t++) {
            if ((t == s || t[-1] == L'#') && begins(t, L"USB(")) {
                contains = 1;
                break;
            }
        }
        if (contains) {
            if (!port_match(s, target.ports, target.nports))
                return 0;
            found = 1;
        }
    }
    return found;
}
static int validate(int first) {
    DEVINST parent, child;
    int seen = 0, adbs = 0;
    CONFIGRET rc;
    unsigned visited = 0;
    if (target.parent == target.child || !live_id(target.parent, idbuf) || !instance_hw(idbuf, -1) ||
        !hardware(target.parent, -1) || !ports_correct(target.parent))
        return 0;
    if (first)
        memcpy(target.parent_id, idbuf, (wlen(idbuf) + 1) * sizeof(WCHAR));
    else if (!eq(target.parent_id, idbuf))
        return 0;
    if (!prop(target.parent, CM_DRP_SERVICE, REG_SZ, servicebuf, WCOUNT(servicebuf), 0) ||
        !(eq(servicebuf, L"usbccgp") || eq(servicebuf, L"dg_ssudbus")))
        return 0;
    rc = p_CM_Get_Child(&child, target.parent, 0);
    while (rc == CR_SUCCESS) {
        if (++visited > 64 || p_CM_Get_Parent(&parent, child, 0) != CR_SUCCESS ||
            parent != target.parent || !live_id(child, idbuf))
            return 0;
        if (instance_hw(idbuf, 0)) {
            if (child != target.child || !hardware(child, 0) || !profile(child, 0))
                return 0;
            if (first)
                memcpy(target.child_id, idbuf, (wlen(idbuf) + 1) * sizeof(WCHAR));
            else if (!eq(target.child_id, idbuf))
                return 0;
            seen++;
        } else if (adb_child(child, idbuf)) {
            if (first) {
                target.sibling = child;
                memcpy(target.sibling_id, idbuf, (wlen(idbuf) + 1) * sizeof(WCHAR));
            } else if (child != target.sibling || !eq(target.sibling_id, idbuf))
                return 0;
            adbs++;
        }
        rc = p_CM_Get_Sibling(&child, child, 0);
    }
    return rc == CR_NO_SUCH_DEVNODE && seen == 1 && adbs == 1;
}

static WCHAR sysinf[MAX_PATH], storeinf[MAX_PATH];
static SP_INF_SIGNER_INFO_W signer;
static int inbox_package(void) {
    WCHAR dir[MAX_PATH];
    unsigned n = GetSystemWindowsDirectoryW(dir, WCOUNT(dir));
    if (!n || n >= WCOUNT(dir) || !join(sysinf, WCOUNT(sysinf), dir, L"\\INF\\winusb.inf"))
        return 0;
    if (!p_SetupGetInfDriverStoreLocationW(sysinf, NULL, NULL, storeinf, WCOUNT(storeinf), NULL))
        return 0;
    WCHAR repository[MAX_PATH];
    if (!join(repository, WCOUNT(repository), dir,
              L"\\System32\\DriverStore\\FileRepository\\winusb.inf_"))
        return 0;
    if (!begins(storeinf, repository))
        return 0;
    const WCHAR *base = storeinf;
    for (const WCHAR *s = storeinf; *s; s++)
        if (*s == L'\\')
            base = s + 1;
    if (!eq(base, L"winusb.inf"))
        return 0;
    signer.cbSize = sizeof(signer);
    if (!p_SetupVerifyInfFileW(sysinf, NULL, &signer)) {
        signer.cbSize = sizeof(signer);
        if (!p_SetupVerifyInfFileW(storeinf, NULL, &signer))
            return 0;
    }
    if (!(eq(signer.DigitalSigner, L"Microsoft Windows") ||
          eq(signer.DigitalSigner, L"Microsoft Windows Publisher") ||
          eq(signer.DigitalSigner, L"Microsoft Windows Hardware Compatibility Publisher") ||
          eq(signer.DigitalSigner, L"Microsoft Corporation")))
        return 0;
    return 1;
}
static int winusb_service_section(const WCHAR *section) {
    WCHAR decorated[256], services[280], service[256];
    INFCONTEXT ctx;
    int flags = 0;
    HINF inf = p_SetupOpenInfFileW(sysinf, L"USBDevice", INF_STYLE_WIN4, NULL);
    if (inf == INVALID_HANDLE_VALUE)
        return 0;
    int verified =
        p_SetupDiGetActualSectionToInstallW(inf, L"WINUSB", decorated, WCOUNT(decorated), NULL, NULL) &&
        (eq(section, L"WINUSB") || eq(section, decorated)) &&
        join(services, WCOUNT(services), decorated, L".Services") &&
        p_SetupFindFirstLineW(inf, services, L"AddService", &ctx) &&
        p_SetupGetStringFieldW(&ctx, 1, service, WCOUNT(service), NULL) && eq(service, L"WinUSB") &&
        p_SetupGetIntField(&ctx, 2, &flags) && (flags & SPSVCINST_ASSOCSERVICE);
    p_SetupCloseInfFile(inf);
    return verified;
}
static SP_DEVINSTALL_PARAMS_W params;
static SP_DRVINFO_DATA_W chosen;
static union {
    ULONG_PTR align;
    BYTE bytes[16384];
} detail_storage;
static int choose_driver(HDEVINFO set, SP_DEVINFO_DATA *dev) {
    params.cbSize = sizeof(params);
    if (!p_SetupDiGetDeviceInstallParamsW(set, NULL, &params))
        return 0;
    params.Flags |= DI_ENUMSINGLEINF;
    /* Microsoft requires this flag to enumerate class nodes for a PnP
     * device. Only the one catalog-verified Microsoft inbox INF is searched. */
    params.FlagsEx |= DI_FLAGSEX_ALLOWEXCLUDEDDRVS;
    if (!join(params.DriverPath, MAX_PATH, sysinf, L"") ||
        !p_SetupDiSetDeviceInstallParamsW(set, NULL, &params) ||
        !p_SetupDiBuildDriverInfoList(set, NULL, SPDIT_CLASSDRIVER))
        return 0;
    dev->cbSize = sizeof(*dev);
    if (!p_SetupDiOpenDeviceInfoW(set, target.child_id, NULL, DIOD_INHERIT_CLASSDRVS, dev) ||
        dev->DevInst != target.child)
        return 0;
    unsigned found = 0;
    for (DWORD i = 0; i < 128; i++) {
        static SP_DRVINFO_DATA_W node;
        node.cbSize = sizeof(node);
        if (!p_SetupDiEnumDriverInfoW(set, dev, SPDIT_CLASSDRIVER, i, &node))
            return GetLastError() == ERROR_NO_MORE_ITEMS && found == 1;
        SP_DRVINFO_DETAIL_DATA_W *d = (void *)detail_storage.bytes;
        d->cbSize = sizeof(*d);
        DWORD needed = 0;
        if (!p_SetupDiGetDriverInfoDetailW(set, dev, &node, d, sizeof(detail_storage), &needed))
            return 0;
        /* Only an actually offered Microsoft model from the precise, verified
         * inbox INF. Do not force an arbitrary OEM INF or add another match. */
        if (!(eq(d->InfFileName, sysinf) || eq(d->InfFileName, storeinf)) ||
            !(eq(node.ProviderName, L"Microsoft") || eq(node.ProviderName, L"Microsoft Corporation")) ||
            !eq(d->HardwareID, L"USB\\MS_COMP_WINUSB") || !winusb_service_section(d->SectionName))
            continue;
        chosen = node;
        found++;
    }
    return 0;
}

/* Use the OS-opened hardware key for the verified SP_DEVINFO_DATA. Do not
 * construct a registry path from an argument. Existing GUIDs are preserved. */
#include "winusb_setup_guid.h"
static int binding(int *is_winusb, int *has_driver) {
    if (!prop(target.child, CM_DRP_SERVICE, REG_SZ, servicebuf, WCOUNT(servicebuf), 1))
        return 0;
    *has_driver = servicebuf[0] != 0;
    *is_winusb = eq(servicebuf, L"WinUSB");
    for (int i = 0; i < 2; i++) {
        if (!prop(target.child, i ? CM_DRP_UPPERFILTERS : CM_DRP_LOWERFILTERS, REG_MULTI_SZ, propbuf,
                  WCOUNT(propbuf), 1))
            return 0;
        if (contains(propbuf, L"WinUSB"))
            *is_winusb = 1;
    }
    return 1;
}
static int current_inbox(void) {
    DEVPROPTYPE type = 0;
    ULONG n = sizeof(propbuf);
    if (!prop(target.child, CM_DRP_SERVICE, REG_SZ, servicebuf, WCOUNT(servicebuf), 0) ||
        !eq(servicebuf, L"WinUSB"))
        return 0;
    if (p_CM_Get_DevNode_PropertyW(target.child, &DEVPKEY_Device_DriverInfPath, &type, (BYTE *)propbuf,
                                   &n, 0) != CR_SUCCESS ||
        type != DEVPROP_TYPE_STRING || n < 2 || n > sizeof(propbuf) || n % 2 || propbuf[n / 2 - 1] != 0)
        return 0;
    return eq(propbuf, L"winusb.inf");
}
static int run(int mode) {
    /* Mode 1 installs a driver; mode 2 can change GUIDs only; mode 3 removes
     * only a binding/GUID this helper recorded. No backup means no rollback. */
    if (!validate(1))
        return BAD_TARGET;
    int bound = 0, had_driver = 0;
    if (!binding(&bound, &had_driver))
        return BAD_TARGET;
    if ((mode == 1 && bound) || (mode == 2 && !bound))
        return BAD_TARGET;
    if (!inbox_package())
        return NO_INBOX;
    HDEVINFO set = p_SetupDiCreateDeviceInfoList(NULL, NULL);
    if (set == INVALID_HANDLE_VALUE)
        return NO_INBOX;
    SP_DEVINFO_DATA device = {0};
    device.cbSize = sizeof(device);
    HKEY key = INVALID_HANDLE_VALUE;
    int result = NO_INBOX;
    DWORD state = 0;
    if (mode == 1) {
        if (!choose_driver(set, &device))
            goto done;
    } else if (!p_SetupDiOpenDeviceInfoW(set, target.child_id, NULL, 0, &device) ||
               device.DevInst != target.child) {
        result = BAD_TARGET;
        goto done;
    }
    if (!validate(0)) {
        result = BAD_TARGET;
        goto done;
    }
    key = p_SetupDiOpenDevRegKey(set, &device, DICS_FLAG_GLOBAL, 0, DIREG_DEV,
                                 KEY_QUERY_VALUE | KEY_SET_VALUE);
    if (key == INVALID_HANDLE_VALUE) {
        result = DENIED;
        goto done;
    }
    if (!get_mark(key, &state)) {
        result = POST_FAILED;
        goto done;
    }
    DWORD before = state;
    BOOL reboot = FALSE;
    /* If PnP invalidated the handle during a previous install, an intent
     * is not proof of success. Only promote it on the same revalidated child
     * with the actual Microsoft inbox function driver now installed. */
    if (state & BIND_INTENT) {
        if (!current_inbox() || !validate(0)) {
            result = mode == 3 ? NO_ROLLBACK : RECONNECT;
            goto done;
        }
        state = (state & ~BIND_INTENT) | OWN_BIND;
        if (!set_mark(key, state)) {
            result = POST_FAILED;
            goto done;
        }
        before = state;
    }
    if (mode == 3) {
        if (!state) {
            result = NO_ROLLBACK;
            goto done;
        }
        if (state & OWN_BIND) {
            if (!(state & HAD_BIND) || !current_inbox() || !validate(0)) {
                result = NO_ROLLBACK;
                goto done;
            }
            if (!p_DiRollbackDriver(set, &device, NULL, 0, &reboot)) {
                result = GetLastError() == ERROR_CANCELLED ? CANCELLED : NO_ROLLBACK;
                goto done;
            }
            state &= ~(OWN_BIND | HAD_BIND);
            if (!validate(0) || !set_mark(key, state)) {
                result = POST_FAILED;
                goto done;
            }
        }
        if (!remove_guid(key, &state)) {
            result = POST_FAILED;
            goto done;
        }
        result = reboot ? REBOOT : RECONNECT;
        goto done;
    }
    /* No mutation until the exact child has been revalidated and, in
     * install mode, its explicit verified inbox model has been selected. */
    if (!validate(0) || !binding(&bound, &had_driver) || (mode == 1 && bound) || (mode == 2 && !bound)) {
        result = BAD_TARGET;
        goto done;
    }
    if (!add_guid(key, &state)) {
        result = POST_FAILED;
        goto done;
    }
    if (mode == 2) {
        DWORD units;
        int exists;
        if (!validate(0) || !binding(&bound, &had_driver) || !bound ||
            !guid_list(key, &units, &exists) || !(contains(guids, APP_GUID) || eq(old_single, APP_GUID)))
            result = POST_FAILED;
        else
            result = before == state ? READY : RECONNECT;
        goto done;
    }
    if (!validate(0) || !binding(&bound, &had_driver) || bound) {
        result = BAD_TARGET;
        goto undo_guid;
    }
    DWORD prebind = state;
    state |= BIND_INTENT | (had_driver ? HAD_BIND : 0);
    if (!set_mark(key, state)) {
        state = prebind;
        result = POST_FAILED;
        goto undo_guid;
    }
    if (!validate(0) || !binding(&bound, &had_driver) || bound) {
        result = BAD_TARGET;
        goto cancel_intent;
    }
    if (!p_DiInstallDevice(NULL, set, &device, &chosen, 0, &reboot)) {
        DWORD error = GetLastError();
        if (!validate(0) || current_inbox()) {
            result = POST_FAILED;
            goto done;
        }
        result = error == ERROR_CANCELLED ? CANCELLED : DENIED;
        goto cancel_intent;
    }
    /* Deferred PnP completion can require reconnect/reboot. Never select a
     * replacement device if the original identity disappears meanwhile. */
    if (!validate(0)) {
        result = reboot ? REBOOT : RECONNECT;
        goto done;
    }
    state = (state & ~BIND_INTENT) | OWN_BIND;
    if (!set_mark(key, state)) {
        result = POST_FAILED;
        goto done;
    }
    result = reboot ? REBOOT : (current_inbox() ? OK : RECONNECT);
    goto done;
cancel_intent:
    state = prebind;
    if (!set_mark(key, state)) {
        result = POST_FAILED;
        goto done;
    }
undo_guid:
    if (!(before & OWN_GUID) && (state & OWN_GUID) && !remove_guid(key, &state))
        result = POST_FAILED;
done:
    if (key != INVALID_HANDLE_VALUE)
        p_RegCloseKey(key);
    p_SetupDiDestroyDeviceInfoList(set);
    return result;
}
void mainCRTStartup(void) {
    /* Constrain dependent DLL resolution as well as the explicitly absolute
     * paths in system_dll(). This changes only this helper process. */
    if (!SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_SYSTEM32))
        ExitProcess(NO_INBOX);
    if (!bind_apis())
        ExitProcess(NO_INBOX);
    int argc = 0;
    WCHAR **argv = p_CommandLineToArgvW(GetCommandLineW(), &argc);
    if (!argv)
        ExitProcess(BAD_TARGET);
    if (argc == 2 && eq(argv[1], L"--verify-api")) {
        LocalFree(argv);
        ExitProcess(0);
    }
    int mode = argc == 5 && eq(argv[1], L"--install-mi00")    ? 1
               : argc == 5 && eq(argv[1], L"--register-mi00") ? 2
               : argc == 5 && eq(argv[1], L"--rollback-mi00") ? 3
                                                              : 0;
    unsigned a = 0, b = 0;
    int good = mode && token(argv[2], &a) && token(argv[3], &b) && a != b &&
               port_list(argv[4], target.ports, &target.nports);
    LocalFree(argv);
    if (!good)
        ExitProcess(BAD_TARGET);
    target.parent = (DEVINST)a;
    target.child = (DEVINST)b;
    ExitProcess((UINT)run(mode));
}

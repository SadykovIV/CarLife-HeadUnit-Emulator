/* Compile and exercise the production C policy and GUID transaction on an
 * in-memory registry. No device, Windows API, subprocess installer or UAC. */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#include <wchar.h>
#include <stdio.h>
#include "../../windows/winusb_setup_policy.h"
typedef wchar_t WCHAR;
typedef uint32_t DWORD;
typedef long LONG;
typedef unsigned char BYTE;
typedef int HKEY;
#define REG_SZ 1
#define REG_DWORD 4
#define REG_MULTI_SZ 7
#define ERROR_SUCCESS 0
#define ERROR_FILE_NOT_FOUND 2
#define WCOUNT(a) (sizeof(a) / sizeof((a)[0]))
#define APP_GUID L"{6A84F459-3DD8-4692-B430-C4993595BB6B}"
#define MARK L"CarLifeHeadUnitWinUSBSetupV1"
#define OWN_GUID 1u
#define OWN_BIND 2u
#define HAD_BIND 4u
#define NEW_MULTI 8u

static struct {
    int exists;
    DWORD type, n;
    BYTE data[4096];
} values[3];
static const WCHAR *names[] = {MARK, L"DeviceInterfaceGUID", L"DeviceInterfaceGUIDs"};
static const WCHAR *fail_set;
static int calls, valid = 1;
static int validate(int first) {
    (void)first;
    return valid;
}
static int slot(const WCHAR *name) {
    for (int i = 0; i < 3; i++)
        if (eq(names[i], name))
            return i;
    return -1;
}
static LONG p_RegQueryValueExW(HKEY key, const WCHAR *name, void *r, DWORD *t, BYTE *out, DWORD *size) {
    (void)r;
    int i = slot(name);
    if (key != 7 || i < 0)
        return 5;
    if (!values[i].exists)
        return ERROR_FILE_NOT_FOUND;
    if (*size < values[i].n)
        return 234;
    *size = values[i].n;
    *t = values[i].type;
    memcpy(out, values[i].data, *size);
    return 0;
}
static LONG p_RegSetValueExW(HKEY key, const WCHAR *name, DWORD r, DWORD t, const BYTE *in, DWORD size) {
    (void)r;
    calls++;
    int i = slot(name);
    if (key != 7 || i < 0 || size > sizeof(values[i].data) || (fail_set && eq(fail_set, name)))
        return 5;
    values[i].n = size;
    values[i].type = t;
    values[i].exists = 1;
    memcpy(values[i].data, in, size);
    return 0;
}
static LONG p_RegDeleteValueW(HKEY key, const WCHAR *name) {
    calls++;
    int i = slot(name);
    if (key != 7 || i < 0)
        return 5;
    if (!values[i].exists)
        return ERROR_FILE_NOT_FOUND;
    values[i].exists = 0;
    return 0;
}
#include "../../windows/winusb_setup_guid.h"
#define CHECK(x)                                                                                        \
    do {                                                                                                \
        if (!(x)) {                                                                                     \
            fprintf(stderr, "failed: %s at line %d\n", #x, __LINE__);                                   \
            return 1;                                                                                   \
        }                                                                                               \
    } while (0)

int main(void) {
    unsigned node = 0, n = 0, ports[7];
    CHECK(token(L"4294967295", &node) && node == UINT32_MAX);
    CHECK(!token(L"4294967296", &node) && !token(L"01", &node) && !token(L"0", &node));
    CHECK(port_list(L"1.7", ports, &n) && n == 2 &&
          port_match(L"PCIROOT(0)#USBROOT(0)#USB(1)#USB(7)", ports, n));
    CHECK(!port_match(L"PCIROOT(0)#USBROOT(0)#USB(1)#USB(8)", ports, n));
    CHECK(!port_list(L"1.0", ports, &n) && !port_list(L"1.7.", ports, &n) &&
          !port_list(L"1.256", ports, &n));
    CHECK(instance_hw(L"USB\\VID_18D1&PID_2D01&MI_00\\private", 0));
    CHECK(instance_hw(L"USB\\VID_18D1&PID_2D01&REV_0100&MI_00\\private", 0));
    CHECK(instance_hw(L"USB\\VID_18D1&PID_2D01\\private", -1));
    CHECK(!instance_hw(L"USB\\VID_18D1&PID_2D01\\parent&MI_00", 0));
    CHECK(!instance_hw(L"USB\\VID_18D1&PID_2D01&ADB\\private", 0));
    CHECK(!instance_hw(L"USB\\VID_18D1&PID_2D01&MI_01\\private", 0));
    CHECK(!instance_hw(L"USB\\VID_04E8&PID_6860&MI_00\\private", 0));
    CHECK(!instance_hw(L"USB\\VID_18D1&PID_2D01&MI_00&ADB\\private", 0));
    CHECK(!multi_ok(L"ok\0\0hidden\0", 12));

    DWORD state = 0;
    const WCHAR singular[] = L"{12345678-0000-0000-0000-000000000001}";
    const WCHAR plural[] = L"{12345678-0000-0000-0000-000000000002}\0";
    const WCHAR unrelated[] = L"{12345678-0000-0000-0000-000000000003}";
    CHECK(!p_RegSetValueExW(7, names[1], 0, REG_SZ, (const BYTE *)singular, sizeof(singular)));
    CHECK(!p_RegSetValueExW(7, names[2], 0, REG_MULTI_SZ, (const BYTE *)plural, sizeof(plural)));
    calls = 0;
    fail_set = MARK;
    CHECK(!add_guid(7, &state) && state == 0 && values[2].n == sizeof(plural) &&
          !memcmp(values[2].data, plural, sizeof(plural)));
    fail_set = names[2];
    CHECK(!add_guid(7, &state) && state == 0 && !values[0].exists &&
          !memcmp(values[2].data, plural, sizeof(plural)));
    fail_set = NULL;
    valid = 0;
    CHECK(!add_guid(7, &state) && !values[0].exists && values[2].n == sizeof(plural));
    valid = 1;
    CHECK(add_guid(7, &state) && state == OWN_GUID && get_mark(7, &state));
    CHECK(contains((const WCHAR *)values[2].data, singular) &&
          contains((const WCHAR *)values[2].data, plural));
    CHECK(contains((const WCHAR *)values[2].data, APP_GUID));
    int before = calls;
    CHECK(add_guid(7, &state) && calls == before);
    /* Simulate a later GUID being registered by a different application. */
    unsigned used = values[2].n / sizeof(WCHAR) - 1;
    memcpy(values[2].data + used * sizeof(WCHAR), unrelated, sizeof(unrelated));
    values[2].n += (DWORD)sizeof(unrelated);
    memset(values[2].data + values[2].n - sizeof(WCHAR), 0, sizeof(WCHAR));
    CHECK(remove_guid(7, &state) && state == 0 && !values[0].exists);
    CHECK(contains((const WCHAR *)values[2].data, unrelated) &&
          contains((const WCHAR *)values[2].data, plural));
    CHECK(!contains((const WCHAR *)values[2].data, APP_GUID) &&
          !memcmp(values[1].data, singular, sizeof(singular)));
    CHECK(!p_RegDeleteValueW(7, names[2]));
    CHECK(add_guid(7, &state) && state == (OWN_GUID | NEW_MULTI));
    CHECK(remove_guid(7, &state) && !values[2].exists && values[1].exists);
    CHECK(!p_RegSetValueExW(7, names[1], 0, REG_SZ, (const BYTE *)APP_GUID, sizeof(APP_GUID)));
    before = calls;
    CHECK(add_guid(7, &state) && !state && calls == before);
    return 0;
}

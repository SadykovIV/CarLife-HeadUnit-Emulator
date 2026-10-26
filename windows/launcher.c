/* Small dependency-free Windows x64 GUI launcher for the bundled interpreter. */
typedef unsigned short WCHAR;
typedef unsigned long DWORD;
typedef int BOOL;
typedef void *HANDLE;
typedef struct { DWORD cb; WCHAR *reserved, *desktop, *title; DWORD x, y, width, height,
    chars_x, chars_y, fill, flags; unsigned short show, reserved_size;
    unsigned char *reserved_bytes; HANDLE input, output, error; } STARTUPINFOW;
typedef struct { HANDLE process, thread; DWORD process_id, thread_id; } PROCESS_INFORMATION;
#define IMPORT __declspec(dllimport)
IMPORT DWORD GetModuleFileNameW(HANDLE, WCHAR *, DWORD);
IMPORT WCHAR *GetCommandLineW(void);
IMPORT BOOL CreateProcessW(const WCHAR *, WCHAR *, void *, void *, BOOL, DWORD, void *, const WCHAR *, STARTUPINFOW *, PROCESS_INFORMATION *);
IMPORT DWORD WaitForSingleObject(HANDLE, DWORD);
IMPORT BOOL GetExitCodeProcess(HANDLE, DWORD *);
IMPORT BOOL CloseHandle(HANDLE);
IMPORT void ExitProcess(DWORD);
IMPORT int MessageBoxW(HANDLE, const WCHAR *, const WCHAR *, unsigned int);
static WCHAR base[32768], executable[32768], command[32768];
static STARTUPINFOW startup;
static PROCESS_INFORMATION child;
static unsigned int append(WCHAR *out, unsigned int used, const WCHAR *input) {
    while (*input && used < 32766) out[used++] = *input++;
    out[used] = 0;
    if (*input) ExitProcess(2);
    return used;
}
void mainCRTStartup(void) {
    DWORD size = GetModuleFileNameW(0, base, 32768), exit_code = 1;
    if (!size || size >= 32767) ExitProcess(2);
    while (size && base[size - 1] != '\\') --size;
    if (!size) ExitProcess(2);
    base[size] = 0;
    append(executable, append(executable, 0, base), L"runtime\\pythonw.exe");
    unsigned int used = append(command, 0, L"\"");
    used = append(command, used, executable);
    used = append(command, used, L"\" -X utf8 \"");
    used = append(command, used, base);
    used = append(command, used, L"entry.py\" ");
    WCHAR *tail = GetCommandLineW();
    if (*tail == '"') { ++tail; while (*tail && *tail != '"') ++tail; if (*tail) ++tail; }
    else while (*tail && *tail != ' ' && *tail != '\t') ++tail;
    while (*tail == ' ' || *tail == '\t') ++tail;
    append(command, used, tail);
    startup.cb = sizeof(startup);
    if (!CreateProcessW(executable, command, 0, 0, 0, 0, 0, base, &startup, &child)) {
        MessageBoxW(0, L"Cannot start bundled Python. Extract the complete ZIP archive, then run CarLifeHeadUnitEmulator.exe.", L"CarLife HeadUnit Emulator", 0x10);
        ExitProcess(1);
    }
    CloseHandle(child.thread);
    WaitForSingleObject(child.process, 0xffffffff);
    GetExitCodeProcess(child.process, &exit_code);
    CloseHandle(child.process);
    ExitProcess(exit_code);
}

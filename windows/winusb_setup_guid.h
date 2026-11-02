/* Exact hardware-key GUID transaction; also compiled against the in-memory registry tests. */
#define GMAX 8192
static WCHAR guids[GMAX], old_single[64], merged[GMAX];
static int guid_list(HKEY key, DWORD *units, int *exists) {
    DWORD type = 0, bytes = sizeof(guids);
    LONG r = p_RegQueryValueExW(key, L"DeviceInterfaceGUIDs", NULL, &type, (BYTE *)guids, &bytes);
    if (r == ERROR_FILE_NOT_FOUND) {
        guids[0] = guids[1] = 0;
        *units = 2;
        *exists = 0;
    } else if (r != ERROR_SUCCESS || type != REG_MULTI_SZ || bytes % sizeof(WCHAR) ||
               bytes > sizeof(guids) || !multi_ok(guids, bytes / sizeof(WCHAR)))
        return 0;
    else {
        *units = bytes / sizeof(WCHAR);
        *exists = 1;
    }
    unsigned used = 0;
    for (const WCHAR *s = guids; *s; s += wlen(s) + 1) {
        if (!valid_guid(s))
            return 0;
        used += wlen(s) + 1;
    }
    /* Reject hidden trailing data instead of truncating it on a rewrite. */
    for (unsigned i = used; i < *units; i++)
        if (guids[i])
            return 0;
    bytes = sizeof(old_single);
    r = p_RegQueryValueExW(key, L"DeviceInterfaceGUID", NULL, &type, (BYTE *)old_single, &bytes);
    if (r == ERROR_FILE_NOT_FOUND) {
        old_single[0] = 0;
        return 1;
    }
    return r == ERROR_SUCCESS && type == REG_SZ && bytes >= sizeof(WCHAR) &&
           bytes <= sizeof(old_single) && bytes % sizeof(WCHAR) == 0 &&
           !old_single[bytes / sizeof(WCHAR) - 1] && (wlen(old_single) + 1) * sizeof(WCHAR) == bytes &&
           valid_guid(old_single);
}
static int contains(const WCHAR *multi, const WCHAR *s) {
    for (const WCHAR *p = multi; *p; p += wlen(p) + 1)
        if (eq(p, s))
            return 1;
    return 0;
}
static int append_guid(unsigned *n, const WCHAR *s) {
    unsigned z = wlen(s) + 1;
    if (*n + z + 1 > GMAX)
        return 0;
    memcpy(merged + *n, s, z * sizeof(WCHAR));
    *n += z;
    merged[*n] = 0;
    return 1;
}
static int get_mark(HKEY key, DWORD *state) {
    DWORD t = 0, n = sizeof(*state);
    LONG r = p_RegQueryValueExW(key, MARK, NULL, &t, (BYTE *)state, &n);
    if (r == ERROR_FILE_NOT_FOUND) {
        *state = 0;
        return 1;
    }
    return r == ERROR_SUCCESS && n == sizeof(*state) && t == REG_DWORD && *state <= 31;
}
static int set_mark(HKEY key, DWORD state) {
    if (!validate(0))
        return 0;
    if (!state) {
        LONG r = p_RegDeleteValueW(key, MARK);
        return r == ERROR_SUCCESS || r == ERROR_FILE_NOT_FOUND;
    }
    return p_RegSetValueExW(key, MARK, 0, REG_DWORD, (const BYTE *)&state, sizeof(state)) ==
           ERROR_SUCCESS;
}
static int add_guid(HKEY key, DWORD *state) {
    DWORD bytes;
    int exists;
    if (!guid_list(key, &bytes, &exists))
        return 0;
    if (contains(guids, APP_GUID) || eq(old_single, APP_GUID))
        return 1;
    unsigned used = 0;
    merged[0] = 0;
    for (const WCHAR *s = guids; *s; s += wlen(s) + 1)
        if (!append_guid(&used, s))
            return 0;
    if (old_single[0] && !contains(merged, old_single) && !append_guid(&used, old_single))
        return 0;
    if (!append_guid(&used, APP_GUID) || !validate(0))
        return 0;
    /* Write ownership first: if this fails, the GUIDs remain untouched. If
     * writing the GUIDs fails, compensate only the marker that we changed. */
    DWORD before = *state, updated = before | OWN_GUID | (exists ? 0 : NEW_MULTI);
    if (!set_mark(key, updated))
        return 0;
    if (!validate(0) || p_RegSetValueExW(key, L"DeviceInterfaceGUIDs", 0, REG_MULTI_SZ, (BYTE *)merged,
                                         (used + 1) * sizeof(WCHAR)) != ERROR_SUCCESS) {
        set_mark(key, before);
        return 0;
    }
    *state = updated;
    return 1;
}
static int remove_guid(HKEY key, DWORD *state) {
    if (!(*state & OWN_GUID))
        return 1;
    DWORD units;
    int exists;
    if (!guid_list(key, &units, &exists))
        return 0;
    if (exists && contains(guids, APP_GUID)) {
        unsigned used = 0;
        merged[0] = 0;
        for (const WCHAR *s = guids; *s; s += wlen(s) + 1)
            if (!eq(s, APP_GUID) && !append_guid(&used, s))
                return 0;
        if (!validate(0))
            return 0;
        /* A plural value introduced solely by us can be removed once its
         * only remaining entry, if any, is the still-intact singular value. */
        if ((*state & NEW_MULTI) &&
            (!used || (old_single[0] && eq(merged, old_single) && !merged[wlen(merged) + 1]))) {
            if (p_RegDeleteValueW(key, L"DeviceInterfaceGUIDs") != ERROR_SUCCESS)
                return 0;
        } else {
            DWORD length = used ? used + 1 : 2;
            if (!used)
                merged[1] = 0;
            if (p_RegSetValueExW(key, L"DeviceInterfaceGUIDs", 0, REG_MULTI_SZ, (BYTE *)merged,
                                 length * sizeof(WCHAR)) != ERROR_SUCCESS)
                return 0;
        }
    }
    *state &= ~(OWN_GUID | NEW_MULTI);
    return set_mark(key, *state);
}

/* Pure, fail-closed policy shared by the native helper and offline tests. */
static unsigned wlen(const wchar_t *s) {
    unsigned n = 0;
    while (s[n])
        n++;
    return n;
}
static wchar_t upper(wchar_t c) { return (c >= L'a' && c <= L'z') ? c - (L'a' - L'A') : c; }
static int eqn(const wchar_t *a, const wchar_t *b, unsigned n) {
    for (unsigned i = 0; i < n; i++)
        if (upper(a[i]) != upper(b[i]))
            return 0;
    return 1;
}
static int eq(const wchar_t *a, const wchar_t *b) { return wlen(a) == wlen(b) && eqn(a, b, wlen(a)); }
static int begins(const wchar_t *a, const wchar_t *b) {
    return wlen(a) >= wlen(b) && eqn(a, b, wlen(b));
}
static int hex4(const wchar_t *s) {
    for (unsigned i = 0; i < 4; i++) {
        wchar_t c = upper(s[i]);
        if (!((c >= L'0' && c <= L'9') || (c >= L'A' && c <= L'F')))
            return 0;
    }
    return 1;
}
static const wchar_t hwstem[] = L"USB\\VID_18D1&PID_2D01";
/* Accept actual, entire hardware IDs only. Never search a serial suffix for MI. */
static int hw(const wchar_t *s, unsigned n, int mi) {
    unsigned p = wlen(hwstem);
    if (n < p || !eqn(s, hwstem, p))
        return 0;
    if (n >= p + 9 && eqn(s + p, L"&REV_", 5)) {
        if (!hex4(s + p + 5))
            return 0;
        p += 9;
    }
    if (mi >= 0) {
        const wchar_t *tag = mi == 0 ? L"&MI_00" : L"&MI_01";
        if (n != p + 6 || !eqn(s + p, tag, 6))
            return 0;
        p += 6;
    }
    return p == n;
}
static int instance_hw(const wchar_t *s, int mi) {
    unsigned n = 0;
    while (s[n] && s[n] != L'\\')
        n++;
    if (!s[n])
        return 0;
    n++;
    while (s[n] && s[n] != L'\\')
        n++;
    return s[n] == L'\\' && s[n + 1] != 0 && hw(s, n, mi);
}
static int decimal(const wchar_t **p, unsigned *result) {
    unsigned n = 0, x = 0;
    const wchar_t *s = *p;
    while (*s >= L'0' && *s <= L'9') {
        unsigned d = (unsigned)(*s - L'0');
        if (x > (0xffffffffU - d) / 10)
            return 0;
        x = x * 10 + d;
        s++;
        n++;
    }
    if (!n || (n > 1 && **p == L'0'))
        return 0;
    *p = s;
    *result = x;
    return 1;
}
static int token(const wchar_t *s, unsigned *r) { return decimal(&s, r) && !(*s) && *r != 0; }
static int port_list(const wchar_t *s, unsigned ports[7], unsigned *n) {
    *n = 0;
    while (1) {
        unsigned v;
        if (*n == 7 || !decimal(&s, &v) || v < 1 || v > 255)
            return 0;
        ports[(*n)++] = v;
        if (!*s)
            return 1;
        if (*s++ != L'.')
            return 0;
    }
}
static int port_match(const wchar_t *s, const unsigned *ports, unsigned n) {
    unsigned used = 0;
    while (*s) {
        if (begins(s, L"USB(")) {
            unsigned v;
            s += 4;
            if (!decimal(&s, &v) || *s++ != L')' || (*s && *s != L'#') || used >= n ||
                ports[used++] != v)
                return 0;
        }
        while (*s && *s != L'#')
            s++;
        if (*s)
            s++;
    }
    return used == n;
}
static int valid_guid(const wchar_t *s) {
    if (wlen(s) != 38 || s[0] != L'{' || s[37] != L'}')
        return 0;
    for (unsigned i = 1; i < 37; i++) {
        wchar_t c = upper(s[i]);
        if (i == 9 || i == 14 || i == 19 || i == 24) {
            if (c != L'-')
                return 0;
        } else if (!((c >= L'0' && c <= L'9') || (c >= L'A' && c <= L'F')))
            return 0;
    }
    return 1;
}

static int multi_ok(const wchar_t *s, unsigned n) {
    if (n < 2 || s[n - 1] || s[n - 2])
        return 0;
    unsigned i = 0;
    while (i + 1 < n && (s[i] || s[i + 1]))
        i++;
    for (; i < n; i++)
        if (s[i])
            return 0;
    return 1;
}

/* Compare Wine's UpdateLayeredWindow composition with alpha-probe.py.
 * Build: winegcc -m64 -Wall -Wextra -Werror -o /tmp/fusion-alpha-probe.exe
 *        internal/diagnostics/alpha-probe.c -luser32 -lgdi32
 */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static HWND overlay_window;
static unsigned toggle_step;

static LRESULT CALLBACK procedure(HWND window, UINT message, WPARAM wparam, LPARAM lparam) {
    if (message == WM_TIMER) {
        if (wparam == 2) {
            LONG_PTR style = GetWindowLongPtrA(overlay_window, GWL_EXSTYLE);
            SetWindowLongPtrA(overlay_window, GWL_EXSTYLE, style ^ WS_EX_TRANSPARENT);
            if (++toggle_step == 2) KillTimer(window, 2);
        } else {
            DestroyWindow(window);
        }
        return 0;
    }
    if (message == WM_DESTROY) {
        PostQuitMessage(0);
        return 0;
    }
    if (message == WM_PAINT) {
        PAINTSTRUCT paint;
        HDC dc = BeginPaint(window, &paint);
        FillRect(dc, &paint.rcPaint, (HBRUSH)GetStockObject(WHITE_BRUSH));
        EndPaint(window, &paint);
        return 0;
    }
    return DefWindowProcA(window, message, wparam, lparam);
}

int main(int argc, char **argv) {
    unsigned seconds = argc > 1 ? (unsigned)strtoul(argv[1], NULL, 10) : 120;
    const char *mode = argc > 2 ? argv[2] : "alpha";
    HINSTANCE instance = GetModuleHandleA(NULL);
    WNDCLASSA cls = {0};
    cls.lpfnWndProc = procedure;
    cls.hInstance = instance;
    cls.lpszClassName = "AlphaProbe";
    cls.hbrBackground = (HBRUSH)GetStockObject(WHITE_BRUSH);
    SetProcessDPIAware();
    int x = argc > 3 ? atoi(argv[3]) : GetSystemMetrics(SM_XVIRTUALSCREEN) +
        (GetSystemMetrics(SM_CXVIRTUALSCREEN) - 440) / 2;
    int y = argc > 4 ? atoi(argv[4]) : GetSystemMetrics(SM_YVIRTUALSCREEN) +
        (GetSystemMetrics(SM_CYVIRTUALSCREEN) - 200) / 2;
    if (!RegisterClassA(&cls)) return 1;
    HWND parent = CreateWindowExA(WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE, cls.lpszClassName,
                                 "Wine alpha probe background", WS_POPUP,
                                 x, y, 440, 200, NULL, NULL, instance, NULL);
    DWORD ex_style = WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE;
    if (!strcmp(mode, "transparent")) ex_style |= WS_EX_TRANSPARENT;
    HWND overlay = CreateWindowExA(ex_style,
                                  cls.lpszClassName, "Wine alpha probe ARGB", WS_POPUP,
                                  x + 20, y + 20, 400, 160, parent, NULL, instance, NULL);
    if (!parent || !overlay) return 1;
    overlay_window = overlay;
    if (!strcmp(mode, "region")) {
        HRGN region = CreateRectRgn(0, 0, 200, 160);
        if (!region) return 1;
        if (!SetWindowRgn(overlay, region, FALSE)) {
            DeleteObject(region);
            return 1;
        }
    }
    BITMAPINFO info = {0};
    info.bmiHeader.biSize = sizeof(info.bmiHeader);
    info.bmiHeader.biWidth = 400;
    info.bmiHeader.biHeight = -160;
    info.bmiHeader.biPlanes = 1;
    info.bmiHeader.biBitCount = 32;
    info.bmiHeader.biCompression = BI_RGB;
    DWORD *pixels = NULL;
    HDC dc = CreateCompatibleDC(NULL);
    HBITMAP bitmap = CreateDIBSection(dc, &info, DIB_RGB_COLORS, (void **)&pixels, NULL, 0);
    if (!dc || !bitmap || !pixels) return 1;
    HGDIOBJ previous = SelectObject(dc, bitmap);
    const DWORD colors[] = {0xffff0000, 0x80800000, 0x80008000, 0x80000080};
    const DWORD alphas[] = {0, 64, 128, 255};
    for (unsigned y = 0; y < 160; ++y) {
        for (unsigned x = 0; x < 400; ++x) {
            DWORD alpha = alphas[x / 100];
            DWORD gray = alpha / 5;
            pixels[y * 400 + x] = y < 80 ? colors[x / 100] :
                (alpha << 24) | (gray << 16) | (gray << 8) | gray;
        }
    }
    POINT target = {x + 20, y + 20}, source = {0, 0};
    SIZE size = {400, 160};
    BLENDFUNCTION blend = {AC_SRC_OVER, 0, 255, AC_SRC_ALPHA};
    ShowWindow(parent, SW_SHOWNOACTIVATE);
    BOOL color_key = !strcmp(mode, "color-key");
    if (!UpdateLayeredWindow(overlay, NULL, &target, &size, dc, &source,
                             color_key ? RGB(255, 0, 0) : 0,
                             color_key ? NULL : &blend, color_key ? ULW_COLORKEY : ULW_ALPHA)) {
        fprintf(stderr, "UpdateLayeredWindow failed: %lu\n", (unsigned long)GetLastError());
        return 1;
    }
    ShowWindow(overlay, SW_SHOWNOACTIVATE);
    SetTimer(parent, 1, seconds * 1000, NULL);
    if (!strcmp(mode, "toggle")) SetTimer(parent, 2, 3000, NULL);
    MSG message;
    while (GetMessageA(&message, NULL, 0, 0) > 0) {
        TranslateMessage(&message);
        DispatchMessageA(&message);
    }
    SelectObject(dc, previous);
    DeleteObject(bitmap);
    DeleteDC(dc);
    return 0;
}

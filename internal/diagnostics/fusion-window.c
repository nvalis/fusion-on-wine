#include <windows.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static BOOL CALLBACK visit(HWND window, LPARAM maximize) {
    char title[512] = {0};
    char class_name[256] = {0};
    RECT bounds;
    if (!IsWindowVisible(window)) return TRUE;
    GetWindowTextA(window, title, sizeof(title));
    GetClassNameA(window, class_name, sizeof(class_name));
    GetWindowRect(window, &bounds);
    if (strstr(title, "Autodesk Fusion") || !strcmp(title, "Fusion360")) {
        printf("%p %s [%s] rect=%d,%d,%d,%d style=%lx exstyle=%lx xid=%lx thread=%lu\n",
               window, title, class_name, bounds.left, bounds.top,
               bounds.right, bounds.bottom,
               (unsigned long)GetWindowLongPtrA(window, GWL_STYLE),
               (unsigned long)GetWindowLongPtrA(window, GWL_EXSTYLE),
               (unsigned long)(ULONG_PTR)GetPropA(window, "__wine_x11_whole_window"),
               (unsigned long)GetWindowThreadProcessId(window, NULL));
        if (maximize && strstr(title, "Autodesk Fusion")) {
            ShowWindow(window, SW_RESTORE);
            MoveWindow(window, 0, 0, 1800, 1100, TRUE);
            SetForegroundWindow(window);
        }
    }
    return TRUE;
}

int main(int argc, char **argv) {
    if (argc >= 3 && !strcmp(argv[1], "--desktop")) {
        HDESK desktop = OpenDesktopA(argv[2], 0, FALSE, GENERIC_ALL);
        if (!desktop || !SetThreadDesktop(desktop)) return 2;
        argc -= 2;
        argv += 2;
    }
    if (argc == 3 && !strcmp(argv[1], "focus")) {
        HWND window = (HWND)(ULONG_PTR)strtoull(argv[2], NULL, 16);
        char title[512] = {0};
        GetWindowTextA(window, title, sizeof(title));
        if (!strstr(title, "Autodesk Fusion") || !IsWindowVisible(window)) return 2;
        return !SetForegroundWindow(window);
    }
    if (argc == 2 && !strcmp(argv[1], "foreground")) {
        HWND window = GetForegroundWindow();
        char title[512] = {0};
        GetWindowTextA(window, title, sizeof(title));
        printf("%p %s\n", window, title);
        return 0;
    }
    if (argc == 5 && !strcmp(argv[1], "move")) {
        HWND window = (HWND)(ULONG_PTR)strtoull(argv[2], NULL, 16);
        char title[512] = {0};
        GetWindowTextA(window, title, sizeof(title));
        if (strcmp(title, "Fusion360")) return 2;
        return !SetWindowPos(window, NULL, atoi(argv[3]), atoi(argv[4]), 0, 0,
                             SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE);
    }
    if (argc == 2 && !strcmp(argv[1], "nav")) {
        INPUT events[6] = {0};
        WORD keys[6] = {VK_CONTROL, VK_MENU, 'N', 'N', VK_MENU, VK_CONTROL};
        for (int i = 0; i < 6; ++i) {
            events[i].type = INPUT_KEYBOARD;
            events[i].ki.wVk = keys[i];
            if (i >= 3) events[i].ki.dwFlags = KEYEVENTF_KEYUP;
        }
        printf("Sent %u keyboard events\n", SendInput(6, events, sizeof(INPUT)));
        return 0;
    }
    if (argc == 4 && !strcmp(argv[1], "click")) {
        SetCursorPos(atoi(argv[2]), atoi(argv[3]));
        // Let XWayland deliver the motion before Qt handles the button events.
        Sleep(200);
        mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0);
        mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0);
        return 0;
    }
    if (argc >= 3 && !strcmp(argv[1], "keys")) {
        size_t count = (size_t)argc - 2;
        INPUT *events = calloc(count * 2, sizeof(*events));
        if (!events) return 1;
        for (size_t i = 0; i < count; ++i) {
            events[i].type = events[count * 2 - i - 1].type = INPUT_KEYBOARD;
            events[i].ki.wVk = events[count * 2 - i - 1].ki.wVk =
                (WORD)strtoul(argv[i + 2], NULL, 0);
            events[count * 2 - i - 1].ki.dwFlags = KEYEVENTF_KEYUP;
        }
        UINT sent = SendInput((UINT)(count * 2), events, sizeof(*events));
        free(events);
        return sent != count * 2;
    }
    if (argc == 3 && !strcmp(argv[1], "key")) {
        INPUT events[2] = {0};
        events[0].type = events[1].type = INPUT_KEYBOARD;
        events[0].ki.wVk = events[1].ki.wVk = (WORD)argv[2][0];
        events[1].ki.dwFlags = KEYEVENTF_KEYUP;
        printf("Sent %u keyboard events\n", SendInput(2, events, sizeof(INPUT)));
        return 0;
    }
    EnumWindows(visit, argc == 2 && !strcmp(argv[1], "maximize"));
    return 0;
}

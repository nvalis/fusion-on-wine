// Isolated D3D11/DXVK presentation fixture; never opens Fusion or its documents.
// Build: x86_64-w64-mingw32-g++ -static -o present_latency.exe present_latency.cpp -ld3d11 -ldxgi
#include <windows.h>
#include <d3d11.h>
#include <cstdio>
#include <cstring>
#include <vector>

static void idle(unsigned milliseconds)
{
    DWORD start = GetTickCount();
    do {
        MSG msg;
        while (PeekMessageW(&msg, nullptr, 0, 0, PM_REMOVE)) {
            TranslateMessage(&msg);
            DispatchMessageW(&msg);
        }
        Sleep(10);
    } while (GetTickCount() - start < milliseconds);
}

int main(int argc, char **argv)
{
    bool toplevel = argc == 2 && !strcmp(argv[1], "--toplevel");
    if (argc > 1 && !toplevel) return 2;
    WNDCLASSW cls = {};
    cls.lpfnWndProc = DefWindowProcW;
    cls.hInstance = GetModuleHandleW(nullptr);
    cls.lpszClassName = L"PresentationLatencyProbe";
    if (!RegisterClassW(&cls)) return 2;
    HWND parent = CreateWindowW(cls.lpszClassName, L"Isolated presentation latency probe",
        WS_OVERLAPPEDWINDOW | WS_VISIBLE, 50, 50, 420, 340, nullptr, nullptr, cls.hInstance, nullptr);
    if (!parent) return 2;
    HWND target = toplevel ? parent : CreateWindowW(cls.lpszClassName, L"",
        WS_CHILD | WS_VISIBLE, 0, 0, 320, 240, parent, nullptr, cls.hInstance, nullptr);
    if (!target) return 2;
    puts("ready"); fflush(stdout);
    // Let the checker place the fixture BEFORE swapchain creation and sampling.
    idle(2000);

    DXGI_SWAP_CHAIN_DESC desc = {};
    desc.BufferDesc.Width = 320; desc.BufferDesc.Height = 240;
    desc.BufferDesc.Format = DXGI_FORMAT_R8G8B8A8_UNORM;
    desc.SampleDesc.Count = 1;
    desc.BufferUsage = DXGI_USAGE_RENDER_TARGET_OUTPUT;
    desc.BufferCount = 2; desc.OutputWindow = target; desc.Windowed = TRUE;
    desc.SwapEffect = DXGI_SWAP_EFFECT_DISCARD;
    IDXGISwapChain *swap = nullptr;
    ID3D11Device *device = nullptr;
    ID3D11DeviceContext *context = nullptr;
    HRESULT hr = D3D11CreateDeviceAndSwapChain(nullptr, D3D_DRIVER_TYPE_HARDWARE, nullptr,
        0, nullptr, 0, D3D11_SDK_VERSION, &desc, &swap, &device, nullptr, &context);
    if (FAILED(hr)) { printf("create failed %lx\n", (unsigned long)hr); return 2; }
    ID3D11Texture2D *buffer = nullptr;
    hr = swap->GetBuffer(0, __uuidof(ID3D11Texture2D), (void **)&buffer);
    if (FAILED(hr)) return 2;
    const DWORD colors[] = {0xff0000ff, 0xff00ff00, 0xffff0000}; // RGBA red, green, blue
    std::vector<DWORD> pixels(320 * 240);
    int result = 0;
    for (int frame = 0; frame < 6; ++frame) {
        for (auto &pixel : pixels) pixel = colors[frame % 3];
        context->UpdateSubresource(buffer, 0, nullptr, pixels.data(), 320 * 4, 0);
        context->Flush();
        hr = swap->Present(0, 0);
        if (FAILED(hr)) { printf("present failed %lx\n", (unsigned long)hr); result = 2; break; }
        printf("frame %d\n", frame); fflush(stdout);
        // Keep dispatching messages, but do not submit another frame to unstick this one.
        idle(1000);
    }
    buffer->Release(); swap->Release(); context->Release(); device->Release();
    DestroyWindow(parent);
    return result;
}

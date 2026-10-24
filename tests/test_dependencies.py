"""Host dependency checks accept Wine's GCC and LLVM Windows toolchains."""
import subprocess
import unittest
from unittest.mock import patch

from internal.setup import REQUIRED_COMMANDS, dependencies


class DependencyTests(unittest.TestCase):
    def check_tools(self, extra=(), missing=(), missing_library=False):
        available = (set(REQUIRED_COMMANDS) | set(extra)) - set(missing)
        with patch("internal.setup.shutil.which",
                   side_effect=lambda name: "/usr/bin/" + name if name in available else None), \
             patch("internal.setup.subprocess.run",
                   return_value=subprocess.CompletedProcess([], int(missing_library))):
            dependencies()

    def test_mingw_without_llvm(self):
        self.check_tools(("x86_64-w64-mingw32-gcc", "i686-w64-mingw32-gcc"))

    def test_llvm_without_mingw(self):
        self.check_tools(("clang", "lld", "llvm-dlltool"))

    def test_no_windows_toolchain(self):
        with self.assertRaisesRegex(RuntimeError, "Windows cross-compiler toolchain"):
            self.check_tools()

    def test_mingw_requires_both_architectures(self):
        for compiler in ("x86_64-w64-mingw32-gcc", "i686-w64-mingw32-gcc"):
            with self.subTest(compiler=compiler), self.assertRaisesRegex(RuntimeError, "cross-compiler"):
                self.check_tools((compiler,))

    def test_llvm_requires_linker_and_dlltool(self):
        llvm = ("clang", "lld", "llvm-dlltool")
        for tool in llvm:
            with self.subTest(missing=tool), self.assertRaisesRegex(RuntimeError, "cross-compiler"):
                self.check_tools(llvm, missing=(tool,))

    def test_other_host_tools_still_required(self):
        with self.assertRaisesRegex(RuntimeError, "Missing host tools: cabextract"):
            self.check_tools(("clang", "lld", "llvm-dlltool"), missing=("cabextract",))

    def test_development_libraries_still_required(self):
        with self.assertRaisesRegex(RuntimeError, "Missing development libraries"):
            self.check_tools(("clang", "lld", "llvm-dlltool"), missing_library=True)


if __name__ == "__main__":
    unittest.main()

# PyInstaller spec for the Windows app (one folder, no console window).
# Build with packaging\build.bat or the GitHub Actions workflow.
import glob
import os

from PyInstaller.utils.hooks import collect_submodules

root = os.path.abspath(os.path.join(SPECPATH, ".."))
datas = [
    (os.path.join(root, "rtlnc", "launcher.html"), "rtlnc"),
    (os.path.join(root, "rtlnc", "addons", "panel.html"), os.path.join("rtlnc", "addons")),
    (os.path.join(root, "rtlnc", "addons", "devices.html"), os.path.join("rtlnc", "addons")),
    (os.path.join(root, "packaging", "THIRD-PARTY.txt"), "."),
]
# RTL-SDR Blog V4 driver library (rtlsdr.dll + libusb etc.), fetched by fetch_dlls.ps1
dlls = glob.glob(os.path.join(root, "packaging", "vendor", "rtlsdr", "*.dll"))
if not dlls and not os.environ.get("RTLNC_ALLOW_NO_DLL"):
    raise SystemExit("packaging/vendor/rtlsdr/*.dll missing: run packaging/fetch_dlls.ps1 first")
binaries = [(f, ".") for f in dlls]

hidden = collect_submodules("rtlnc") + [
    "serial.tools.list_ports_windows",
    "scipy.signal", "scipy.ndimage", "scipy.special._cdflib",
]

a = Analysis(
    [os.path.join(SPECPATH, "rtlnc_app.py")],
    pathex=[root],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["pyrtlsdrlib", "tkinter", "matplotlib", "IPython", "pytest", "playwright"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="rtlnc",
    console=False,                       # no command window, ever
    icon=os.path.join(SPECPATH, "rtlnc.ico"),
    upx=False,
    version=None,
)
coll = COLLECT(exe, a.binaries, a.datas, name="rtlnc", upx=False)

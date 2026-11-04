import ctypes
import time
import traceback
import webbrowser
import subprocess
from base64 import b64encode
from ctypes import wintypes
from dataclasses import dataclass
import hashlib
import queue
import re
from typing import Any
import os
import shutil
import datetime
import sys
import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from tkinter import filedialog, messagebox
import threading
import json
from typing import Callable
import urllib.error
import urllib.request
from pathlib import Path
import winreg
import zipfile

LOGGER = Callable[..., None]
# Number of seconds to sleep for before running subprocess.Popen.
# This is to avoid collision issues with shared libraries in-between
# multiple calls.
BUFFER = 2
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# Each download URL is tried this many times; an interrupted download resumes
# where it stopped.
DOWNLOAD_ATTEMPTS = 3
GB = 1024**3
R2 = "https://pub-4d7a00307627488aa260526c1e97239f.r2.dev"
CONFIG = {
    "LOG_DIR": "runs",
    "BUILD_DIR": "build",
    "USER_AGENT": "DecompFrontier-Installer",
    # Windows paths stop at 259 characters. The longest file the installer
    # writes is deploy\game_content\ plus the longest assets/ entry (119
    # characters today), so the install folder itself must stay shorter.
    "LONGEST_INSTALLED_PATH": 150,
    "PROXY_DLL": {
        # The offline proxy that replaces the client's libcurl.dll: built from
        # Seltraeh/offline-proxy 50c3a8b (branch fps-cap), connects to
        # 127.0.0.1:9960. A new proxy needs a new URL and checksum here.
        "URLS": [f"{R2}/proxy/50c3a8b/libcurl.dll"],
        "SHA256": "44dedefdaccdf8bd401af03b8b34c59c3d28f26470b9dd1ff0adf9be699b454f",
        "SAVE_PATH": "build/libcurl.dll",
    },
    "WINDOWS_SDK_ID": "Microsoft.WindowsSDK.10.0.26100",
    "CERT": {
        # The package publisher becomes CN=<NAME>, so it also fixes the
        # client's package family name (and with it the loopback exemption).
        "NAME": "BraveFrontier",
        "PUBLIC_PATH": "build/BraveFrontier.cer",
    },
    "APPX": {
        "NAME": "gumi.BraveFrontier",
        "PROCESS": "BraveFrontier.Windows",
        "URLS": [f"{R2}/client.appx"],
        "SHA256": "1e8028b08e35d05cffd5048ecc12cdfcbb325352e48a2e0dddd2149a581721c2",
        "SAVE_PATH": "build/client.appx",
        "UNPACK_DIR": "build/BraveFrontierAppxClient",
        "PATCHED_PATH": "build/BraveFrontierPatched.appx",
        "ORIGINAL_PUBLISHER_CN": "5AA816A3-ED94-4AA2-A2B4-3ADDA1FABFB6",
        "DEP": {
            "NAME": "Microsoft.VCLibs.120.00",
            "MIN_VERSION": "12.0.21005.1",
            # The same Microsoft-signed package from two hosts; the checksum
            # decides, so either copy is safe to use.
            "URLS": [
                f"{R2}/deps/Microsoft.VCLibs.120.00_12.0.21005.1_x86.appx",
                "https://raw.githubusercontent.com/M1k3G0/Win10_LTSC_VP9_Installer/master/Microsoft.VCLibs.120.00_12.0.21005.1_x86__8wekyb3d8bbwe.appx",
            ],
            "SHA256": "f1302e899f015818b1b807f7a1dc44c221c184e3f34a19fe81b5874d4ab974bd",
            "SAVE_PATH": "build/Microsoft.VCLibs.x86.appx",
            # The game imports these, and Windows supplies them only from this
            # package, to the installed app. An .exe started from a folder
            # fails with "msvcr120_app.dll was not found".
            "DLLS": ("msvcr120_app.dll", "msvcp120_app.dll", "vccorlib120_app.dll"),
        },
    },
    "ASSETS": {
        "URLS": [f"{R2}/assets.zip"],
        "SAVE_PATH": "build/assets.zip",
        # Used for the disk-space check until the hosted size is known.
        "SIZE_GUESS": 3.5 * GB,
        "CONTENT_SUBDIR": "game_content",
    },
    "SERVER": {
        "URLS": [
            "https://github.com/Seltraeh/server/releases/download/Open-Alpha/server.zip"
        ],
        "SAVE_PATH": "build/server.zip",
        "EXPORT_DIR": "deploy",
        "BINARY": "gimuserverw.exe",
        "BACKUP_DIR": "build/save_backups",
        "SAVE_FILES": ("gme.sqlite", "gme.sqlite-wal", "gme.sqlite-shm"),
    },
}


# Context object to hold shared state and utilities for the setup process.
@dataclass
class Context:
    logger: LOGGER
    offlineproxyPath: Path | None = None
    makeappxPath: Path | None = None
    signtoolPath: Path | None = None
    certThumbprint: str | None = None
    installedVersion: tuple[int, ...] | None = None
    clientCurrent: bool = False
    otherClients: list[str] | None = None
    patchedAppxPath: Path | None = None
    serverBinaryPath: Path | None = None
    saveBackupDir: Path | None = None


@dataclass
class Logline:
    txt: str
    clearLine: bool = False


class StreamToLogger:
    def __init__(self, ctx):
        self.ctx = ctx
        self.buffer = []
        self._ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

    def write(self, data):
        for char in data:
            if char in ["\r", "\n"]:
                line_text = "".join(self.buffer)
                clean = self._ANSI_ESCAPE.sub("", line_text).strip()
                if clean:
                    self.ctx.logger(Logline(clean, (char == "\r")))
                self.buffer = []
            else:
                self.buffer.append(char)

    # Required for sys.stdout compatibility.
    def flush(self):
        pass


def psQuote(text: str) -> str:
    # A PowerShell single-quoted literal: nothing inside it is expanded.
    return "'" + text.replace("'", "''") + "'"


def powershell(script: str) -> subprocess.CompletedProcess:
    # Runs a short PowerShell script and captures its output as UTF-8.
    return subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )


def fileSha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def formatSize(n: float) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.2f} GB"


def httpRequest(
    url: str, method: str = "GET", headers: dict | None = None, timeout: int = 60
):
    req = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": CONFIG["USER_AGENT"], **(headers or {})},
    )
    return urllib.request.urlopen(req, timeout=timeout)


def remoteSize(url: str) -> int | None:
    # Content-Length from a HEAD request, or None when the host does not say.
    try:
        with httpRequest(url, method="HEAD", timeout=30) as r:
            size = r.headers.get("Content-Length")
            return int(size) if size and size.isdigit() else None
    except Exception:
        return None


def _fetch(ctx: Context, url: str, dest: Path) -> None:
    # Downloads url into dest.part, resuming an earlier interrupted download of
    # the same file, checks the byte count, then renames it to dest.
    part = dest.with_name(dest.name + ".part")
    meta = dest.with_name(dest.name + ".part.json")
    have = part.stat().st_size if part.exists() else 0
    saved: dict = {}
    if have:
        try:
            saved = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            saved = {}
        if saved.get("url") != url or not saved.get("etag"):
            have = 0

    try:
        r = httpRequest(url, headers={"Range": f"bytes={have}-"} if have else None)
    except urllib.error.HTTPError as e:
        if e.code == 416 and have:
            # The partial file no longer fits the hosted one: start over.
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            return _fetch(ctx, url, dest)
        raise

    with r:
        etag = r.headers.get("ETag")
        total = None
        mode = "wb"
        if have and r.status == 206:
            m = re.match(r"bytes (\d+)-\d+/(\d+)", r.headers.get("Content-Range", ""))
            if (
                not m
                or int(m.group(1)) != have
                or etag != saved.get("etag")
                or int(m.group(2)) != saved.get("total")
            ):
                # The hosted file changed since the partial download began.
                r.close()
                part.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                return _fetch(ctx, url, dest)
            mode = "ab"
            total = int(m.group(2))
            ctx.logger(Logline(f"Resuming {dest.name} at {formatSize(have)}"))
        else:
            have = 0
            length = r.headers.get("Content-Length")
            total = int(length) if length and length.isdigit() else None
            meta.write_text(
                json.dumps({"url": url, "etag": etag, "total": total}),
                encoding="utf-8",
            )

        done = have
        lastReport = 0.0
        with part.open(mode) as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                now = time.monotonic()
                if now - lastReport >= 0.5:
                    lastReport = now
                    progress = f"{formatSize(done)}"
                    if total:
                        progress += f" of {formatSize(total)} ({done * 100 // total}%)"
                    ctx.logger(Logline(f"{dest.name}: {progress}", clearLine=True))

    size = part.stat().st_size
    if total is not None and size != total:
        raise RuntimeError(f"the download stopped at {size:,} of {total:,} bytes")
    os.replace(part, dest)
    meta.unlink(missing_ok=True)


def download(
    ctx: Context,
    urls: str | list[str],
    dest: Path,
    overwrite: bool = False,
    sha256: str | None = None,
) -> None:
    urls = [urls] if isinstance(urls, str) else list(urls)
    if dest.exists() and not overwrite:
        # A file kept from an earlier run is reused only while it still matches
        # the expected file, so re-running the installer picks up updates.
        if sha256:
            if fileSha256(dest) == sha256:
                ctx.logger(Logline(f"{dest} is already downloaded and verified"))
                return
            ctx.logger(
                Logline(f"{dest} is not the expected file, downloading it again")
            )
        else:
            size = remoteSize(urls[0])
            if size is None or size == dest.stat().st_size:
                ctx.logger(
                    Logline(f"File already exists at {dest}, skipping download")
                )
                return
            ctx.logger(
                Logline(f"{dest} differs from the hosted copy, downloading the new one")
            )

    dest.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    for url in urls:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            again = f" (attempt {attempt} of {DOWNLOAD_ATTEMPTS})" if attempt > 1 else ""
            ctx.logger(Logline(f"Downloading {dest.name} from {url}{again}"))
            try:
                _fetch(ctx, url, dest)
            except urllib.error.HTTPError as e:
                errors.append(f"{url}: HTTP {e.code}")
                ctx.logger(Logline(f"The host answered HTTP {e.code}"))
                if e.code in (401, 403, 404, 410):
                    break  # asking again would get the same answer
                time.sleep(3)
                continue
            except Exception as e:
                errors.append(f"{url}: {e}")
                ctx.logger(Logline(f"Download interrupted: {e}"))
                time.sleep(3)
                continue

            if sha256 and fileSha256(dest) != sha256:
                dest.unlink(missing_ok=True)
                errors.append(f"{url}: not the expected file (checksum mismatch)")
                ctx.logger(Logline(f"{dest.name} from {url} is not the expected file"))
                break  # this host serves a different file; try the next one
            ctx.logger(Logline(f"Download complete: {dest}"))
            return

    webbrowser.open(urls[0])
    raise RuntimeError(
        f"Could not download {dest.name} ({'; '.join(errors[-3:])}). Your browser"
        f" was opened at {urls[0]}: save that file as {dest.absolute()}, then run"
        " the installer again."
    )


def runSubprocess(ctx: Context, cmd: list[str], **kwargs: Any) -> int:
    time.sleep(BUFFER)
    creationFlags = kwargs.pop("creationflags", 0) | CREATE_NO_WINDOW
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationFlags,
        **kwargs,
    )
    stream = StreamToLogger(ctx)
    if p.stdout:
        for char_byte in iter(lambda: p.stdout.read(1), b""):  # type: ignore
            char = char_byte.decode("utf-8", errors="replace")  # type: ignore
            stream.write(char)

    return p.wait()


class _ShellExecuteInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("fMask", wintypes.ULONG),
        ("hwnd", wintypes.HWND),
        ("lpVerb", wintypes.LPCWSTR),
        ("lpFile", wintypes.LPCWSTR),
        ("lpParameters", wintypes.LPCWSTR),
        ("lpDirectory", wintypes.LPCWSTR),
        ("nShow", ctypes.c_int),
        ("hInstApp", wintypes.HINSTANCE),
        ("lpIDList", ctypes.c_void_p),
        ("lpClass", wintypes.LPCWSTR),
        ("hkeyClass", wintypes.HKEY),
        ("dwHotKey", wintypes.DWORD),
        ("hIconOrMonitor", wintypes.HANDLE),
        ("hProcess", wintypes.HANDLE),
    ]


_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SEE_MASK_NOASYNC = 0x00000100
_ERROR_CANCELLED = 1223
_INFINITE = 0xFFFFFFFF
_shell32 = ctypes.WinDLL("shell32", use_last_error=True)
_shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(_ShellExecuteInfo)]
_shell32.ShellExecuteExW.restype = wintypes.BOOL
_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_kernel32.WaitForSingleObject.restype = wintypes.DWORD
_kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
_kernel32.GetExitCodeProcess.restype = wintypes.BOOL
_kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
_kernel32.CloseHandle.restype = wintypes.BOOL


def runElevatedPowerShell(
    ctx: Context, script: str, purpose: str, verb: str = "runas"
) -> None:
    # Runs script in a hidden, elevated PowerShell (one UAC prompt) and waits
    # for it to finish. The script travels on the command line
    # (-EncodedCommand), so no script file can vanish or be blocked by the
    # execution policy, and its error text comes back through a log file.
    logPath = Path(CONFIG["BUILD_DIR"]).absolute() / "elevated_step.log"
    logPath.parent.mkdir(parents=True, exist_ok=True)
    logPath.unlink(missing_ok=True)
    wrapped = (
        "$ErrorActionPreference = 'Stop'\n"
        "try {\n" + script + "\n}\n"
        "catch {\n"
        f"    ($_ | Out-String) | Set-Content -Encoding UTF8 -LiteralPath {psQuote(str(logPath))}\n"
        "    exit 1\n"
        "}\n"
        "exit 0\n"
    )
    encoded = b64encode(wrapped.encode("utf-16-le")).decode("ascii")

    info = _ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = _SEE_MASK_NOCLOSEPROCESS | _SEE_MASK_NOASYNC
    info.lpVerb = verb
    info.lpFile = "powershell.exe"
    info.lpParameters = (
        f"-NoProfile -NonInteractive -WindowStyle Hidden -EncodedCommand {encoded}"
    )
    info.nShow = 0  # SW_HIDE

    ctx.logger(
        Logline(f"Windows will ask for administrator permission to {purpose}...")
    )
    if not _shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == _ERROR_CANCELLED:
            raise RuntimeError(
                f"Administrator permission was declined, so the installer could not"
                f" {purpose}. Run the installer again and choose Yes."
            )
        raise RuntimeError(f"Could not start the administrator step (error {err}).")
    if not info.hProcess:
        raise RuntimeError("The administrator step did not start.")
    try:
        _kernel32.WaitForSingleObject(info.hProcess, _INFINITE)
        code = wintypes.DWORD()
        _kernel32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
    finally:
        _kernel32.CloseHandle(info.hProcess)

    try:
        if code.value != 0:
            detail = (
                logPath.read_text(encoding="utf-8-sig", errors="replace").strip()
                if logPath.exists()
                else f"exit code {code.value}"
            )
            raise RuntimeError(f"Could not {purpose}: {detail}")
    finally:
        logPath.unlink(missing_ok=True)


def packageFamilyName() -> str:
    # Windows names a package family <name>_<publisher id>; the publisher id is
    # the first 64 bits of SHA-256(UTF-16LE publisher), in Crockford base32.
    publisher = "CN=" + CONFIG["CERT"]["NAME"]
    digest = hashlib.sha256(publisher.encode("utf-16-le")).digest()[:8]
    bits = "".join(f"{b:08b}" for b in digest) + "0"
    alphabet = "0123456789abcdefghjkmnpqrstvwxyz"
    publisherId = "".join(alphabet[int(bits[i : i + 5], 2)] for i in range(0, 65, 5))
    return f"{CONFIG['APPX']['NAME']}_{publisherId}"


def installedClient() -> tuple[tuple[int, ...], Path] | None:
    # The client this installer made earlier (same publisher), for this user.
    publisher = "CN=" + CONFIG["CERT"]["NAME"]
    p = powershell(
        f"Get-AppxPackage -Name {psQuote(CONFIG['APPX']['NAME'])} | "
        f"Where-Object {{ $_.Publisher -eq {psQuote(publisher)} }} | "
        "Select-Object -First 1 | "
        "ForEach-Object { [string]$_.Version + '|' + [string]$_.InstallLocation }"
    )
    line = next((l.strip() for l in p.stdout.splitlines() if "|" in l), None)
    if not line:
        return None
    version, location = line.split("|", 1)
    try:
        return tuple(int(x) for x in version.split(".")), Path(location)
    except ValueError:
        return None


def clientProblem(expectedVersion: str | None = None) -> str | None:
    # Why the client this installer made cannot start, or None when it can: its
    # package must be registered and healthy, and its VCLibs framework must
    # hold the DLLs the game imports.
    dep = CONFIG["APPX"]["DEP"]
    publisher = "CN=" + CONFIG["CERT"]["NAME"]
    dlls = ", ".join(psQuote(d) for d in dep["DLLS"])
    p = powershell(
        f"$g = Get-AppxPackage -Name {psQuote(CONFIG['APPX']['NAME'])} | "
        f"Where-Object {{ $_.Publisher -eq {psQuote(publisher)} }} | Select-Object -First 1; "
        "if (-not $g) { 'state=missing'; return }; "
        "'version=' + [string]$g.Version; 'status=' + [string]$g.Status; "
        f"$v = @($g.Dependencies) | Where-Object {{ $_.Name -eq {psQuote(dep['NAME'])} }} | Select-Object -First 1; "
        "if (-not $v) { 'dependency=missing'; return }; "
        "'dependency=' + [string]$v.Status; "
        f"foreach ($d in @({dlls})) {{ if (-not (Test-Path -LiteralPath (Join-Path ([string]$v.InstallLocation) $d))) {{ 'missingdll=' + $d }} }}"
    )
    facts: dict[str, list[str]] = {}
    for line in p.stdout.splitlines():
        key, sep, value = line.strip().partition("=")
        if sep:
            facts.setdefault(key, []).append(value)
    if not facts:
        return f"Windows did not report the installed client ({(p.stderr or 'no output').strip()})"
    if "state" in facts:
        return "Brave Frontier is not installed"
    version = facts.get("version", ["?"])[0]
    if expectedVersion and version != expectedVersion:
        return f"version {version} is installed instead of {expectedVersion}"
    status = facts.get("status", ["?"])[0]
    if status != "Ok":
        return f"its package status is {status}"
    depStatus = facts.get("dependency", ["missing"])[0]
    if depStatus == "missing":
        return f"its {dep['NAME']} dependency is not registered"
    if depStatus != "Ok":
        return f"its {dep['NAME']} dependency's status is {depStatus}"
    if "missingdll" in facts:
        return f"{', '.join(facts['missingdll'])} missing from {dep['NAME']}"
    return None


def otherClientCopies() -> list[str]:
    # Brave Frontier packages from another publisher (not made by this
    # installer). They cannot reach the offline server, but share the name.
    publisher = "CN=" + CONFIG["CERT"]["NAME"]
    p = powershell(
        f"Get-AppxPackage -Name {psQuote(CONFIG['APPX']['NAME'])} | "
        f"Where-Object {{ $_.Publisher -ne {psQuote(publisher)} }} | "
        "ForEach-Object { [string]$_.PackageFullName }"
    )
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


def removeUnpackedClient(ctx: Context) -> None:
    # The unpacked client is only a step towards the signed package. Its
    # BraveFrontier.Windows.exe cannot start outside the installed app, so it
    # is not left where a player could double-click it.
    unpackDir = Path(CONFIG["APPX"]["UNPACK_DIR"])
    if unpackDir.exists():
        shutil.rmtree(unpackDir, ignore_errors=True)
        ctx.logger(
            Logline(
                f"Removed the unpacked client in {unpackDir} (the game starts only"
                " from the Start menu)"
            )
        )


def isLoopbackExempt(family: str) -> bool:
    p = subprocess.run(
        ["CheckNetIsolation.exe", "LoopbackExempt", "-s"],
        capture_output=True,
        text=True,
        errors="replace",
        creationflags=CREATE_NO_WINDOW,
    )
    # Matches the family name itself, not the (localised) "Name:" label.
    pattern = r"(?<![\w.])" + re.escape(family.lower()) + r"(?![\w.])"
    return re.search(pattern, p.stdout.lower()) is not None


def loopbackScript(family: str) -> str:
    return (
        f"& CheckNetIsolation.exe LoopbackExempt -a {psQuote('-n=' + family)} | Out-Null\n"
        'if ($LASTEXITCODE -ne 0) { throw "CheckNetIsolation.exe exited with code $LASTEXITCODE" }'
    )


def confirmLoopback(ctx: Context, family: str) -> None:
    # The elevated step already failed on a non-zero exit, so a listing that
    # does not show the name only earns a warning.
    if isLoopbackExempt(family):
        ctx.logger(Logline(f"Loopback enabled for {family}"))
    else:
        ctx.logger(
            Logline(
                f"Could not confirm the loopback exemption for {family}. If the game"
                " cannot reach the server, run 'Enable Client Loopback.ps1' from the"
                " deploy folder as administrator."
            )
        )


def runningProcessPaths(name: str) -> list[str]:
    # Paths of running processes with this name ('' when Windows hides one).
    p = powershell(
        f"Get-Process -Name {psQuote(name)} -ErrorAction SilentlyContinue | "
        "ForEach-Object { [string]$_.Path }"
    )
    return [line.strip() for line in p.stdout.splitlines()]


def removeSigningCert(ctx: Context) -> None:
    # The signing key is only needed while the client is signed; Windows trusts
    # the public certificate from then on.
    if not ctx.certThumbprint:
        return
    certPath = "Cert:\\CurrentUser\\My\\" + ctx.certThumbprint
    powershell(
        f"Remove-Item -Path {psQuote(certPath)} -DeleteKey -ErrorAction SilentlyContinue"
    )


def dirSize(path: Path) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.stat(os.path.join(root, name)).st_size
            except OSError:
                pass
    return total


class Preflight:
    # Checks that would otherwise fail halfway through a long install.
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _checkFolder(self) -> None:
        installDir = Path.cwd()
        longest = len(str(installDir)) + 1 + CONFIG["LONGEST_INSTALLED_PATH"]
        if longest > 259:
            raise RuntimeError(
                f"The installation folder's path is too long ({len(str(installDir))}"
                " characters) for Windows to hold the game files. Choose a folder with"
                " a shorter path, such as C:\\Games\\BraveFrontier."
            )
        if "onedrive" in str(installDir).lower():
            self.ctx.logger(
                Logline(
                    "Note: this folder is inside OneDrive, which will try to upload"
                    " about 7 GB of game files. A folder outside OneDrive, such as"
                    " C:\\Games\\BraveFrontier, avoids that."
                )
            )

    def _checkServerNotRunning(self) -> None:
        deployDir = str(Path(CONFIG["SERVER"]["EXPORT_DIR"]).absolute()).lower()
        for path in runningProcessPaths(Path(CONFIG["SERVER"]["BINARY"]).stem):
            if path and path.lower().startswith(deployDir + os.sep):
                raise RuntimeError(
                    "The game server from this folder is running. Close its window,"
                    " then run the installer again."
                )

    def _checkDiskSpace(self) -> None:
        assetsZip = Path(CONFIG["ASSETS"]["SAVE_PATH"])
        deployDir = Path(CONFIG["SERVER"]["EXPORT_DIR"])
        hosted = remoteSize(CONFIG["ASSETS"]["URLS"][0]) or int(
            CONFIG["ASSETS"]["SIZE_GUESS"]
        )
        need = GB + 300 * 1024**2  # headroom, the client packages and the server
        freed = 0
        haveZip = assetsZip.exists() and assetsZip.stat().st_size == hosted
        if not haveZip:
            need += hosted
        if not (haveZip and SetupGameServer.contentMatches(deployDir, assetsZip)):
            need += int(hosted * 1.15)  # the extracted game content
            oldContent = deployDir / CONFIG["ASSETS"]["CONTENT_SUBDIR"]
            if oldContent.is_dir():
                freed = dirSize(oldContent)  # replaced by the new extraction
        free = shutil.disk_usage(Path.cwd()).free
        if free + freed < need:
            raise RuntimeError(
                f"This install needs about {formatSize(need - freed)} of free space on"
                f" this drive, but only {formatSize(free)} is free. Free some space or"
                " choose a folder on another drive."
            )
        self.ctx.logger(
            Logline(f"Free space: {formatSize(free)} (about {formatSize(max(need - freed, 0))} needed)")
        )

    def run(self) -> None:
        self.ctx.logger(header="Checking this PC...")
        self._checkFolder()
        self._checkServerNotRunning()
        self._checkDiskSpace()


class CheckInstalledClient:
    # Decides whether the client must be installed, updated or left alone, so
    # that re-running the installer to update the server needs no client steps.
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self) -> None:
        self.ctx.logger(header="Checking the installed client...")
        removeUnpackedClient(self.ctx)  # left behind by earlier installers

        others = otherClientCopies()
        if others:
            self.ctx.otherClients = others
            for name in others:
                self.ctx.logger(
                    Logline(
                        f"Warning: another Brave Frontier is installed ({name}). It was"
                        " not made by this installer and cannot reach your server."
                    )
                )

        found = installedClient()
        if not found:
            self.ctx.logger(Logline("Brave Frontier is not installed yet."))
            return

        version, location = found
        self.ctx.installedVersion = version
        shown = ".".join(str(v) for v in version)
        try:
            current = (
                fileSha256(location / "libcurl.dll") == CONFIG["PROXY_DLL"]["SHA256"]
            )
        except OSError:
            current = False

        if current:
            problem = clientProblem()
            if not problem:
                self.ctx.clientCurrent = True
                self.ctx.logger(
                    Logline(
                        f"The installed client ({shown}) already has the current"
                        " offline proxy, so the client steps are skipped."
                    )
                )
                return
            self.ctx.logger(
                Logline(
                    f"The installed client ({shown}) cannot start ({problem}). It will"
                    " be installed again, keeping its downloaded game data."
                )
            )
        else:
            self.ctx.logger(
                Logline(
                    f"The installed client ({shown}) has an older offline proxy. It"
                    " will be updated in place, keeping its downloaded game data."
                )
            )
        if runningProcessPaths(CONFIG["APPX"]["PROCESS"]):
            raise RuntimeError("Close Brave Frontier, then run the installer again.")


class SetupOfflineProxy:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self) -> Exception | None:
        self.ctx.logger(header="Fetching offline-proxy...")
        path = Path(CONFIG["PROXY_DLL"]["SAVE_PATH"])
        download(
            self.ctx,
            CONFIG["PROXY_DLL"]["URLS"],
            path,
            sha256=CONFIG["PROXY_DLL"]["SHA256"],
        )
        self.ctx.offlineproxyPath = path


class FetchWindowsSDKTools:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _sdkRoots(self) -> list[Path]:
        roots = [
            Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)"))
            / "Windows Kits"
            / "10",
            Path(os.environ.get("ProgramFiles", "C:/Program Files"))
            / "Windows Kits"
            / "10",
        ]

        key = r"SOFTWARE\Microsoft\Windows Kits\Installed Roots"
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE, key, 0, winreg.KEY_READ | view
                ) as k:
                    for name in ("KitsRoot10", "KitsRoot81"):
                        try:
                            val, _ = winreg.QueryValueEx(k, name)
                            if val:
                                roots.append(Path(val))
                        except FileNotFoundError:
                            pass
            except OSError:
                pass

        unique: list[Path] = []
        for root in roots:
            if (root / "bin").is_dir() and all(
                os.path.normcase(str(root)) != os.path.normcase(str(u)) for u in unique
            ):
                unique.append(root)
        return unique

    _VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

    def _parseVer(self, v: str) -> tuple[int, int, int, int]:
        # Version string is expected to be four dot-separated integers.
        a, b, c, d = v.split(".")
        return int(a), int(b), int(c), int(d)

    def _findSDKTools(self) -> tuple[Path | None, Path | None]:
        # Newest SDK version first, then the 8.1 layout (bin\<arch>); a version
        # folder without both tools (a partial SDK) is passed over.
        for root in self._sdkRoots():
            binDir = root / "bin"
            versions = sorted(
                (
                    p
                    for p in binDir.iterdir()
                    if p.is_dir() and self._VERSION_RE.match(p.name)
                ),
                key=lambda p: self._parseVer(p.name),
                reverse=True,
            )
            for folder in versions + [binDir]:
                for arch in ("x64", "x86"):
                    makeappx = folder / arch / "makeappx.exe"
                    signtool = folder / arch / "signtool.exe"
                    if makeappx.exists() and signtool.exists():
                        return makeappx, signtool
        return None, None

    def _installWindowsSDK(self) -> None:
        if not shutil.which("winget"):
            raise RuntimeError(
                "winget not found. Install/enable 'App Installer' first."
            )

        cmd = [
            "winget",
            "install",
            "--exact",
            "--id",
            CONFIG["WINDOWS_SDK_ID"],
            "--source",
            "winget",
            "--accept-package-agreements",
            "--accept-source-agreements",
        ]

        self.ctx.logger(Logline("Installing Windows SDK via winget..."))
        if (runSubprocess(self.ctx, cmd)) != 0:
            raise RuntimeError(
                f"winget install failed. You can attempt to manually install "
                f"the Windows SDK from "
                f"https://developer.microsoft.com/windows/downloads/windows-10-sdk/ and "
                f"then re-run this installer."
            )
        self.ctx.logger(Logline("Windows SDK installation complete!"))

    def run(self) -> Exception | None:
        self.ctx.logger(header="Discovering Windows SDK tools...")

        self.ctx.makeappxPath, self.ctx.signtoolPath = self._findSDKTools()
        if not (self.ctx.makeappxPath and self.ctx.signtoolPath):
            self.ctx.logger(
                Logline(
                    "Failed to find tools, attempting to install Windows SDK..."
                )
            )
            self._installWindowsSDK()
            self.ctx.makeappxPath, self.ctx.signtoolPath = self._findSDKTools()
            if not (self.ctx.makeappxPath and self.ctx.signtoolPath):
                raise RuntimeError(
                    "makeappx.exe and signtool.exe are still missing after the"
                    " Windows SDK install. Restart the PC and run the installer again."
                )

        self.ctx.logger(
            Logline(f"Found makeappx.exe at {self.ctx.makeappxPath}")
        )
        self.ctx.logger(
            Logline(f"Found signtool.exe at {self.ctx.signtoolPath}")
        )


class GenerateDeveloperCert:
    # A fresh self-signed certificate for this install. It stays in the user's
    # certificate store only until the client is signed, and no password or
    # .pfx file is needed.
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self) -> Exception | None:
        self.ctx.logger(header="Generating developer certificate...")
        cerPath = Path(CONFIG["CERT"]["PUBLIC_PATH"]).absolute()
        cerPath.parent.mkdir(parents=True, exist_ok=True)
        cerPath.unlink(missing_ok=True)

        subject = "CN=" + CONFIG["CERT"]["NAME"]
        p = powershell(
            f"$c = New-SelfSignedCertificate -Type Custom -Subject {psQuote(subject)} "
            "-KeyUsage DigitalSignature -FriendlyName 'DecompFrontier client signing' "
            "-CertStoreLocation 'Cert:\\CurrentUser\\My' "
            "-TextExtension @('2.5.29.37={text}1.3.6.1.5.5.7.3.3', '2.5.29.19={text}'); "
            f"Export-Certificate -Cert $c -FilePath {psQuote(str(cerPath))} | Out-Null; "
            "Write-Output $c.Thumbprint"
        )
        match = re.search(r"\b([A-F0-9]{40})\b", p.stdout, re.IGNORECASE)
        if match:
            self.ctx.certThumbprint = match.group(1).upper()
        if p.returncode != 0 or not match or not cerPath.exists():
            raise RuntimeError(
                "Could not create the signing certificate: "
                + (p.stderr or p.stdout).strip()
            )
        self.ctx.logger(
            Logline(f"Certificate created with thumbprint: {self.ctx.certThumbprint}")
        )


class TrustCertAndEnableLoopback:
    # The single administrator step: trust the new certificate (so Windows
    # installs the client) and let the client reach the server on this PC
    # (Windows blocks localhost for store apps by default).
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self) -> None:
        self.ctx.logger(header="Trusting the certificate and enabling loopback...")
        assert self.ctx.certThumbprint, "The certificate must be created first!"
        family = packageFamilyName()
        cerPath = str(Path(CONFIG["CERT"]["PUBLIC_PATH"]).absolute())
        rootPath = "Cert:\\LocalMachine\\Root"
        runElevatedPowerShell(
            self.ctx,
            f"Import-Certificate -FilePath {psQuote(cerPath)} -CertStoreLocation {psQuote(rootPath)} | Out-Null\n"
            + loopbackScript(family),
            "trust the signing certificate and let the game connect to the server on this PC",
        )

        certInRoot = rootPath + "\\" + self.ctx.certThumbprint
        trusted = powershell(f"Test-Path -Path {psQuote(certInRoot)}")
        if trusted.stdout.strip() != "True":
            raise RuntimeError(
                "The signing certificate is not in the trusted root store."
            )
        self.ctx.logger(Logline("Certificate trusted"))
        confirmLoopback(self.ctx, family)


class EnsureLoopback:
    # For a client that is already installed: re-add the loopback exemption
    # only if it went missing (it needs administrator permission).
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self) -> None:
        self.ctx.logger(header="Checking the loopback exemption...")
        family = packageFamilyName()
        if isLoopbackExempt(family):
            self.ctx.logger(Logline(f"Loopback is already enabled for {family}"))
            return
        runElevatedPowerShell(
            self.ctx,
            loopbackScript(family),
            "let the game connect to the server on this PC",
        )
        confirmLoopback(self.ctx, family)


def nextClientVersion(manifestVersion: str, installed: tuple[int, ...] | None) -> str:
    # Windows refuses to reinstall a package at the same version, so an update
    # takes the next revision above the installed one (the game never reads it).
    base = tuple(int(x) for x in manifestVersion.split("."))
    if installed is None or tuple(installed) < base:
        return manifestVersion
    major, minor, build, revision = (tuple(installed) + (0, 0, 0, 0))[:4]
    if revision >= 65535:
        raise RuntimeError(
            "The installed client's version cannot be raised further; uninstall"
            " Brave Frontier, then run the installer again."
        )
    return f"{major}.{minor}.{build}.{revision + 1}"


class PatchGameClient:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.version: str | None = None  # the package version being installed

    def _unpackAppx(self, appxPath: Path) -> None:
        # Clean up any previous unpacked client to ensure a fresh start.
        unpackDir = Path(CONFIG["APPX"]["UNPACK_DIR"])
        if unpackDir.exists():
            self.ctx.logger(Logline("Cleaning up previous unpacked client..."))
            shutil.rmtree(unpackDir)

        # Recreate the now-empty directory
        unpackDir.mkdir(parents=True, exist_ok=True)

        cmd = [
            str(self.ctx.makeappxPath),
            "unpack",
            "/p",
            str(appxPath),
            "/d",
            CONFIG["APPX"]["UNPACK_DIR"],
        ]

        self.ctx.logger(Logline("Unpacking the client..."))
        if (runSubprocess(self.ctx, cmd)) != 0:
            raise RuntimeError(f"makeappx unpack failed.")
        self.ctx.logger(
            Logline(
                f"Client unpacked successfully to {CONFIG['APPX']['UNPACK_DIR']}"
            )
        )

    def _patchAppx(self) -> None:
        self.ctx.logger(Logline("Patching the client..."))

        unpackDir = Path(CONFIG["APPX"]["UNPACK_DIR"])
        shutil.copy2(self.ctx.offlineproxyPath, unpackDir / "libcurl.dll")  # type: ignore

        for f in [
            "AppxMetadata",
            "AppxSignature.p7x",
            "AppxBlockMap.xml",
            "ApplicationInsights.config",
        ]:
            path = unpackDir / f
            if not path.exists():
                continue
            if path.is_dir():
                shutil.rmtree(path)
                continue
            path.unlink()

        target = CONFIG["APPX"]["ORIGINAL_PUBLISHER_CN"]
        manifest = (unpackDir / "AppxManifest.xml").read_text(encoding="utf-8")
        if target not in manifest:
            raise RuntimeError(
                f"Target CN {target} not found in manifest. Cannot patch client!"
            )
        manifest = manifest.replace(target, CONFIG["CERT"]["NAME"])

        identity = re.search(r'(<Identity\b[^>]*?\bVersion=")([^"]+)(")', manifest)
        if not identity:
            raise RuntimeError("No package version found in the client's manifest!")
        version = nextClientVersion(identity.group(2), self.ctx.installedVersion)
        self.version = version
        manifest = (
            manifest[: identity.start(2)] + version + manifest[identity.end(2) :]
        )
        (unpackDir / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
        self.ctx.logger(
            Logline(
                f"Publisher set to CN={CONFIG['CERT']['NAME']}, package version {version}"
            )
        )

    def _packAppx(self) -> None:
        Path(CONFIG["APPX"]["PATCHED_PATH"]).parent.mkdir(
            parents=True, exist_ok=True
        )

        cmd = [
            str(self.ctx.makeappxPath),
            "pack",
            "/d",
            CONFIG["APPX"]["UNPACK_DIR"],
            "/p",
            CONFIG["APPX"]["PATCHED_PATH"],
            "/o",
        ]

        self.ctx.logger(Logline("Repacking the patched client..."))
        if (runSubprocess(self.ctx, cmd)) != 0:
            raise RuntimeError(f"makeappx repacking failed.")
        self.ctx.logger(
            Logline(
                f"Client repacked successfully to {CONFIG['APPX']['PATCHED_PATH']}"
            )
        )

        # Signs with the certificate in the user's store (by thumbprint).
        sign_cmd = [
            str(self.ctx.signtoolPath),
            "sign",
            "/v",
            "/fd",
            "SHA256",
            "/sha1",
            str(self.ctx.certThumbprint),
            CONFIG["APPX"]["PATCHED_PATH"],
        ]

        self.ctx.logger(Logline("Signing the patched client..."))
        try:
            if (runSubprocess(self.ctx, sign_cmd)) != 0:
                raise RuntimeError(f"signtool failed to sign the client.")
        finally:
            removeSigningCert(self.ctx)
        self.ctx.logger(Logline("Client signed successfully!"))

        self.ctx.patchedAppxPath = Path(CONFIG["APPX"]["PATCHED_PATH"])

    def _dependencyInstalled(self) -> bool:
        # Registered, healthy, and actually holding the DLLs the game imports.
        dep = CONFIG["APPX"]["DEP"]
        files = " -and ".join(
            f"(Test-Path -LiteralPath (Join-Path ([string]$_.InstallLocation) {psQuote(d)}))"
            for d in dep["DLLS"]
        )
        p = powershell(
            f"@(Get-AppxPackage -Name {psQuote(dep['NAME'])} | Where-Object {{"
            f" [string]$_.Architecture -eq 'X86' -and [version]$_.Version -ge [version]{psQuote(dep['MIN_VERSION'])}"
            f" -and [string]$_.Status -eq 'Ok' -and {files}"
            " }).Count"
        )
        count = p.stdout.strip()
        return count.isdigit() and int(count) > 0

    def _installDependencies(self) -> None:
        dep = CONFIG["APPX"]["DEP"]
        if self._dependencyInstalled():
            self.ctx.logger(Logline(f"{dep['NAME']} (x86) is already installed"))
            return

        vclibs_path = Path(dep["SAVE_PATH"])
        download(self.ctx, dep["URLS"], vclibs_path, sha256=dep["SHA256"])

        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Add-AppxPackage -Path {psQuote(str(vclibs_path.absolute()))}",
        ]

        self.ctx.logger(Logline("Installing VCLibs dependency..."))
        runSubprocess(self.ctx, cmd)
        if not self._dependencyInstalled():
            raise RuntimeError(
                f"Windows could not install {dep['NAME']} (x86), which the game needs"
                " (it provides msvcr120_app.dll and vccorlib120_app.dll). The reason"
                " is in the lines above."
            )
        self.ctx.logger(Logline("VCLibs dependency installed successfully!"))

    def _installGameClient(self) -> None:
        patched_path = Path(CONFIG["APPX"]["PATCHED_PATH"]).absolute()

        cmd = [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"Add-AppxPackage -Path {psQuote(str(patched_path))}",
        ]

        self.ctx.logger(Logline(f"Installing the patched client..."))
        if runSubprocess(self.ctx, cmd) != 0:
            raise RuntimeError(
                "Windows could not install the patched game client (the reason is in"
                " the lines above). If a copy of Brave Frontier that this installer"
                " did not make is installed, uninstall it, then run the installer"
                " again."
            )
        # Add-AppxPackage succeeding is not proof the game can start: check the
        # registered package and the DLLs its framework supplies.
        problem = clientProblem(self.version)
        if problem:
            raise RuntimeError(
                f"Windows reports the game as installed, but it cannot start: {problem}."
            )
        self.ctx.logger(Logline(f"Game client {self.version} installed and checked!"))

    def run(self) -> Exception | None:
        assert (
            self.ctx.offlineproxyPath and self.ctx.offlineproxyPath.exists()
        ), "Offline proxy DLL path is not set or does not exist!"
        assert (
            self.ctx.makeappxPath and self.ctx.makeappxPath.exists()
        ), "makeappx.exe path is not set or does not exist!"
        assert (
            self.ctx.signtoolPath and self.ctx.signtoolPath.exists()
        ), "signtool.exe path is not set or does not exist!"
        assert self.ctx.certThumbprint, "The signing certificate is not set!"

        self.ctx.logger(header="Patching game client...")

        appxPath = Path(CONFIG["APPX"]["SAVE_PATH"])
        download(
            self.ctx, CONFIG["APPX"]["URLS"], appxPath, sha256=CONFIG["APPX"]["SHA256"]
        )

        self._unpackAppx(appxPath)
        self._patchAppx()
        self._packAppx()
        removeUnpackedClient(self.ctx)
        self._installDependencies()
        self._installGameClient()


class SetupGameServer:
    # Everything is downloaded and checked before the deploy folder changes.
    # The old folder is renamed to deploy.previous and only deleted once the
    # new one is complete; on any failure it is put back. The marker file,
    # written last, records which assets.zip the extracted content came from:
    # an unchanged assets.zip keeps its extracted copy, so a re-run takes
    # seconds instead of minutes.
    def __init__(self, ctx: Context):
        self.ctx = ctx

    @staticmethod
    def _marker(deployDir: Path) -> Path:
        return deployDir / "game_content.installed.json"

    @staticmethod
    def _zipStamp(zipPath: Path) -> dict:
        st = zipPath.stat()
        return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}

    @classmethod
    def contentMatches(cls, deployDir: Path, zipPath: Path) -> bool:
        try:
            return (deployDir / CONFIG["ASSETS"]["CONTENT_SUBDIR"]).is_dir() and json.loads(
                cls._marker(deployDir).read_text(encoding="utf-8")
            ) == cls._zipStamp(zipPath)
        except (OSError, ValueError):
            return False

    @staticmethod
    def _assetMembers(z: zipfile.ZipFile):
        for info in z.infolist():
            parts = Path(info.filename.replace("\\", "/")).parts
            if len(parts) < 3 or parts[0] != "assets":
                continue
            if parts[1] not in ("content", "mst"):
                continue
            if any(part in ("", ".", "..") or ":" in part for part in parts):
                raise RuntimeError(
                    f"Unsafe path in assets archive: {info.filename}"
                )
            yield info, parts

    def _damaged(self, zipPath: Path, error: Exception) -> RuntimeError:
        zipPath.unlink(missing_ok=True)
        return RuntimeError(
            f"{zipPath} is damaged ({error}), so it was deleted. Run the installer"
            " again to download it again."
        )

    def _checkArchives(self, assetsZip: Path, serverZip: Path) -> None:
        contentDir = (
            Path(CONFIG["SERVER"]["EXPORT_DIR"]).absolute()
            / CONFIG["ASSETS"]["CONTENT_SUBDIR"]
        )
        try:
            with zipfile.ZipFile(assetsZip) as z:
                files = [p for i, p in self._assetMembers(z) if not i.is_dir()]
        except zipfile.BadZipFile as e:
            raise self._damaged(assetsZip, e)
        if not files:
            raise RuntimeError(
                f"{assetsZip} contained no assets/content or assets/mst files;"
                " delete it and run the installer again to download a fresh copy."
            )
        longest = max(len(str(contentDir.joinpath(*p[1:]))) for p in files)
        if longest > 259:
            raise RuntimeError(
                f"Some game files would have paths of {longest} characters, more than"
                " Windows allows. Choose an installation folder with a shorter path,"
                " such as C:\\Games\\BraveFrontier."
            )

        try:
            with zipfile.ZipFile(serverZip) as z:
                names = z.namelist()
        except zipfile.BadZipFile as e:
            raise self._damaged(serverZip, e)
        binary = CONFIG["SERVER"]["BINARY"]
        if not any(n.count("/") == 1 and n.endswith("/" + binary) for n in names):
            raise RuntimeError(f"{serverZip} does not contain the server ({binary}).")

    def _extractAssets(self, source: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)
        self.ctx.logger(Logline(f"Extracting server assets to {dest}..."))

        extracted = 0
        try:
            with zipfile.ZipFile(source, "r") as z:
                members = [(i, p) for i, p in self._assetMembers(z) if not i.is_dir()]
                for fileInfo, parts in members:
                    target = dest.joinpath(*parts[1:])
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(fileInfo, "r") as sourceFile:
                        with target.open("wb") as targetFile:
                            shutil.copyfileobj(sourceFile, targetFile)
                    extracted += 1
                    if extracted % 500 == 0 or extracted == len(members):
                        self.ctx.logger(
                            Logline(
                                f"Extracted {extracted:,} of {len(members):,} asset files",
                                clearLine=True,
                            )
                        )
        except zipfile.BadZipFile as e:
            raise self._damaged(source, e)

        # An archive without the assets/ prefix extracts nothing; the server would
        # then start with no game files, so stop here instead of reporting success.
        if extracted == 0:
            raise RuntimeError(
                f"{source} contained no assets/content or assets/mst files;"
                " delete it and run the installer again to download a fresh copy."
            )
        self.ctx.logger(Logline(f"Successfully extracted {extracted} asset files to {dest}"))

    def _extractServer(self, source: Path, dest: Path) -> None:
        dest.mkdir(parents=True, exist_ok=True)

        self.ctx.logger(Logline(f"Extracting server files to {dest}..."))
        try:
            with zipfile.ZipFile(source, "r") as z:
                for f in z.infolist():
                    # Skip directory entries since we will create parent dirs as needed
                    if f.is_dir():
                        continue
                    # Strip the top-level folder: 'server/app.exe' becomes 'app.exe'
                    parts = f.filename.replace("\\", "/").split("/")[1:]
                    if not parts:
                        continue
                    if any(part in ("", ".", "..") or ":" in part for part in parts):
                        raise RuntimeError(
                            f"Unsafe path in server archive: {f.filename}"
                        )
                    destPath = dest.joinpath(*parts)
                    destPath.parent.mkdir(parents=True, exist_ok=True)
                    with z.open(f) as s:
                        with open(destPath, "wb") as d:
                            shutil.copyfileobj(s, d)
        except zipfile.BadZipFile as e:
            raise self._damaged(source, e)

        if not (dest / CONFIG["SERVER"]["BINARY"]).exists():
            raise RuntimeError("Expected server binary not found after extraction!")
        self.ctx.logger(
            Logline(f"Successfully extracted server files to {dest}")
        )

    def _recoverInterruptedInstall(self, deployDir: Path, previous: Path) -> None:
        if not previous.exists():
            return
        if deployDir.exists() and self._marker(deployDir).exists():
            # The last install finished; only deleting the old folder was cut short.
            shutil.rmtree(previous, ignore_errors=True)
            return
        if deployDir.exists():
            shutil.rmtree(deployDir)
        previous.rename(deployDir)
        self.ctx.logger(
            Logline("Recovered the server folder from an interrupted install.")
        )

    def _backupSave(self, deployDir: Path) -> None:
        names = [n for n in CONFIG["SERVER"]["SAVE_FILES"] if (deployDir / n).exists()]
        if not names:
            return
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backupDir = Path(CONFIG["SERVER"]["BACKUP_DIR"]) / stamp
        backupDir.mkdir(parents=True, exist_ok=True)
        for name in names:
            shutil.copy2(deployDir / name, backupDir / name)
        self.ctx.saveBackupDir = backupDir
        self.ctx.logger(Logline(f"Backed up your save to {backupDir}"))

    def run(self) -> Exception | None:

        self.ctx.logger(header="Installing game server...")

        assetsZip = Path(CONFIG["ASSETS"]["SAVE_PATH"])
        serverZip = Path(CONFIG["SERVER"]["SAVE_PATH"])
        deployDir = Path(CONFIG["SERVER"]["EXPORT_DIR"])
        previous = deployDir.with_name(deployDir.name + ".previous")
        contentName = CONFIG["ASSETS"]["CONTENT_SUBDIR"]
        self._recoverInterruptedInstall(deployDir, previous)

        self.ctx.logger(
            Logline("Downloading server assets, this may take a while...")
        )
        download(self.ctx, CONFIG["ASSETS"]["URLS"], assetsZip)
        # Overwrite the server to pull new updates
        download(self.ctx, CONFIG["SERVER"]["URLS"], serverZip, overwrite=True)
        self._checkArchives(assetsZip, serverZip)

        hadPrevious = deployDir.exists()
        if hadPrevious:
            # Checked again here: the server may have been started during the
            # long download, and Windows lets a folder holding a running .exe
            # be renamed (an open file inside it does block the rename).
            Preflight(self.ctx)._checkServerNotRunning()
            try:
                deployDir.rename(previous)
            except OSError as e:
                raise RuntimeError(
                    f"Files in {deployDir.absolute()} are in use ({e.strerror}). Close"
                    " the server window and anything opened from that folder, then"
                    " run the installer again."
                )
            self._backupSave(previous)

        content = deployDir / contentName
        oldContent = previous / contentName
        movedContent = False
        removedOldContent = False
        try:
            self._extractServer(serverZip, deployDir)
            if hadPrevious and self.contentMatches(previous, assetsZip):
                if content.exists():
                    shutil.rmtree(content)
                oldContent.rename(content)
                movedContent = True
                self.ctx.logger(
                    Logline("assets.zip is unchanged, so the extracted game content was kept.")
                )
            else:
                if oldContent.exists():
                    # Outdated: delete it first, so the new copy has the space.
                    self._marker(previous).unlink(missing_ok=True)
                    removedOldContent = True
                    shutil.rmtree(oldContent)
                self._extractAssets(assetsZip, content)

            if hadPrevious:
                restored = False
                for name in CONFIG["SERVER"]["SAVE_FILES"]:
                    if (previous / name).exists():
                        shutil.copy2(previous / name, deployDir / name)
                        restored = True
                if restored:
                    self.ctx.logger(Logline("Restored your save into the new server."))
            self._marker(deployDir).write_text(
                json.dumps(self._zipStamp(assetsZip)), encoding="utf-8"
            )
        except Exception:
            if hadPrevious:
                if movedContent and content.exists() and not oldContent.exists():
                    content.rename(oldContent)
                shutil.rmtree(deployDir, ignore_errors=True)
                try:
                    previous.rename(deployDir)
                    self.ctx.logger(
                        Logline(
                            "The previous server folder and your save were put back."
                            + (
                                " Its outdated game content was already removed, so"
                                " run the installer again to finish."
                                if removedOldContent
                                else ""
                            )
                        )
                    )
                except OSError:
                    self.ctx.logger(
                        Logline(f"The previous server folder is in {previous.absolute()}")
                    )
            raise

        if hadPrevious:
            shutil.rmtree(previous, ignore_errors=True)
        self.ctx.serverBinaryPath = deployDir / CONFIG["SERVER"]["BINARY"]
        self.ctx.logger(Logline(f"Server setup complete!"))


class App:

    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("DecompFrontier Installer")
        root.geometry("500x250")
        root.resizable(True, False)

        self.header = tk.StringVar(
            value="Press the button below to begin installing DecompFrontier"
        )

        tk.Label(
            root,
            textvariable=self.header,
            font=("Segoe UI", 11),
        ).pack(pady=(16, 6))
        self.logBox = ScrolledText(
            root, height=10, width=60, state="disabled", wrap="word"
        )

        disclaimer = "\n".join(
            [
                "Disclaimer: This installer is an unofficial community project and is not ",
                "affiliated with or endorsed by the original game developers/publishers. ",
                "We do not own any of the game assets or original binaries. This is a ",
                "non-commercial project made for educational and preservation purposes. ",
                "By using this installer, you acknowledge that you have read and understood ",
                "the above statement. Please see https://decompfrontier.github.io/ for more ",
                "information about the project.",
            ]
        )
        self.disclaimerLabel = tk.Label(
            root,
            text=disclaimer,
        )
        self.disclaimerLabel.pack(pady=(16, 20))

        self.btn = tk.Button(root, text="Start", width=18, command=self.start)
        self.btn.pack(pady=(0, 10))

        # We use queues to store logs from the worker thread,
        # and the main thread will poll it to update the UI.
        self.logQueue = queue.Queue()
        self.root.after(100, self._update)
        # We track the previous log line to do some hacky in-place
        # updates for progress lines (clearLine=True).
        self.prevLog = None

        self.installDir: Path | None = None
        self.logPath: Path | None = None

    def start(self):
        initialDir = (
            Path(sys.executable).parent
            if getattr(sys, "frozen", False)
            else Path.cwd()
        )
        selectedDir = filedialog.askdirectory(
            parent=self.root,
            title="Select DecompFrontier installation folder",
            initialdir=str(initialDir),
            mustexist=True,
        )
        if not selectedDir:
            return

        self.installDir = Path(selectedDir).resolve()
        try:
            os.chdir(self.installDir)
            logDir = Path(CONFIG["LOG_DIR"])
            logDir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            messagebox.showerror(
                "Invalid Installation Folder",
                f"The selected folder cannot be used:\n\n{e}",
                parent=self.root,
            )
            return

        logId = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self.logPath = logDir / f"{logId}.log"

        # Widgets are changed here, on the Tk thread, before the worker starts.
        self.btn.config(state="disabled")
        self.disclaimerLabel.destroy()
        self.logBox.pack(
            padx=10, pady=(0, 10), fill="both", expand=True, before=self.btn
        )
        self.root.geometry("560x360")

        self.logger(Logline(f"Installation folder: {self.installDir}"))
        self.logger(Logline("Starting installation..."))
        threading.Thread(target=self.worker, daemon=True).start()

    # API for the worker thread to update the log message.
    def logger(self, log: Logline | None = None, header: str = ""):
        if log:
            self.logQueue.put(log)
            if (
                not log.clearLine
            ):  # Only write to log file if not a progress update
                assert self.logPath, "Log path must be set before logging"
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                with self.logPath.open("a", encoding="utf-8") as f:
                    if self.prevLog and self.prevLog.clearLine:
                        f.write(f"[{ts}] {self.prevLog.txt}\n")
                    f.write(f"[{ts}] {log.txt}\n")
        if header:
            # run on Tkinter (main) thread to avoid thread issues with StringVar
            self.root.after(0, lambda h=header: self.header.set(h))

    # Updates the status label and schedules itself to run again.
    def _update(self):
        # Drain all log lines
        while not self.logQueue.empty():
            self.logBox.config(state="normal")
            log = self.logQueue.get()
            if log.clearLine and self.prevLog and self.prevLog.clearLine:
                self.logBox.delete("end-2l", "end-1l")
            self.logBox.insert(tk.END, log.txt + "\n")
            self.logBox.see(tk.END)
            self.logBox.config(state="disabled")
            self.prevLog = log
        self.root.after(100, self._update)

    def worker(self):
        ctx = Context(logger=self.logger)

        errored = False
        try:
            Preflight(ctx).run()
            CheckInstalledClient(ctx).run()
            if ctx.clientCurrent:
                steps = [EnsureLoopback]
            else:
                steps = [
                    SetupOfflineProxy,
                    FetchWindowsSDKTools,
                    GenerateDeveloperCert,
                    TrustCertAndEnableLoopback,
                    PatchGameClient,
                ]
            for work in steps + [SetupGameServer]:
                work(ctx=ctx).run()
        except Exception as e:
            errored = True
            self.logger(Logline(f"Error: {e}"), header="Installation stopped")
            if self.logPath:
                self.logger(
                    Logline(
                        f"The full log is {self.logPath.absolute()}; please attach it"
                        " when you ask for help."
                    )
                )
                with self.logPath.open("a", encoding="utf-8") as f:
                    f.write(traceback.format_exc())
        finally:
            removeSigningCert(ctx)

        self.root.after(
            0,
            lambda: self.btn.config(
                state="normal", text="Finish", command=self.root.quit
            ),
        )

        if errored:
            return

        assert (
            ctx.serverBinaryPath
        ), "The server binary path should be set in context!"

        starter = ctx.serverBinaryPath.parent / "Start Server.bat"
        launch = starter if starter.exists() else ctx.serverBinaryPath
        message = (
            "\nYou have now completed the entire installation process of DecompFrontier!\n"
            f"1. Start the server: open {launch.absolute()} and leave its window open"
            " while you play.\n"
            "2. Start Brave Frontier from the Start menu. The first start downloads the"
            " game data from your server, which can take 30 minutes or more. (Opening"
            " a BraveFrontier.Windows.exe file directly fails with \"msvcr120_app.dll"
            " was not found\": Windows supplies that DLL only to the installed app.)\n"
            + (
                "\nAnother Brave Frontier, not made by this installer, is also installed"
                " and cannot reach your server. To remove it, run this in PowerShell:\n"
                + "\n".join(f"  Remove-AppxPackage {name}" for name in ctx.otherClients)
                + "\n"
                if ctx.otherClients
                else ""
            )
            + (
                f"Your save was kept; a copy is in {ctx.saveBackupDir.absolute()}.\n"
                if ctx.saveBackupDir
                else ""
            )
            + "\nPlease direct all questions, issues, and feedback to the DecompFrontier discord server. "
            "Thank you for using this installer, and have fun! :)"
        )
        self.logger(Logline(message), header="Installation complete")


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

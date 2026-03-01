import cmd
import io
import subprocess
from dataclasses import dataclass
import queue
import re
from typing import Any
import os
import stat
import shutil
import datetime
import sys
import tkinter as tk
from tkinter.scrolledtext import ScrolledText
from tkinter import messagebox
import threading
import json
from typing import Callable
import urllib.request
from pathlib import Path
import winreg
import zipfile
import gdown

LOGGER = Callable[..., None]

CONFIG = {
    "LOG_DIR": "runs",
    "PROXY_DLL": {
        "OWNER": "Tom2096",
        "REPO": "offline-proxy-fork",
        "TAG": "v0.0.1",
        "SAVE_DIR": "build",
    },
    "WINDOWS_SDK_ID": "Microsoft.WindowsSDK.10.0.26100",
    "CERT_PATH": "build/myKey.pfx",
    "APPX": {
        "URL": "https://drive.google.com/uc?export=download&id=1NB64gzQOe-QQx9fY0mkoZiCSfe3WlTYi",
        "SAVE_DIR": "build",
        "UNPACK_DIR": "build/BraveFrontierAppxClient",
        "PATCHED_PATH": "build/BraveFrontierPatched.appx",
    },
    "ASSETS": {
        "URL": "https://drive.google.com/uc?export=download&id=1ApVcJISPovYuWEidnkkTJi_NI8sD1Xmx",
        "SAVE_DIR": "build",
        "EXPORT_DIR": "deploy/game_content",
    },
    "SERVER": {
        "OWNER": "Tom2096",
        "REPO": "server",
        "TAG": "v0.0.1",
        "SAVE_DIR": "build",
        "EXPORT_DIR": "deploy",
    },
}


# Context object to hold shared state and utilities for the setup process.
@dataclass
class Context:
    logger: LOGGER
    offlineproxyPath: Path | None = None
    makeappxPath: Path | None = None
    signtoolPath: Path | None = None
    certName: str | None = None
    certPassword: str | None = None
    patchedAppxPath: Path | None = None
    serverBinaryPath: Path | None = None


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


def fetchGithubRelease(releaseApi: str) -> tuple[str, str]:
    req = urllib.request.Request(
        releaseApi,
        headers={
            "User-Agent": "SimpleDownloader",
            "Accept": "application/vnd.github+json",
        },
    )

    with urllib.request.urlopen(req) as r:
        release = json.loads(r.read().decode("utf-8"))

    assets = release.get("assets", [])
    if not assets:
        raise RuntimeError("No assets found in this release.")

    # Pick the first asset
    asset = assets[0]
    name = asset.get("name")
    url = asset.get("browser_download_url")
    if not name or not url:
        raise RuntimeError("Release asset is missing name or download URL.")

    return name, url


def download(ctx: Context, url: str, dest: Path) -> None:
    if dest.exists():
        ctx.logger(Logline(f"File already exists at {dest}, skipping download"))
        return

    ctx.logger(Logline(f"Download started for {dest}"))
    dest.parent.mkdir(parents=True, exist_ok=True)

    old_stdout = sys.stdout
    old_stderr = sys.stderr

    stream = StreamToLogger(ctx)
    sys.stdout = stream
    sys.stderr = stream

    gdown.download(url, output=str(dest), quiet=False)

    sys.stdout = old_stdout
    sys.stderr = old_stderr

    ctx.logger(Logline(f"Download complete: {dest}"))


def runSubprocess(ctx: Context, cmd: list[str], **kwargs: Any) -> int:
    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        **kwargs,
    )
    stream = StreamToLogger(ctx)
    if p.stdout:
        for char_byte in iter(lambda: p.stdout.read(1), b""):  # type: ignore
            char = char_byte.decode("utf-8", errors="replace")  # type: ignore
            stream.write(char)

    return p.wait()


class SetupOfflineProxy:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def run(self) -> Exception | None:
        self.ctx.logger(header="Fetching offline-proxy...")

        releaseApi = "/".join(
            [
                "https://api.github.com/repos",
                CONFIG["PROXY_DLL"]["OWNER"],
                CONFIG["PROXY_DLL"]["REPO"],
                "releases/tags",
                CONFIG["PROXY_DLL"]["TAG"],
            ]
        )
        name, url = fetchGithubRelease(releaseApi)
        self.ctx.offlineproxyPath = Path(CONFIG["PROXY_DLL"]["SAVE_DIR"]) / name
        download(self.ctx, url, self.ctx.offlineproxyPath)


class FetchWindowsSDKTools:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _findWindowsSDK(self) -> Path | None:
        key = r"SOFTWARE\Microsoft\Windows Kits\Installed Roots"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                for name in ("KitsRoot10", "KitsRoot81"):
                    try:
                        val, _ = winreg.QueryValueEx(k, name)
                        if val and Path(val).exists():
                            return Path(val)
                    except FileNotFoundError:
                        pass
        except FileNotFoundError:
            return None
        return None

    _VERSION_RE = re.compile(r"^\d+\.\d+\.\d+\.\d+$")

    def _parseVer(self, v: str) -> tuple[int, int, int, int]:
        # Version string is expected to be four dot-separated integers.
        a, b, c, d = v.split(".")
        return int(a), int(b), int(c), int(d)

    def _findSDKTools(self) -> tuple[Path | None, Path | None]:
        sdkRoot = self._findWindowsSDK()
        if not sdkRoot or not (sdkRoot / "bin").exists():
            return None, None

        hasVersioned = any(
            p.is_dir() and self._VERSION_RE.match(p.name)
            for p in (sdkRoot / "bin").iterdir()
        )

        if hasVersioned:
            # treat like KitsRoot10 (10/11) layout: bin/<version>/<arch>/
            versions = [
                p
                for p in (sdkRoot / "bin").iterdir()
                if p.is_dir() and self._VERSION_RE.match(p.name)
            ]
            latest = max(versions, key=lambda p: self._parseVer(p.name))

            for arch in ("x64", "x86"):
                makeappx = latest / arch / "makeappx.exe"
                signtool = latest / arch / "signtool.exe"
                if makeappx.exists() and signtool.exists():
                    return makeappx, signtool
            return None, None
        else:
            # treat like KitsRoot81 (8.1) layout: bin\<arch>\makeappx.exe
            for arch in ("x64", "x86"):
                makeappx = sdkRoot / "bin" / arch / "makeappx.exe"
                signtool = sdkRoot / "bin" / arch / "signtool.exe"
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
                f"https://developer.microsoft.com/windows/downloads/windows-10-sdk/ and"
                f"then re-run this installer."
            )
        self.ctx.logger(Logline("Windows SDK installation complete!"))

    def run(self) -> Exception | None:
        self.ctx.logger(header="Discovering Windows SDK tools...")

        self.ctx.makeappxPath, self.ctx.signtoolPath = self._findSDKTools()
        if self.ctx.makeappxPath and self.ctx.signtoolPath:
            self.ctx.logger(
                Logline(f"Found makeappx.exe at {self.ctx.makeappxPath}")
            )
            self.ctx.logger(
                Logline(f"Found signtool.exe at {self.ctx.signtoolPath}")
            )
            return

        self.ctx.logger(
            Logline(
                "Failed to find tools, attempting to install Windows SDK..."
            )
        )
        self._installWindowsSDK()

        self.ctx.makeappxPath, self.ctx.signtoolPath = self._findSDKTools()
        assert (
            self.ctx.makeappxPath and self.ctx.signtoolPath
        ), "Tools still not found after SDK installation!"

        self.ctx.logger(
            Logline(f"Found makeappx.exe at {self.ctx.makeappxPath}")
        )
        self.ctx.logger(
            Logline(f"Found signtool.exe at {self.ctx.signtoolPath}")
        )


class GenerateDeveloperCert:
    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.thumbprint = None

    def _parseThumbprint(self, result: str) -> None:
        # Thumbprints are exactly 40 characters of 0-9 and A-F
        match = re.search(r"([A-F0-9]{40})", result, re.IGNORECASE)
        if match:
            self.thumbprint = match.group(1)

    _ARGS = [
        "powershell",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
    ]

    def _createCert(self) -> None:
        # Create a self-signed certificate using PowerShell's New-SelfSignedCertificate cmdlet.
        cmd = (
            f'New-SelfSignedCertificate -Type Custom -Subject "CN={self.ctx.certName}" '
            '-KeyUsage DigitalSignature -FriendlyName "My Developer Certificate" '
            '-CertStoreLocation "Cert:\\CurrentUser\\My" '
            '-TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3", "2.5.29.19={text}") '
            "-KeyExportPolicy Exportable; "
        )

        p = subprocess.run(
            self._ARGS + [cmd],
            text=True,
            check=True,
            encoding="utf-8",
            capture_output=True,
        )
        assert (
            p.stdout
        ), "Expected thumbprint output from cert creation command!"
        self._parseThumbprint(p.stdout.strip())
        self.ctx.logger(
            Logline(f"Certificate created with thumbprint: {self.thumbprint}")
        )

    def _exportCert(self) -> None:
        Path(CONFIG["CERT_PATH"]).parent.mkdir(parents=True, exist_ok=True)

        cmd = (
            f'$pwd = ConvertTo-SecureString -String "{self.ctx.certPassword}" -Force -AsPlainText; '
            f'Export-PfxCertificate -Cert "Cert:\\CurrentUser\\My\\{self.thumbprint}" '
            f'-FilePath "{Path(CONFIG["CERT_PATH"])}" -Password $pwd'
        )

        self.ctx.logger(
            Logline(f"Exporting certificate to {CONFIG['CERT_PATH']}...")
        )
        if (runSubprocess(self.ctx, self._ARGS + [cmd])) != 0:
            raise RuntimeError("Certificate export failed.")
        assert Path(
            CONFIG["CERT_PATH"]
        ).exists(), "PFX file not found after export!"
        self.ctx.logger(
            Logline(
                f"Successfully exported certificate to {Path(CONFIG['CERT_PATH'])}"
            )
        )

        # Deletes the cert from the virtual 'Personal' store
        cleanup_cmd = f'Remove-Item -Path "Cert:\\CurrentUser\\My\\{self.thumbprint}" -ErrorAction SilentlyContinue'

        self.ctx.logger(Logline("Cleaning up certificate from store..."))
        if (runSubprocess(self.ctx, self._ARGS + [cleanup_cmd])) != 0:
            raise RuntimeError("Certificate cleanup failed.")
        self.ctx.logger(
            Logline(f"Successfully cleaned up cert: {self.thumbprint}")
        )

    def run(self) -> Exception | None:
        self.ctx.logger(header="Generating developer certificate...")
        self._createCert()
        self._exportCert()

        message = (
            "\nIMPORTANT! "
            "You should now follow the 'Installing the Certificate' section "
            "from the link below to verify the generated certificate: "
            "https://decompfrontier.github.io/pages/Tutorial/dev-client-winrt.html. "
            f"Your key file is located at:\n{Path(CONFIG['CERT_PATH']).absolute()}\n"
        )
        self.ctx.logger(Logline(message))


class PatchGameClient:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _unpackAppx(self, appxPath: str) -> None:
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
            appxPath,
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

        shutil.copy2(
            self.ctx.offlineproxyPath,  # type: ignore
            Path(CONFIG["APPX"]["UNPACK_DIR"]) / self.ctx.offlineproxyPath.name,  # type: ignore
        )

        unpackDir = Path(CONFIG["APPX"]["UNPACK_DIR"])
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

        target = "5AA816A3-ED94-4AA2-A2B4-3ADDA1FABFB6"
        manifest = (unpackDir / "AppxManifest.xml").read_text(encoding="utf-8")
        if target in manifest:
            content = manifest.replace(target, self.ctx.certName)  # type: ignore
            (unpackDir / "AppxManifest.xml").write_text(
                content, encoding="utf-8"
            )
            self.ctx.logger(
                Logline(f"Successfully replaced CN with: {self.ctx.certName}")
            )
        else:
            raise RuntimeError(
                f"Target CN {target} not found in manifest. Cannot patch client!"
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

        sign_cmd = [
            str(self.ctx.signtoolPath),
            "sign",
            "/a",
            "/v",
            "/fd",
            "SHA256",
            "/f",
            CONFIG["CERT_PATH"],
            "/p",
            self.ctx.certPassword,
            CONFIG["APPX"]["PATCHED_PATH"],
        ]

        self.ctx.logger(Logline("Signing the patched client..."))
        if (runSubprocess(self.ctx, sign_cmd)) != 0:
            raise RuntimeError(f"signtool failed to sign the client.")
        self.ctx.logger(Logline("Client signed successfully!"))

        self.ctx.patchedAppxPath = Path(CONFIG["APPX"]["PATCHED_PATH"])

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
        assert self.ctx.certName, "Certificate name is not set in context!"
        assert (
            self.ctx.certPassword
        ), "Certificate password is not set in context!"

        self.ctx.logger(header="Patching game client...")

        dest = CONFIG["APPX"]["SAVE_DIR"] + "/client.appx"
        download(self.ctx, CONFIG["APPX"]["URL"], Path(dest))

        self._unpackAppx(dest)
        self._patchAppx()
        self._packAppx()


class SetupGameServer:
    def __init__(self, ctx: Context):
        self.ctx = ctx

    def _extractAssets(self) -> None:
        dest = Path(CONFIG["ASSETS"]["EXPORT_DIR"])
        dest.mkdir(parents=True, exist_ok=True)
        source = CONFIG["ASSETS"]["SAVE_DIR"] + "/21900.zip"

        self.ctx.logger(Logline(f"Extracting server assets to {dest}..."))

        with zipfile.ZipFile(source, "r") as z:
            with z.open("assets.zip") as assets:
                bytes = io.BytesIO(assets.read())
                with zipfile.ZipFile(bytes) as inner:
                    for file in z.infolist():
                        if file.filename.startswith(("content/", "mst/")):
                            inner.extract(file, dest)

        self.ctx.logger(Logline(f"Successfully extracted assets to {dest}"))

    def _extractServer(self, source: Path) -> None:
        dest = Path(CONFIG["SERVER"]["EXPORT_DIR"])
        dest.mkdir(parents=True, exist_ok=True)

        self.ctx.logger(Logline(f"Extracting server files to {dest}..."))
        with zipfile.ZipFile(source, "r") as z:
            for f in z.infolist():
                parts = f.filename.split("/")

                # Skip the top-level folder entry itself
                if len(parts) <= 1:
                    continue

                # Create the new path by joining everything AFTER the first part
                # Example: 'server/app.exe' becomes 'app.exe'
                subPath = Path(os.path.join(*parts[1:]))
                destPath = dest / subPath

                # Skip directory entries since we will create parent dirs as needed
                if f.is_dir():
                    continue

                destPath.parent.mkdir(parents=True, exist_ok=True)
                with z.open(f) as s:
                    with open(destPath, "wb") as d:
                        shutil.copyfileobj(s, d)

        assert (
            dest / "gimuserverw.exe"
        ).exists(), "Expected server binary not found after extraction!"
        self.ctx.serverBinaryPath = dest / "gimuserverw.exe"

        self.ctx.logger(
            Logline(f"Successfully extracted server files to {dest}")
        )

    def run(self) -> Exception | None:

        self.ctx.logger(header="Installing game server...")

        dest = CONFIG["ASSETS"]["SAVE_DIR"] + "/21900.zip"
        self.ctx.logger(
            Logline("Downloading server assets, this may take a while...")
        )
        download(self.ctx, CONFIG["ASSETS"]["URL"], Path(dest))

        self._extractAssets()

        releaseApi = "/".join(
            [
                "https://api.github.com/repos",
                CONFIG["SERVER"]["OWNER"],
                CONFIG["SERVER"]["REPO"],
                "releases/tags",
                CONFIG["SERVER"]["TAG"],
            ]
        )
        name, url = fetchGithubRelease(releaseApi)
        dest = Path(CONFIG["SERVER"]["SAVE_DIR"]) / name
        download(self.ctx, url, dest)

        self._extractServer(dest)
        self.ctx.logger(Logline(f"Server setup complete!"))


class CredentialsDialog(tk.Toplevel):
    def __init__(self, parent):
        super().__init__(parent)
        self.title("Certificate Credentials")
        self.geometry("320x300")
        self.resizable(False, False)
        self.result = None

        self.protocol("WM_DELETE_WINDOW", self.onCancel)

        tk.Label(
            self,
            text="To create a developer certificate, please provide the following information. "
            "A certificate will be generated and used to sign the app package. "
            "This is required to run the the Brave Frontier client on your machine.",
            wraplength=280,
        ).pack(pady=(15, 0))

        tk.Label(self, text="Common Name (CN):").pack(pady=(15, 0))
        self.name_entry = tk.Entry(self, width=35)
        self.name_entry.insert(0, "BraveFrontier")
        self.name_entry.pack(pady=5)

        tk.Label(self, text="Private Key Password:").pack(pady=(10, 0))
        self.pwd_entry = tk.Entry(self, width=35, show="*")
        self.pwd_entry.pack(pady=5)

        tk.Button(self, text="Confirm", width=15, command=self.onSubmit).pack(
            pady=20
        )

        # Set to be on top of main window.
        self.transient(parent)
        # Prevents interaction with main window.
        self.grab_set()
        self.pwd_entry.focus_set()
        parent.wait_window(self)

    def onSubmit(self) -> None:
        cn = self.name_entry.get().strip()
        pwd = self.pwd_entry.get()
        if not cn or not pwd:
            messagebox.showwarning(
                "Input Required", "Please provide both a name and a password."
            )
            return
        self.result = (cn, pwd)
        self.destroy()

    def onCancel(self) -> None:
        self.result = None
        self.destroy()


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

        # Set up logging directory with timestamped log file.
        id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        logDir = Path(CONFIG["LOG_DIR"])
        self.logPath = logDir / f"{id}.log"
        Path(CONFIG["LOG_DIR"]).mkdir(parents=True, exist_ok=True)

        # Used to pause/unpause during the installation process.
        self.continueEvent = threading.Event()

    def start(self):
        self.btn.config(state="disabled")
        self.logger(Logline("Starting installation..."))
        threading.Thread(target=self.worker, daemon=True).start()

    # API for the worker thread to update the log message.
    def logger(self, log: Logline | None = None, header: str = ""):
        if log:
            self.logQueue.put(log)
            if (
                not log.clearLine
            ):  # Only write to log file if not a progress update
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

        dialog = CredentialsDialog(self.root)
        if not dialog.result:
            self.root.quit()
            return

        self.disclaimerLabel.destroy()

        ctx = Context(
            logger=self.logger,
            certName=dialog.result[0],
            certPassword=dialog.result[1],
        )

        self.logBox.pack(
            padx=10, pady=(0, 10), fill="both", expand=True, before=self.btn
        )
        self.root.geometry("560x360")

        errored = False
        for work in [
            SetupOfflineProxy,
            FetchWindowsSDKTools,
            GenerateDeveloperCert,
        ]:
            try:
                work(ctx=ctx).run()
            except Exception as e:
                self.logger(Logline(f"Error: {e}"))
                errored = True
                break

        if errored:
            self.btn.config(
                state="normal", text="Finish", command=self.root.quit
            )
            return

        # We will wait for the user to verify the cert installation via
        # the Windows GUI.
        self.btn.config(
            state="normal",
            text="I have verified the certificate",
            width=30,
            command=self.continueEvent.set,
        )
        self.continueEvent.wait()
        self.continueEvent.clear()
        self.btn.config(state="disabled")

        for work in [PatchGameClient, SetupGameServer]:
            try:
                work(ctx=ctx).run()
            except Exception as e:
                self.logger(Logline(f"Error: {e}"))
                errored = True
                break

        self.btn.config(state="normal", text="Finish", command=self.root.quit)

        if errored:
            return

        assert (
            ctx.patchedAppxPath and ctx.serverBinaryPath
        ), "Patched APPX path and server binary path should be set in context!"

        message = (
            "\nYou have now completed the entire installation process of DecompFrontier! "
            "You should now be able to install the patched client APPX in "
            f"{ctx.patchedAppxPath.absolute()}. "
            f"You can launch the server at {ctx.serverBinaryPath.absolute()}. "
            "It is highly recommended to read and follow the 'Connecting to the Server' section "
            "in https://decompfrontier.github.io/pages/Tutorial/dev-client-winrt.html "
            "to understand how to connect the client to the server. \n"
            "\nPlease direct all questions, issues, and feedback to the DecompFrontier discord server. "
            "Thank you for using this installer, and have fun! :)"
        )

        if not errored:
            self.logger(Logline(message))


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

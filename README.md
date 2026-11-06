## Binary Installation (Recommended)

For most users, using the pre-compiled binary is the simplest method:

1. **Download**: Grab the latest `DecompInstaller.exe` from the [Releases](https://github.com/Tom2096/DecompFrontier-Installer/releases/tag/v0.0.3) page.
2. **Run**: Place the `.exe` wherever convenient and double-click it.
3. **Choose a destination**: Press **Start**, then select the folder where DecompFrontier should be installed (e.g., `C:\Games\BraveFrontier`).

### Notes:
* You do not need to install Python or any external tools manually.
* On its first run, the installer will automatically download all necessary dependencies, libraries, and game files into the folder you selected.
* If you encounter an error, simply review the log for details and restart the installer. If any packages or dependencies were successfully downloaded during a previous attempt, the installer will detect them and skip those steps.

### Post Installation

1. **Start the server**: open `deploy\Start Server.bat` in your install folder and leave its window open while you play.
2. **Start the game**: launch Brave Frontier from the Start menu. The first start downloads the game data from your server, which can take 30 minutes or more.

Windows asks once for administrator permission during the install. That one prompt trusts the installer's signing certificate and adds the **loopback exemption**: by default Windows blocks UWP/WinRT apps (like Brave Frontier) from connecting to a server on the same machine. If you ever need to add the exemption again, run `deploy\Enable Client Loopback.ps1` as administrator, or follow the [Network Connection Guide](https://decompfrontier.github.io/pages/Tutorial/dev-client-winrt.html#connecting-to-the-server).

### Updating

Run the installer again and choose the same folder. It downloads only what changed. Your save (`deploy\gme.sqlite`) is kept, and a copy goes to `build\save_backups`. If the client is already current, the client steps are skipped. If only the offline proxy changed, the client is updated in place and keeps its downloaded game data.

### "msvcr120_app.dll / vccorlib120_app.dll was not found"

These DLLs come from Microsoft's VCLibs 12.0 package, and Windows provides them only to the **installed** app. Start the game from the Start menu, not by opening a `BraveFrontier.Windows.exe` file. If the Start menu entry shows the error too, run the installer again: it checks that the installed game and its VCLibs package are complete, and reinstalls them if not.

### Troubleshooting & Persistence

Most often, the source of an error will be clear from reading the installation log. If you need assistance:

1. **Locate your Logs**: Logs are archived in the `runs` folder within your install folder.
2. **Contact for help**: Whenever you reach out for help, please **attach a copy of your logs from your last run**. This is essential for diagnosing the issue.

## Running from Source

If you wish to compile the binary yourself:

### 1. Prerequisites
In your working directory
```
# Create the environment
python -m venv venv

# Activate it (Windows)
.\venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Build Command
Run this in the project root:

```bash
pyinstaller --onefile --noconsole --name "DecompInstaller" main.py

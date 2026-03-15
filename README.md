## Binary Installation (Recommended)

For most users, using the pre-compiled binary is the simplest method:

1. **Download**: Grab the latest `DecompInstaller.exe` from the [Releases](https://github.com/Tom2096/DecompFrontier-Installer/releases/tag/V0.0.1) page.
2. **Placement**: Move the `.exe` into any folder of your choice (e.g., `C:\Games\BraveFrontier`).
3. **Run**: Double-click the installer. 

### Notes:
* You do not need to install Python or any external tools manually.
* On its first run, the installer will automatically download all necessary dependencies, libraries, and game files into the folder where the `.exe` is located.
* If you encounter an error, simply review the log for details and restart the installer. If any packages or dependencies were successfully downloaded during a previous attempt, the installer will detect them and skip those steps.

### ⚠️ CRITICAL: Post Installation

**Loopback Exemption is mandatory.** Before attempting to launch the client, you must follow the steps in the [Network Connection Guide](https://decompfrontier.github.io/pages/Tutorial/dev-client-winrt.html#connecting-to-the-server).

**Why this matters:** By default, Windows blocks UWP/WinRT applications (like Brave Frontier) from connecting to local servers on the same machine. Without enabling loopback, the client will fail to connect and will likely time out or show a connection error.

### Troubleshooting & Persistence

Most often, the source of an error will be clear from reading the installation log. If you need assistance:

1. **Locate your Logs**: Logs are archived in the `/run` directory within your install folder.
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
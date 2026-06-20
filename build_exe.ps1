$ErrorActionPreference = "Stop"

$appName = "Saint Petersburg Rail Path Finder"

Write-Host "== Building $appName =="
Write-Host "Project folder: $PWD"

try {
    python --version
} catch {
    Write-Host ""
    Write-Host "ERROR: Khong tim thay python trong PATH."
    Write-Host "Hay cai Python tu python.org va tich 'Add python.exe to PATH', sau do mo lai PowerShell."
    exit 1
}

if (!(Test-Path ".venv\Scripts\python.exe")) {
    Write-Host "Creating virtual environment..."
    python -m venv .venv
}

Write-Host "Installing dependencies..."
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install pyinstaller -r requirements.txt

Write-Host "Checking Tkinter/Tcl..."
.\.venv\Scripts\python.exe -c "import tkinter; t=tkinter.Tcl(); print('Tcl/Tk OK:', t.eval('info patchlevel'))"

Write-Host "Running PyInstaller..."
.\.venv\Scripts\python.exe -m PyInstaller `
    --noconfirm `
    --clean `
    --windowed `
    --onedir `
    --name "$appName" `
    --collect-all customtkinter `
    --collect-all tkintermapview `
    --collect-all osmnx `
    --collect-all geopandas `
    --collect-all fiona `
    --collect-all pyproj `
    --collect-all shapely `
    --collect-all certifi `
    app.py

Write-Host "Copying seed cache/database..."
$bundleCache = Join-Path $PWD "dist\$appName\_internal\cache"
New-Item -ItemType Directory -Force -Path $bundleCache | Out-Null

$seedFiles = @(
    "spb_rail.pkl",
    "spb_walk.pkl",
    "spb_station_nodes.json",
    "spb_station_records.json",
    "search_history.sqlite3"
)

foreach ($fileName in $seedFiles) {
    $source = Join-Path $PWD "cache\$fileName"
    if (Test-Path $source) {
        Copy-Item -Force $source (Join-Path $bundleCache $fileName)
        Write-Host "  added cache\$fileName"
    } else {
        Write-Host "  skip missing cache\$fileName"
    }
}

Write-Host "Creating zip package..."
$zipPath = Join-Path $PWD "dist\$appName.zip"
if (Test-Path $zipPath) {
    Remove-Item -Force $zipPath
}
Compress-Archive -Path "$PWD\dist\$appName\*" -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Done."
Write-Host "EXE path: $PWD\dist\$appName\$appName.exe"
Write-Host "When sharing, send the whole folder: $PWD\dist\$appName"
Write-Host "Or send zip file: $zipPath"

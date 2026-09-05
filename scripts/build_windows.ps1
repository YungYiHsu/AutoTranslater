param(
    [string]$Version = "0.1.0"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$specPath = Join-Path $projectRoot "AutoTranslater.spec"
$configPath = Join-Path $projectRoot "config.json"
$releaseDirectory = Join-Path $projectRoot "dist\AutoTranslater"
$packageDirectory = Join-Path $projectRoot "release"
$packagePath = Join-Path $packageDirectory "AutoTranslater-v$Version-win64.zip"

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "版本號必須使用 x.y.z 格式。"
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "找不到專案虛擬環境，請先執行 uv sync --dev。"
}
if (-not (Test-Path -LiteralPath $configPath)) {
    throw "找不到 config.json，無法建立可用的發布包。"
}
try {
    $releaseConfig = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 |
        ConvertFrom-Json
}
catch {
    throw "config.json 不是有效的 JSON，無法建立發布包。"
}
if ([string]::IsNullOrWhiteSpace([string]$releaseConfig.model)) {
    throw "config.json 的 model 不可為空白，無法建立正式發布包。"
}

Push-Location $projectRoot
try {
    & $pythonPath -m PyInstaller --noconfirm --clean $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 建置失敗，結束代碼：$LASTEXITCODE"
    }

    Copy-Item -LiteralPath $configPath -Destination $releaseDirectory -Force
    New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
    if (Test-Path -LiteralPath $packagePath) {
        Remove-Item -LiteralPath $packagePath -Force
    }
    $packageItems = @(
        (Join-Path $releaseDirectory "AutoTranslater.exe"),
        (Join-Path $releaseDirectory "config.json"),
        (Join-Path $releaseDirectory "_internal")
    )
    Compress-Archive -LiteralPath $packageItems -DestinationPath $packagePath -CompressionLevel Optimal
    $packageHash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash

    Write-Host "建置完成：$releaseDirectory"
    Write-Host "config.json 已放在 AutoTranslater.exe 同層。"
    Write-Host "發布 ZIP：$packagePath"
    Write-Host "SHA-256：$packageHash"
}
finally {
    Pop-Location
}

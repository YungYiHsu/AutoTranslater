param(
    [string]$Version = "0.3.0"
)

$ErrorActionPreference = "Stop"

$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$specPath = Join-Path $projectRoot "AutoTranslater.spec"
$releaseDirectory = Join-Path $projectRoot "dist\AutoTranslater"
$packageDirectory = Join-Path $projectRoot "release"
$packagePath = Join-Path $packageDirectory "AutoTranslater-v$Version-win64.zip"
$stagingRoot = Join-Path $projectRoot "build\release-staging"
$stagingDirectory = Join-Path $stagingRoot "AutoTranslater"

if ($Version -notmatch '^\d+\.\d+\.\d+$') {
    throw "版本號必須使用 x.y.z 格式。"
}

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "找不到專案虛擬環境，請先執行 uv sync --dev。"
}
Push-Location $projectRoot
try {
    & $pythonPath -m PyInstaller --noconfirm --clean --distpath $stagingRoot $specPath
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller 建置失敗，結束代碼：$LASTEXITCODE"
    }

    $releaseConfig = [ordered]@{
        model = "gemini-3.5-flash"
        saved_models = @()
        output_directory = "outputs"
        chunk_size = 4000
        retry_attempts = 0
    }
    $releaseConfig | ConvertTo-Json -Depth 3 |
        Set-Content -LiteralPath (Join-Path $stagingDirectory "config.json") -Encoding UTF8

    New-Item -ItemType Directory -Path $releaseDirectory -Force | Out-Null
    $resolvedReleaseDirectory = (Resolve-Path -LiteralPath $releaseDirectory).Path
    $internalDirectory = Join-Path $resolvedReleaseDirectory "_internal"
    if (Test-Path -LiteralPath $internalDirectory) {
        $resolvedInternalDirectory = (Resolve-Path -LiteralPath $internalDirectory).Path
        if (
            [System.IO.Path]::GetDirectoryName($resolvedInternalDirectory) -ne
                $resolvedReleaseDirectory -or
            [System.IO.Path]::GetFileName($resolvedInternalDirectory) -ne "_internal"
        ) {
            throw "拒絕清理預期範圍以外的 _internal 目錄。"
        }
        Remove-Item -LiteralPath $resolvedInternalDirectory -Recurse -Force
    }
    Copy-Item -LiteralPath (Join-Path $stagingDirectory "AutoTranslater.exe") `
        -Destination (Join-Path $releaseDirectory "AutoTranslater.exe") -Force
    Copy-Item -LiteralPath (Join-Path $stagingDirectory "_internal") `
        -Destination $releaseDirectory -Recurse -Force
    if (-not (Test-Path -LiteralPath (Join-Path $releaseDirectory "config.json"))) {
        Copy-Item -LiteralPath (Join-Path $stagingDirectory "config.json") `
            -Destination (Join-Path $releaseDirectory "config.json")
    }

    New-Item -ItemType Directory -Path $packageDirectory -Force | Out-Null
    if (Test-Path -LiteralPath $packagePath) {
        Remove-Item -LiteralPath $packagePath -Force
    }
    $packageItems = @(
        (Join-Path $stagingDirectory "AutoTranslater.exe"),
        (Join-Path $stagingDirectory "config.json"),
        (Join-Path $stagingDirectory "_internal")
    )
    Compress-Archive -LiteralPath $packageItems -DestinationPath $packagePath -CompressionLevel Optimal
    $packageHash = (Get-FileHash -LiteralPath $packagePath -Algorithm SHA256).Hash

    Write-Host "建置完成：$releaseDirectory"
    Write-Host "本地使用者資料與設定已保留。"
    Write-Host "發布 ZIP 使用乾淨的預設 config.json。"
    Write-Host "發布 ZIP：$packagePath"
    Write-Host "SHA-256：$packageHash"
}
finally {
    Pop-Location
}

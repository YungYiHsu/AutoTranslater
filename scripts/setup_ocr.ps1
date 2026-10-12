param()

$ErrorActionPreference = 'Stop'
$ocrProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$ocrEnvironment = Join-Path $ocrProjectRoot 'runtime\ocr\venv'
$ocrPython = Join-Path $ocrEnvironment 'Scripts\python.exe'
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Please install uv, then run this script again.'
}
if (-not (Test-Path -LiteralPath $ocrPython)) {
    if (Test-Path -LiteralPath $ocrEnvironment) {
        throw 'An incomplete OCR environment already exists; inspect it before proceeding.'
    }
    & uv venv $ocrEnvironment --python 3.12
    if ($LASTEXITCODE -ne 0) { throw 'Could not create the OCR environment.' }
}
& $ocrPython -c "import sys; assert sys.version_info[:2] == (3, 12), 'OCR requires Python 3.12'"
if ($LASTEXITCODE -ne 0) { throw 'Unexpected OCR Python version.' }
& uv pip install --python $ocrPython -r (Join-Path $ocrProjectRoot 'requirements\ocr-torch.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install CPU PyTorch.' }
& uv pip install --python $ocrPython -r (Join-Path $ocrProjectRoot 'requirements\ocr.txt')
if ($LASTEXITCODE -ne 0) { throw 'Could not install OCR dependencies.' }
& $ocrPython -c "import tkinter, torch, torchvision, yomitoku, cv2; from PIL import Image; print('OCR environment ready:', torch.__version__)"
if ($LASTEXITCODE -ne 0) { throw 'OCR dependency validation failed.' }
Write-Host 'Start gui_main.py and select Ebook > OCR. Models download on first recognition.'

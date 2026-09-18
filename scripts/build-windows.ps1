param([string]$Python = 'python')
$ErrorActionPreference='Stop'
Set-Location (Split-Path -Parent $PSScriptRoot)
& $Python -m pip install -r requirements.txt
if ($LASTEXITCODE -ne 0) { throw '安装构建依赖失败' }
$env:PYTHONPATH=Join-Path (Get-Location) 'src'
& $Python -m unittest discover -s tests -v
if ($LASTEXITCODE -ne 0) { throw '测试失败' }
& $Python scripts/build-portable.py
if ($LASTEXITCODE -ne 0) { throw '生成发布包失败' }

param([Parameter(Mandatory=$true)][string]$Plan)
$ErrorActionPreference = 'Stop'
$p = Get-Content -LiteralPath $Plan -Raw -Encoding UTF8 | ConvertFrom-Json
$log = Join-Path $p.data 'updates\update.log'
$stamp = [guid]::NewGuid().ToString("N")
$old = "$($p.app).previous-$stamp"
$next = "$($p.app).next-$stamp"
$db = Join-Path $p.data 'testagent.sqlite3'
$backup = Join-Path $p.data "updates\database-before-update-$stamp.sqlite3"
$oldMoved = $false
$launched = $null
function Move-WithRetry($source, $destination) {
    for ($attempt = 0; $attempt -lt 6; $attempt++) {
        try { Move-Item -LiteralPath $source -Destination $destination; return }
        catch { if ($attempt -eq 5) { throw }; Start-Sleep -Milliseconds 500 }
    }
}
function Start-Testagent {
    $exe = Join-Path $p.app 'PangeaTestagent.exe'
    if (Test-Path -LiteralPath $exe) {
        return Start-Process -FilePath $exe -ArgumentList "--serve --port $($p.port)" -PassThru
    }
    $runtime = Join-Path $p.app 'runtime\pythonw.exe'
    $entry = Join-Path $p.app 'entry.py'
    return Start-Process -FilePath $runtime -ArgumentList "`"$entry`" --serve --port $($p.port)" -PassThru
}
try {
    Wait-Process -Id $p.pid -Timeout 90 -ErrorAction SilentlyContinue
    if (Get-Process -Id $p.pid -ErrorAction SilentlyContinue) { throw '原进程未退出' }
    if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force }
    if (Test-Path -LiteralPath $next) { Remove-Item -LiteralPath $next -Recurse -Force }
    Copy-Item -LiteralPath (Join-Path $p.stage 'app') -Destination $next -Recurse
    if (Test-Path -LiteralPath $db) {
        Copy-Item -LiteralPath $db -Destination $backup -Force
        foreach ($suffix in @('-wal', '-shm')) {
            if (Test-Path -LiteralPath "$db$suffix") { Copy-Item -LiteralPath "$db$suffix" -Destination "$backup$suffix" -Force }
        }
    }
    Move-WithRetry $p.app $old
    $oldMoved = $true
    Move-WithRetry $next $p.app
    $env:TESTAGENT_DATA_DIR = $p.data
    $launched = Start-Testagent
    $ready = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep -Seconds 1
        try {
            $health = Invoke-RestMethod -Uri "http://127.0.0.1:$($p.port)/api/health" -TimeoutSec 1
            if ($health.version -eq $p.version) { $ready = $true; break }
        } catch { }
        if ($launched.HasExited) { break }
    }
    if (-not $ready) { throw '新版本未通过启动检查' }
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format o) update succeeded: $($p.version); previous app: $old"
    Start-Process "http://127.0.0.1:$($p.port)"
} catch {
    Add-Content -LiteralPath $log -Value "$(Get-Date -Format o) update failed: $_"
    if ($launched -and -not $launched.HasExited) { Stop-Process -Id $launched.Id -Force }
    if ($oldMoved -and (Test-Path -LiteralPath $old)) {
        if (Test-Path -LiteralPath $p.app) { Remove-Item -LiteralPath $p.app -Recurse -Force }
        Move-WithRetry $old $p.app
        if (Test-Path -LiteralPath $backup) {
            Copy-Item -LiteralPath $backup -Destination $db -Force
            Remove-Item -LiteralPath "$db-wal","$db-shm" -Force -ErrorAction SilentlyContinue
            foreach ($suffix in @('-wal', '-shm')) {
                if (Test-Path -LiteralPath "$backup$suffix") { Copy-Item -LiteralPath "$backup$suffix" -Destination "$db$suffix" -Force }
            }
        }
        $env:TESTAGENT_DATA_DIR = $p.data
        Start-Testagent | Out-Null
    }
}

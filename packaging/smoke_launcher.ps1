# Starts the packaged launcher, checks the no-dongle message, then does a
# Demo-mode Start -> tracking -> panel -> Stop, the same as a user would.
$ErrorActionPreference = "Stop"
$env:RTLNC_CONFIG_DIR = Join-Path $env:RUNNER_TEMP "rtlnc-cfg"
$B = "http://127.0.0.1:8765"
$p = Start-Process dist\rtlnc\rtlnc.exe -ArgumentList "--browser --port 8765" -PassThru
try {
  for ($i = 0; $i -lt 30; $i++) {
    try { $null = Invoke-RestMethod "$B/api/state"; break } catch { Start-Sleep 1 }
  }
  $page = Invoke-WebRequest "$B/" -UseBasicParsing
  if ($page.Content -notmatch "Receive") { throw "launcher page missing" }

  $r = Invoke-WebRequest -Method Post "$B/api/start" -Body "{}" -ContentType "application/json" -SkipHttpErrorCheck
  if ($r.StatusCode -ne 400 -or $r.Content -notmatch "Demo mode") { throw "expected the no-dongles message, got $($r.StatusCode) $($r.Content)" }
  Write-Host "no-dongle message OK: $($r.Content)"

  $null = Invoke-RestMethod -Method Post "$B/api/settings" -Body '{"demo": true, "local_audio": false, "freq_mhz": 146.52, "mode": "fm"}' -ContentType "application/json"
  $null = Invoke-RestMethod -Method Post "$B/api/start" -Body "{}" -ContentType "application/json"
  $ok = $false
  for ($i = 0; $i -lt 45; $i++) {
    Start-Sleep 1
    $s = Invoke-RestMethod "$B/api/state"
    if ($s.status.state -eq "track" -and $null -ne $s.status.cancel_db) { $ok = $true; break }
  }
  $s.logs | Select-Object -Last 25 | ForEach-Object { Write-Host $_ }
  if (-not $ok) { throw "launcher demo did not reach tracking" }
  Write-Host "tracking, suppression $($s.status.cancel_db) dB, demod $($s.status.demod), fast lock $($s.status.fast_on)"

  $panel = Invoke-WebRequest "http://127.0.0.1:8073/" -UseBasicParsing
  if ($panel.Content -notmatch "PHASE") { throw "control panel not served" }

  $null = Invoke-RestMethod -Method Post "$B/api/stop" -Body "{}" -ContentType "application/json"
  Start-Sleep 3
  $s = Invoke-RestMethod "$B/api/state"
  if ($s.running) { throw "still running after Stop" }
  $left = Get-Process rtlnc -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $p.Id }
  if ($left) { throw "child processes left after Stop: $($left.Id -join ', ')" }
  Write-Host "launcher smoke test passed"
}
finally {
  Stop-Process -Id $p.Id -Force -ErrorAction SilentlyContinue
}

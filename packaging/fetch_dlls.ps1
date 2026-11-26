# Downloads the official RTL-SDR Blog driver release (V4 + V4L support) and copies the
# 64-bit DLLs into packaging\vendor\rtlsdr so they get bundled into rtlnc.exe.
$ErrorActionPreference = "Stop"
# V1.4.0 is the first release with V4L (Lite) support; it still supports the V4 and V3.
$tag  = if ($env:RTLSDR_BLOG_TAG) { $env:RTLSDR_BLOG_TAG } else { "V1.4.0" }
$url  = "https://github.com/rtlsdrblog/rtl-sdr-blog/releases/download/$tag/Release.zip"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$dest = Join-Path $here "vendor\rtlsdr"
$tmp  = Join-Path $env:TEMP "rtlsdrblog-$tag"
New-Item -ItemType Directory -Force -Path $dest, $tmp | Out-Null
Write-Host "Downloading $url"
Invoke-WebRequest -Uri $url -OutFile "$tmp\Release.zip" -UseBasicParsing
Expand-Archive -Force "$tmp\Release.zip" "$tmp\x"
$x64 = Get-ChildItem -Recurse -Directory "$tmp\x" | Where-Object { $_.Name -eq "x64" } | Select-Object -First 1
if (-not $x64) { throw "x64 folder not found in Release.zip" }
Copy-Item "$($x64.FullName)\*.dll" $dest -Force
if (-not (Test-Path "$dest\rtlsdr.dll")) { throw "rtlsdr.dll not found in the x64 folder" }
# some loaders look for librtlsdr.dll
Copy-Item "$dest\rtlsdr.dll" "$dest\librtlsdr.dll" -Force
Get-ChildItem $dest | ForEach-Object { Write-Host "  bundled: $($_.Name)" }

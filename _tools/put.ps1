param([string]$Path, [string]$InFile)
$bytes = [System.IO.File]::ReadAllBytes($InFile)
$fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Create, [System.IO.FileAccess]::Write, [System.IO.FileShare]::None)
try { $fs.Write($bytes, 0, $bytes.Length) } finally { $fs.Dispose() }
Remove-Item $InFile -Force -ErrorAction SilentlyContinue
Write-Output ("wrote {0} ({1} bytes)" -f $Path, $bytes.Length)
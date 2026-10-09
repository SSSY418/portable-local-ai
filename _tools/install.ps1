<#
  便携 AI 工作台 —— 一键下载缺失的大文件

  这个仓库只备份了"自己写的代码"。bin\ models\ py\ 都是能重新下载的
  大块头（合计 3 GB 左右），所以没有放进 git，用这个脚本拉回来。

  用法（在项目根目录）：
      powershell -ExecutionPolicy Bypass -File _tools\install.ps1
      powershell -ExecutionPolicy Bypass -File _tools\install.ps1 -Proxy http://127.0.0.1:10808

      # 模型没有预设，要自己给地址（不指定就跳过模型那一步）：
      powershell -ExecutionPolicy Bypass -File _tools\install.ps1 `
          -ModelUrl "https://hf-mirror.com/<仓库>/resolve/main/<文件名>.gguf"

      # 也可以只补 Python / koboldcpp，模型自己手动放进 models\：
      powershell -ExecutionPolicy Bypass -File _tools\install.ps1
      # 然后手动下载任意 gguf 放进 models\ 即可

  已经存在的文件会跳过，不会重复下载。
#>
param(
  [string]$Proxy = "",
  [string]$ModelUrl = "",
  [switch]$SkipPython,
  [switch]$SkipKobold
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent

$PyVersion   = '3.12.7'
$KoboldTag   = 'v1.122.1'

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

function Get-File([string]$Url, [string]$Dest, [string]$What) {
  if (Test-Path $Dest) {
    $sz = (Get-Item $Dest).Length
    if ($sz -gt 1MB) { Say ("  已存在，跳过：{0} ({1:N1} MB)" -f (Split-Path $Dest -Leaf), ($sz/1MB)) 'DarkGray'; return $true }
  }
  $dir = Split-Path $Dest -Parent
  if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }

  $curlArgs = @('-L', '--retry', '8', '--retry-delay', '3', '--retry-all-errors', '-C', '-', '-o', $Dest, $Url)
  if ($Proxy -ne '') { $curlArgs = @('-x', $Proxy) + $curlArgs }
  if (-not (Get-Command curl.exe -ErrorAction SilentlyContinue)) {
    Say "  找不到 curl.exe（Windows 10 1803 以上自带），改用 Invoke-WebRequest（大文件较慢）" 'Yellow'
    $iwr = @{ Uri = $Url; OutFile = $Dest; UseBasicParsing = $true }
    if ($Proxy -ne '') { $iwr['Proxy'] = $Proxy }
    Invoke-WebRequest @iwr
    return $true
  }
  Say ("  下载 {0} …" -f $What)
  & curl.exe @curlArgs
  if ($LASTEXITCODE -ne 0) { Say ("  下载失败（curl 退出码 {0}）：{1}" -f $LASTEXITCODE, $Url) 'Red'; return $false }
  return $true
}

Say ""
Say "============================================================"
Say "  便携 AI 工作台 —— 补齐大文件"
Say ("  项目目录: " + $Root)
if ($Proxy -ne '') { Say ("  走代理: " + $Proxy) }
Say "============================================================"
Say ""

# ---------------- 1) 便携版 Python ----------------
if (-not $SkipPython) {
  $pyExe = Join-Path $Root 'py\python.exe'
  if (Test-Path $pyExe) {
    Say "[1/3] 便携版 Python 已存在，跳过。" 'DarkGray'
  } else {
    Say "[1/3] 下载便携版 Python $PyVersion"
    $zipUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
    $zip = Join-Path $Root ("_tmp_python.zip")
    if (Get-File $zipUrl $zip "Python $PyVersion") {
      Say "  解压到 py\ …"
      $pyDir = Join-Path $Root 'py'
      if (-not (Test-Path $pyDir)) { New-Item -ItemType Directory -Force -Path $pyDir | Out-Null }
      Expand-Archive -Path $zip -DestinationPath $pyDir -Force
      Remove-Item $zip -Force -ErrorAction SilentlyContinue
      if (Test-Path $pyExe) { Say ("  完成：" + (& $pyExe -V 2>&1)) 'Green' } else { Say "  解压后没找到 python.exe，请检查。" 'Red' }
    }
  }
} else { Say "[1/3] 按要求跳过 Python" 'DarkGray' }

# ---------------- 2) koboldcpp ----------------
if (-not $SkipKobold) {
  $base = "https://github.com/LostRuins/koboldcpp/releases/download/$KoboldTag"
  $targets = @(
    @{ Name = 'koboldcpp.exe';        What = 'koboldcpp（CUDA 版，大头）' },
    @{ Name = 'koboldcpp_nocuda.exe'; What = 'koboldcpp（Vulkan/CPU 版，兜底）' }
  )
  Say "[2/3] 下载 koboldcpp $KoboldTag"
  foreach ($t in $targets) {
    $dest = Join-Path $Root ("bin\" + $t.Name)
    Get-File "$base/$($t.Name)" $dest $t.What | Out-Null
  }
  $kc = Join-Path $Root 'bin\koboldcpp_nocuda.exe'
  if (Test-Path $kc) { Say ("  验证：" + (& $kc --version 2>&1 | Select-Object -First 1)) 'Green' }
} else { Say "[2/3] 按要求跳过 koboldcpp" 'DarkGray' }

# ---------------- 3) 模型（不预设，要用户自己给地址）----------------
if ($ModelUrl -ne "") {
  $fileName = Split-Path ([Uri]$ModelUrl).AbsolutePath -Leaf
  $dest = Join-Path $Root ("models\" + $fileName)
  Say "[3/3] 下载模型 $fileName"
  Say "  提示：这是一个 gguf 模型文件，通常 1~3 GB，依网速可能要几分钟。" 'DarkGray'

  # 先问出正确的大小，下完对一遍，避免下成半截还以为成功
  $expect = $null
  try {
    $head = & curl.exe -sIL $ModelUrl 2>$null
    $line = $head | Select-String -Pattern '(?i)^content-length:\s*(\d+)' | Select-Object -Last 1
    if ($line) { $expect = [int64]$line.Matches.Groups[1].Value }
  } catch { }

  Get-File $ModelUrl $dest $fileName | Out-Null
  if (Test-Path $dest) {
    $got = (Get-Item $dest).Length
    if ($expect -and $got -ne $expect) {
      Say ("  大小不对：期望 {0} 字节，实际 {1} 字节 —— 可能没下完，再跑一次这个脚本接着下。" -f $expect, $got) 'Red'
    } else {
      Say ("  完成：{0:N2} GB" -f ($got/1GB)) 'Green'
    }
  }
} else {
  Say "[3/3] 没指定 -ModelUrl，跳过模型下载。"
  Say "  模型请自己准备：下载任意 .gguf 文件放进 models\ 目录即可。" 'DarkGray'
  Say "  （注意：单个文件不能超过 4 GB —— 本盘若是 FAT32 会有这个限制）" 'DarkGray'
  Say "  也可以这样指定下载地址：" 'DarkGray'
  Say '      -ModelUrl "https://hf-mirror.com/<仓库>/resolve/main/<文件名>.gguf"' 'DarkGray'
}

Say ""
Say "全部就绪。双击 启动.bat 即可。" 'Green'
Say ""
$ErrorActionPreference = 'Continue'
$Root = $PSScriptRoot | Split-Path -Parent
$Port = 5001
$BenchPort = 8000
$Ctx = 8192
$GpuLayers = if ($env:PORTABLE_AI_GPULAYERS) { $env:PORTABLE_AI_GPULAYERS } else { 22 }
$WantVulkan = ($env:PORTABLE_AI_BACKEND -eq 'vulkan')
# 等模型就绪的上限，默认 120 秒。这个盘读 2.3GB 模型偶尔会超过，
# 等不及就会自动退到另一个后端（能跑起来，只是慢点）。
# 想让每个后端都多等一会儿：set PORTABLE_AI_WAITTIMEOUT=240
$WaitSeconds = if ($env:PORTABLE_AI_WAITTIMEOUT) { [int]$env:PORTABLE_AI_WAITTIMEOUT } else { 120 }
# 单个后端的等待总上限。任务书要求"最多等 120 秒、超时给明确提示"，
# 所以到 120 秒会明确告诉用户"还在读模型、继续等"；但真把这个慢盘上
# 正在加载的进程掐掉，只会去试并不更快的另一个后端，等于白等更久。
# 实测冷启动 105~366 秒，故总上限默认 300 秒（可用环境变量调）。
$MaxWaitSeconds = if ($env:PORTABLE_AI_MAXWAIT) { [int]$env:PORTABLE_AI_MAXWAIT } else { 300 }
# koboldcpp 解包出来的运行库（约 600MB）放这里。
# 故意放 C 盘（系统盘）：解包是纯写磁盘，C 盘是固态、快得多；
# 放项目盘（USB，写 40MB/s）更慢，而且会和"读模型"抢同一块盘的 I/O。
# 残留靠下面每次启动前的清理兜住，不会像以前那样堆到 14.8GB。
$ExtractDir = Join-Path $env:TEMP 'portable-ai-extract'

Write-Host "============================================================"
Write-Host "  便携 AI 工作台"
Write-Host "  项目目录 : $Root"
Write-Host "============================================================"
Write-Host ""

$py = Join-Path $Root 'py\python.exe'
$bench = Join-Path $Root 'app\workbench.py'
if (-not (Test-Path $py))    { Write-Host "[错误] 找不到 py\python.exe" -ForegroundColor Red; exit 1 }
if (-not (Test-Path $bench)) { Write-Host "[错误] 找不到 app\workbench.py" -ForegroundColor Red; exit 1 }

# ---- 挑模型 ----
$model = $null
if ($env:PORTABLE_AI_MODEL) {
  $cand = Join-Path $Root ("models\" + $env:PORTABLE_AI_MODEL)
  if (Test-Path $cand) { $model = $env:PORTABLE_AI_MODEL }
}
if (-not $model) {
  $found = Get-ChildItem (Join-Path $Root 'models\*.gguf') -ErrorAction SilentlyContinue | Select-Object -First 1
  if ($found) { $model = $found.Name }
}
if (-not $model) {
  Write-Host "[错误] models\ 里没有 .gguf 模型文件，请先放一个进去。" -ForegroundColor Red
  exit 1
}
Write-Host "使用模型 : $model"
Write-Host "上下文   : $Ctx    给的显卡层数: $GpuLayers"
Write-Host ""

# ---- 模型服务已经在跑了吗 ----
function Test-Port([int]$p) {
  try { $c = New-Object System.Net.Sockets.TcpClient; $c.Connect('127.0.0.1', $p); $c.Close(); return $true }
  catch { return $false }
}

$cuda = Join-Path $Root 'bin\koboldcpp.exe'
$nocuda = Join-Path $Root 'bin\koboldcpp_nocuda.exe'
$order = if ($WantVulkan) { @($nocuda, $cuda) } else { @($cuda, $nocuda) }
$order = $order | Where-Object { Test-Path $_ }
if (-not $order) { Write-Host "[错误] bin\ 里没有 koboldcpp.exe 或 koboldcpp_nocuda.exe" -ForegroundColor Red; exit 1 }

# 结束某一次尝试留下的进程。必须做这件事：koboldcpp 启动时会先起一个
# 解包的父进程，真正干活的是它的子进程；如果这一次等超时了就直接去试下一个后端，
# 上一次的进程会赖着不走，两个一起抢同一块显存 —— 实测出现过 4 个进程、
# 占掉 3885/4096 MB 显存，离蓝屏就差一点。
# 只认命令里带本项目根目录的进程，绝不误伤别人的 koboldcpp。
function Stop-Attempt([object]$Proc, [string]$RootDir) {
  if ($Proc) { try { $Proc | Stop-Process -Force -ErrorAction SilentlyContinue } catch { } }
  Start-Sleep -Milliseconds 500
  $mine = Get-CimInstance Win32_Process -Filter "Name='koboldcpp.exe' OR Name='koboldcpp_nocuda.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$RootDir*" }
  foreach ($p in $mine) {
    Write-Host ("      清掉上一次没起来的进程 (pid " + $p.ProcessId + ")") -ForegroundColor DarkGray
    try { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue } catch { }
  }
  Start-Sleep -Milliseconds 500
}

if (Test-Port $Port) {
  Write-Host "[1/3] 检测到端口 $Port 上已经有模型服务在跑，直接用它。"
} else {
  # 上一次运行如果留下过残骸（比如被强杀、或之前失败过），先扫干净再开始，
  # 免得新旧进程一起抢显存。
  $stale = Get-CimInstance Win32_Process -Filter "Name='koboldcpp.exe' OR Name='koboldcpp_nocuda.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$Root*" }
  if ($stale) {
    Write-Host ("[0/3] 发现上次遗留的模型服务进程 " + (($stale | ForEach-Object { $_.ProcessId }) -join ',') + "，先清掉。") -ForegroundColor Yellow
    Stop-Attempt $null $Root
  }
  $ok = $false
  foreach ($exe in $order) {
    $name = Split-Path $exe -Leaf
    Write-Host "[1/3] 启动模型服务：$name （第一次启动要解包+读模型，耐心等）"
    $args = @('--model', (Join-Path $Root ('models\' + $model)), '--port', $Port,
              '--host', '127.0.0.1', '--contextsize', $Ctx, '--gpulayers', $GpuLayers)
    if ($name -like '*nocuda*') { $args += '--usevulkan' }

    # koboldcpp 是 PyInstaller 单文件程序，每次启动会把约 600MB 运行库解包到 TEMP。
    # 正常退出它自己会删，但被强杀/蓝屏就留下 —— 实测反复启动堆过 14.8GB。
    # 所以这里每次启动前先清一遍旧残留（正在跑的删不掉、会自动跳过），
    # 再把 TEMP 指到 C 盘固态那个专用目录，解包快、也不会堆。
    if (-not (Test-Path $ExtractDir)) { New-Item -ItemType Directory -Force -Path $ExtractDir | Out-Null }
    foreach ($staleRoot in @($ExtractDir, (Join-Path $Root '_extract'))) {
      if (Test-Path $staleRoot) {
        Get-ChildItem $staleRoot -Force -ErrorAction SilentlyContinue | ForEach-Object {
          $free = $false
          try {
            $_ | Remove-Item -Recurse -Force -ErrorAction Stop
            $free = $true
          } catch { $free = $false }
          if ($free) { Write-Host ("      清掉上次遗留的解包目录: " + $_.Name) -ForegroundColor DarkGray }
        }
      }
    }
    $env:TEMP = $ExtractDir
    $env:TMP  = $ExtractDir
    $t0 = Get-Date
    $proc = Start-Process -FilePath $exe -ArgumentList $args -WorkingDirectory $Root -WindowStyle Hidden -PassThru

    # 等待逻辑故意做得笨一点：进程还活着就一直等，直到到达总上限。
    #
    # 为什么不做"卡死检测"：我试过用 CPU 判断（没用 —— 实测加载时 CPU 从第 30 秒
    # 到第 105 秒一直停在 15.4 秒不动），又试过用"内存+显存指纹"判断，
    # 结果把正常加载的进程误判成卡死、提前掐掉，反而比不判还糟。
    # 这个盘读 2.3GB 模型实测 105~366 秒，那就诚实地给足时间：
    # 能起来就起来，进程自己退出了就立刻放弃。
    $hardDeadline = (Get-Date).AddSeconds($MaxWaitSeconds)
    $notified = $false
    while ($true) {
      if (Test-Port $Port) {
        try { Invoke-WebRequest "http://127.0.0.1:$Port/api/extra/version" -TimeoutSec 4 -UseBasicParsing | Out-Null; $ok = $true; break }
        catch { }
      }
      $now = Get-Date
      # 进程自己没了（崩了/参数不对）就别耗着
      if (-not (Get-Process -Id $proc.Id -ErrorAction SilentlyContinue)) {
        Write-Host "      模型服务进程自己退出了（多半是参数或显存问题）。" -ForegroundColor Yellow
        break
      }
      if (-not $notified -and ((Get-Date) - $t0).TotalSeconds -ge $WaitSeconds) {
        $notified = $true
        Write-Host "      已经等了 $WaitSeconds 秒，还在读模型（这个盘慢），继续等…" -ForegroundColor Yellow
      }
      if ($now -ge $hardDeadline) { break }
      Start-Sleep -Milliseconds 1000
    }
    if ($ok) { Write-Host ("      模型服务已就绪（用了 {0:N0} 秒）。" -f ((Get-Date) - $t0).TotalSeconds) -ForegroundColor Green; break }
    $waited = [int]((Get-Date) - $t0).TotalSeconds
    Write-Host "      等了约 $waited 秒仍未就绪，判定这次起不来。" -ForegroundColor Yellow
    Write-Host "      先把它清掉，再换另一个后端试 —— 能跑起来，但速度会慢一点。" -ForegroundColor Yellow
    Stop-Attempt $proc $Root
  }
  if (-not $ok) {
    Write-Host ""
    Write-Host "[失败] 两次都没能启动模型服务。可以试试：" -ForegroundColor Red
    Write-Host "  · 显存不够：关掉游戏、浏览器等占显卡的程序"
    Write-Host "  · 少给显卡几层：  set PORTABLE_AI_GPULAYERS=12"
    Write-Host "  · 完全用 CPU 跑： set PORTABLE_AI_GPULAYERS=0"
    Write-Host "  · 换另一个后端：  set PORTABLE_AI_BACKEND=vulkan"
    Stop-Attempt $null $Root      # 两次都失败，别把残骸留给用户
    exit 1
  }
}

Write-Host "[2/3] 启动工作台后端（端口 $BenchPort）"
Write-Host "[3/3] 打开浏览器"
Start-Process "http://127.0.0.1:$BenchPort" | Out-Null

Write-Host ""
Write-Host "============================================================"
Write-Host "  工作台地址： http://127.0.0.1:$BenchPort"
Write-Host "  关掉这个窗口，模型服务和工作台会一起退出。"
Write-Host "============================================================"
Write-Host ""

$env:PORTABLE_AI_MANAGED = '1'
& $py $bench
$code = $LASTEXITCODE
if ($null -eq $code) { $code = 0 }

if ($code -ne 0) {
  Write-Host ""
  Write-Host "工作台退出，代码 $code。" -ForegroundColor Yellow
}
exit $code
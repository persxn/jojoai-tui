<#
  install.ps1 - set up everything needed to run `jojo` on Windows.

      irm https://ai.jojocode.in/install.ps1 | iex

  Turnkey: installs Python if missing (winget), installs the TUI into a private
  venv (pulls in windows-curses), puts a `jojo` shim on your PATH, installs
  Ollama (winget), and pulls a model that fits this machine. No admin rights;
  winget handles the package installs.

  Env overrides:
    $env:JOJO_TUI_SOURCE     pip target (default: the endpoint wheel, then git)
    $env:JOJO_ENDPOINT       helper-script host (default https://ai.jojocode.in)
    $env:JOJO_NO_OLLAMA=1    install the TUI only; skip Ollama + model
    $env:JOJO_MODEL=<tag>    pull this model instead of the auto-recommended one
#>
$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 still negotiates TLS 1.0 by default on some builds,
# and every https fetch below then fails with a bare "could not create SSL/TLS
# secure channel". Harmless on PowerShell 7+, where this is already the case.
try {
  [Net.ServicePointManager]::SecurityProtocol =
    [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch { }

$Endpoint = if ($env:JOJO_ENDPOINT) { $env:JOJO_ENDPOINT } else { 'https://ai.jojocode.in' }
$Prefix   = Join-Path $env:LOCALAPPDATA 'jojocode-ai'
$Venv     = Join-Path $Prefix 'venv'
$BinDir   = Join-Path $Prefix 'bin'
$RepoPip  = 'jojocode-ai-tui @ git+https://github.com/persxn/jojoai-tui.git#subdirectory=tui'

function Step($m) { Write-Host "`n> $m" -ForegroundColor Green }
function Warn($m) { Write-Host "! $m" -ForegroundColor Yellow }

# `throw`, not `Write-Error` + `exit`.
#
# Two reasons, both of which bit this script. With $ErrorActionPreference =
# 'Stop', Write-Error *throws*, so the `exit 1` after it never ran and the user
# saw a raw exception instead of the sentence. And `exit` inside `irm | iex`
# exits the whole PowerShell session - closing the terminal of somebody who
# only mistyped a model name.
function Die($m) { Write-Host "install: $m" -ForegroundColor Red; throw $m }

function Have($name) { [bool](Get-Command $name -ErrorAction SilentlyContinue) }

<#
  Run a native program and hand back its stdout and exit code - never a throw.

  Windows PowerShell 5.1 is what `irm | iex` runs in on a stock machine, and
  with $ErrorActionPreference = 'Stop' it turns *any* line a native program
  writes to stderr into a terminating NativeCommandError - even one redirected
  with 2>$null. pip writes its "a new release of pip is available" notice to
  stderr, so every pip call here used to throw: the wheel install failed inside
  its try, the git fallback threw a raw exception, and the installer died with
  a stack trace on a machine where nothing was actually wrong. PowerShell 7
  does not do this, which is why it never reproduced there.

  So the preference is relaxed for the duration of the call only, stderr is
  discarded, and the caller decides what the exit code means.
#>
function Invoke-Native([string]$Exe, [string[]]$Arguments) {
  $saved = $ErrorActionPreference
  $ErrorActionPreference = 'Continue'
  # PowerShell 7.3+ can also be told to throw on a non-zero exit code. Scoped
  # to this function, so the caller's setting is untouched.
  $PSNativeCommandUseErrorActionPreference = $false
  try {
    $out = & $Exe @Arguments 2>$null
    return @{ Out = $out; Code = $LASTEXITCODE }
  } catch {
    return @{ Out = $null; Code = -1 }
  } finally {
    $ErrorActionPreference = $saved
  }
}

# Re-read PATH from the registry into this process. winget does not refresh the
# PATH of the shell that invoked it, so anything it just installed is invisible
# until we do this ourselves.
function Sync-Path {
  $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
  $u = [Environment]::GetEnvironmentVariable('Path', 'User')
  $env:Path = (@($m, $u) | Where-Object { $_ } ) -join ';'
}

# ---------------------------------------------------------------------------
# Python
# ---------------------------------------------------------------------------

<#
  Returns @{ Exe = 'py'; Pre = @('-3') } or $null.

  The command and its leading arguments are kept apart deliberately. The old
  version stored "py -3" as one string and split it, which handed $null as an
  argument for the single-word candidates - `& python3 $null -c ...` - so
  `python3` and `python` never matched and a machine with either but not the
  `py` launcher was told to install Python it already had.
#>
function Find-Python {
  foreach ($c in @(
      @{ Exe = 'py';      Pre = @('-3') },
      @{ Exe = 'python3'; Pre = @() },
      @{ Exe = 'python';  Pre = @() })) {
    if (-not (Have $c.Exe)) { continue }
    # Not `$args` - that is PowerShell's own automatic variable.
    $probe = @($c.Pre) + @('-c', 'import sys;print("%d.%d"%sys.version_info[:2])')
    # The Microsoft Store stub `python.exe` exists on a bare machine, prints an
    # advert to stderr and exits 9009; the exit code is what rules it out.
    $r = Invoke-Native $c.Exe $probe
    try {
      $v = [string]($r.Out | Select-Object -Last 1)
      if ($r.Code -eq 0 -and $v -and [version]$v.Trim() -ge [version]'3.9') { return $c }
    } catch { }
  }
  foreach ($p in @(
      "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe",
      "$env:LOCALAPPDATA\Programs\Python\Python310\python.exe",
      "$env:ProgramFiles\Python313\python.exe",
      "$env:ProgramFiles\Python312\python.exe",
      "$env:ProgramFiles\Python311\python.exe")) {
    if (Test-Path $p) { return @{ Exe = $p; Pre = @() } }
  }
  return $null
}

Step 'Python'
$py = Find-Python
if (-not $py) {
  if (-not (Have winget)) {
    Die "need Python >= 3.9 and winget is not available.`n  Install Python from https://www.python.org/downloads/ and re-run."
  }
  Write-Host '  not found - installing via winget'
  winget install --id Python.Python.3.12 -e --silent --accept-source-agreements --accept-package-agreements --scope user
  Sync-Path
  $py = Find-Python
}
if (-not $py) { Die 'Python installed but could not be located - open a new terminal and re-run.' }
Write-Host "  $($py.Exe) $($py.Pre -join ' ')  ($((Invoke-Native $py.Exe @($py.Pre + @('--version'))).Out))"

# ---------------------------------------------------------------------------
# venv + the TUI
# ---------------------------------------------------------------------------

Step 'The TUI'
New-Item -ItemType Directory -Force -Path $Prefix | Out-Null
$vpy = Join-Path $Venv 'Scripts\python.exe'
if (-not (Test-Path $vpy)) {
  $made = Invoke-Native $py.Exe @($py.Pre + @('-m', 'venv', $Venv))
  if ($made.Code -ne 0 -or -not (Test-Path $vpy)) { Die "could not create a virtualenv at $Venv" }
}

# A venv can exist without pip in it (a Python built without ensurepip), and
# every install below would then fail with a message about the TUI rather than
# about pip. The Linux installer learnt this on python3-minimal; ask directly.
if ((Invoke-Native $vpy @('-m', 'pip', '--version')).Code -ne 0) {
  [void](Invoke-Native $vpy @('-m', 'ensurepip', '--upgrade'))
  if ((Invoke-Native $vpy @('-m', 'pip', '--version')).Code -ne 0) {
    Die "the virtualenv at $Venv has no pip. Reinstall Python from python.org (tick 'pip'), delete $Venv, and re-run."
  }
}
[void](Invoke-Native $vpy @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '--upgrade', 'pip'))

function Pip($spec) {
  return ((Invoke-Native $vpy @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', $spec)).Code -eq 0)
}

$installed = $false
if ($env:JOJO_TUI_SOURCE) {
  if (-not (Pip $env:JOJO_TUI_SOURCE)) { Die "could not install '$env:JOJO_TUI_SOURCE'" }
  $installed = $true
} else {
  # 1) the prebuilt wheel from the endpoint - needs only pip; no git, no
  #    compiler, no Visual Studio build tools on a bare machine.
  try {
    $wheel = (Invoke-RestMethod -UseBasicParsing "$Endpoint/dl/tui-wheel").ToString().Trim()
    if ($wheel -like '*.whl' -and (Pip "$Endpoint/dl/$wheel")) {
      $installed = $true; Write-Host "  from $wheel"
    }
  } catch { }
  # 2) the git repo (needs git - install it if we can)
  if (-not $installed) {
    if (-not (Have git) -and (Have winget)) {
      winget install --id Git.Git -e --silent --accept-source-agreements --accept-package-agreements
      Sync-Path
    }
    if ((Have git) -and (Pip $RepoPip)) { $installed = $true; Write-Host '  from the git repo' }
  }
}
if (-not $installed) { Die "could not install the TUI.`n  Tried: $Endpoint/dl/, and the git repo.`n  Check your connection, or set `$env:JOJO_TUI_SOURCE to a wheel path." }

$jojoExe = Join-Path $Venv 'Scripts\jojo.exe'
if (-not (Test-Path $jojoExe)) { Die "the TUI installed but $jojoExe is missing" }
Write-Host "  $((Invoke-Native $jojoExe @('--version')).Out)"

# ---------------------------------------------------------------------------
# The shim, and PATH
# ---------------------------------------------------------------------------

Step 'Launcher'
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# `%~dp0` - the shim's own directory - rather than the absolute path to the
# venv. It makes the shim independent of where LOCALAPPDATA is, and it means
# the file contains no non-ASCII bytes even when the user's profile name does
# (a `.cmd` is read in the console's OEM code page, and an accented path
# written as UTF-8 becomes mojibake and the shim stops resolving).
$shim = "@echo off`r`n`"%~dp0..\venv\Scripts\jojo.exe`" %*`r`n"
[System.IO.File]::WriteAllText((Join-Path $BinDir 'jojo.cmd'), $shim, [System.Text.ASCIIEncoding]::new())
Write-Host "  $BinDir\jojo.cmd"

<#
  Add $BinDir to the *user* PATH, without destroying it.

  [Environment]::GetEnvironmentVariable('Path','User') returns the value with
  %VARIABLES% already expanded. Writing that back - which is what
  SetEnvironmentVariable does - replaces every reference with whatever it
  happened to expand to at install time, permanently, for the whole account.
  A user whose PATH said %JAVA_HOME%\bin stops tracking their JDK. So the raw
  value is read straight out of the registry with expansion suppressed, and
  written back with the same value kind.
#>
function Add-ToUserPath([string]$Dir) {
  $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey('Environment', $true)
  if (-not $key) { $key = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey('Environment') }
  try {
    $raw = $key.GetValue('Path', '', [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    if ($null -eq $raw) { $raw = '' }
    $raw = [string]$raw

    # Exact segment comparison. A substring test would consider "...\bin" to be
    # already present because "...\bin-old" is.
    $parts = @($raw -split ';' | Where-Object { $_ -ne '' })
    if ($parts -contains $Dir) { return $false }

    # A trailing or doubled ';' leaves an empty PATH entry, which Windows reads
    # as "the current directory" - a real hazard, and what a naive
    # "$raw;$Dir" produced on a profile that had never had a user PATH.
    $next = (@($parts) + @($Dir)) -join ';'

    # Keep ExpandString if it was one (or if the value uses %VARS%), so the
    # references above stay references.
    $kind = [Microsoft.Win32.RegistryValueKind]::String
    try { if ($raw) { $kind = $key.GetValueKind('Path') } } catch { }
    if ($next -match '%') { $kind = [Microsoft.Win32.RegistryValueKind]::ExpandString }

    $key.SetValue('Path', $next, $kind)
    return $true
  } finally { $key.Dispose() }
}

# Tell the rest of Windows the environment changed, so a newly opened Explorer
# or terminal sees it without a sign-out.
function Send-EnvChange {
  try {
    if (-not ('JojoWin32' -as [type])) {
      Add-Type -Namespace '' -Name 'JojoWin32' -MemberDefinition @'
[DllImport("user32.dll", SetLastError = true, CharSet = CharSet.Auto)]
public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam,
    string lParam, uint fuFlags, uint uTimeout, out UIntPtr lpdwResult);
'@
    }
    $HWND_BROADCAST = [IntPtr]0xffff; $WM_SETTINGCHANGE = 0x1A; $SMTO_ABORTIFHUNG = 0x2
    [UIntPtr]$out = [UIntPtr]::Zero
    [void][JojoWin32]::SendMessageTimeout($HWND_BROADCAST, $WM_SETTINGCHANGE,
      [UIntPtr]::Zero, 'Environment', $SMTO_ABORTIFHUNG, 5000, [ref]$out)
  } catch { }
}

if (Add-ToUserPath $BinDir) {
  Write-Host "  added to your user PATH"
  Send-EnvChange
} else {
  Write-Host "  already on your user PATH"
}

# Always fix up *this* process, not only when the registry changed. Re-running
# in a terminal opened before the first install must still leave `jojo` working
# in that terminal - previously the process PATH was only touched inside the
# "if we just added it" branch, so the second run left you exactly where you
# started.
if (($env:Path -split ';') -notcontains $BinDir) { $env:Path = "$env:Path;$BinDir" }

# ---------------------------------------------------------------------------
# Ollama + a model
# ---------------------------------------------------------------------------

if ($env:JOJO_NO_OLLAMA -eq '1') {
  Step 'Skipping Ollama (JOJO_NO_OLLAMA=1)'
} else {
  Step 'Ollama'
  if (Have ollama) {
    Write-Host '  already installed'
  } elseif (Have winget) {
    winget install --id Ollama.Ollama -e --silent --accept-source-agreements --accept-package-agreements
    Sync-Path
  } else {
    Warn 'winget not found - install Ollama from https://ollama.com/download/windows'
  }
  if (Have ollama) {
    if (-not (Get-Process ollama -ErrorAction SilentlyContinue)) {
      Start-Process -WindowStyle Hidden ollama -ArgumentList 'serve'
      Start-Sleep -Seconds 3
    }
    Step 'Model'
    try {
      $rec = Join-Path $env:TEMP 'jojo-recommend-model.py'
      Invoke-WebRequest -UseBasicParsing "$Endpoint/recommend-model.py" -OutFile $rec
      # Not through Invoke-Native: the pull's progress bar belongs on screen.
      # The preference is relaxed so its stderr cannot abort the install.
      $saved = $ErrorActionPreference; $ErrorActionPreference = 'Continue'
      try {
        if ($env:JOJO_MODEL) { & $vpy $rec --model $env:JOJO_MODEL --pull --yes }
        else { & $vpy $rec --pull --yes }
      } finally { $ErrorActionPreference = $saved }
      if ($LASTEXITCODE -ne 0) { throw "recommend-model exited $LASTEXITCODE" }
    } catch {
      Warn "model pull failed - pick one later:  ollama pull gpt-oss:20b"
    }
  }
}

Write-Host @"

[ready]  Start it in any project directory:

    jojo                                            # this terminal works now
    jojo --model gpt-oss:20b                        # a specific model
    jojo --remote $Endpoint --login    # a hosted server

  Full path, always works:  $BinDir\jojo.cmd
  Other terminals: PATH is set for new ones; already-open ones need a restart.

  Update:  re-run this installer - it always fetches the current build
             irm $Endpoint/install.ps1 | iex
  Remove:  Remove-Item -Recurse -Force "$Prefix"   (and drop $BinDir from PATH)
"@ -ForegroundColor Green

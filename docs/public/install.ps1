#Requires -Version 5.1

# Install LKMBot with uv on Windows PowerShell.
[CmdletBinding()]
param(
    # Invoke-Expression validates omitted string parameters as empty values.
    [ValidateSet('', 'zh-CN', 'en-US')]
    [string]$Language,
    [ValidateSet('', 'china', 'official', 'custom')]
    [string]$DownloadSource,
    [string]$UvDownloadBaseUrl,
    [string]$PypiIndexUrl,
    [string]$PythonInstallMirror,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

# Windows PowerShell 5.1 may otherwise negotiate an obsolete TLS version.
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

$UseColor = [string]::IsNullOrEmpty($env:NO_COLOR) -and [Console]::IsOutputRedirected -eq $false
$CanPrompt = -not $NonInteractive -and [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
$Indent = '  '

# Keep the first-run experience predictable across machines. Users can still
# choose Chinese or English explicitly in the language menu.
# Use JSON Unicode escapes so Windows PowerShell 5.1 can read this file
# with any system encoding, while downloaded UTF-8 scripts also work without a BOM.
$bootstrapText = ConvertFrom-Json @'
{
    "InstallerTitle": "\ud83c\udf0c LKMBot Installer",
    "LanguagePrompt": "Select language / \u8bf7\u9009\u62e9\u8bed\u8a00",
    "Chinese": "\u7b80\u4f53\u4e2d\u6587",
    "English": "English",
    "NextStepsEnglish": "Next, run these commands:",
    "NextStepsChinese": "\u63a5\u4e0b\u6765\u53ef\u4ee5\u4f9d\u6b21\u6267\u884c\u4ee5\u4e0b\u6307\u4ee4\u6765\u521d\u59cb\u5316\u548c\u542f\u52a8 LKMBot\uff1a",
    "PythonFallbackEnglish": "Python mirror failed; retrying with the official source...",
    "PythonFallbackChinese": "Python \u955c\u50cf\u4e0b\u8f7d\u5931\u8d25\uff0c\u6b63\u5728\u5207\u6362\u5230\u5b98\u65b9\u6e90\u91cd\u8bd5\u2026"
}
'@

$defaultLanguage = 'en-US'

if (-not $Language) {
    $Language = $defaultLanguage
    if ($CanPrompt) {
        $languages = @('en-US', 'zh-CN')
        $defaultChoice = [array]::IndexOf($languages, $Language) + 1
        Write-Host "`n$Indent$($bootstrapText.InstallerTitle)" -ForegroundColor Cyan
        Write-Host "$Indent-----------------------" -ForegroundColor DarkGray
        Write-Host "`n$Indent$($bootstrapText.LanguagePrompt)"
        Write-Host "$Indent  1. $($bootstrapText.English)"
        Write-Host "$Indent  2. $($bootstrapText.Chinese)"
        do {
            $choice = Read-Host "${Indent}1-2 [$defaultChoice]"
        } until ($choice -eq '' -or $choice -in @('1', '2'))
        if ($choice) { $Language = $languages[[int]$choice - 1] }
    }
}

$messages = ConvertFrom-Json @'
{
    "zh-CN": {
        "ChooseSource": "\u8bf7\u9009\u62e9\u4e0b\u8f7d\u6e90",
        "China": "\u4e2d\u56fd\u5927\u9646\u52a0\u901f\u4e0b\u8f7d\u955c\u50cf\u6e90",
        "Official": "\u5b98\u65b9\u6e90",
        "Custom": "\u81ea\u5b9a\u4e49",
        "Choice": "\u8f93\u5165\u7f16\u53f7\uff0c\u6309\u56de\u8f66\u4f7f\u7528\u9ed8\u8ba4\u9009\u9879",
        "InvalidChoice": "\u8bf7\u8f93\u5165 1\u30012 \u6216 3\u3002",
        "UvUrl": "uv ZIP \u4e0b\u8f7d\u76ee\u5f55 URL\uff08\u4e0d\u542b\u6587\u4ef6\u540d\uff09",
        "PypiUrl": "PyPI simple \u7d22\u5f15 URL",
        "CustomRequired": "\u81ea\u5b9a\u4e49\u6e90\u9700\u8981\u540c\u65f6\u63d0\u4f9b UvDownloadBaseUrl \u548c PypiIndexUrl\u3002",
        "InvalidUrl": "\u4e0b\u8f7d\u6e90\u5fc5\u987b\u662f\u6709\u6548\u7684 HTTPS URL\uff1a{0}",
        "WindowsOnly": "\u8bf7\u5728 Windows \u4e0a\u8fd0\u884c\u6b64 PowerShell \u811a\u672c\uff1bLinux/macOS \u8bf7\u4f7f\u7528 install.sh\u3002",
        "Source": "\u4e0b\u8f7d\u6e90\uff1a{0}",
        "UvMirror": "uv \u4e0b\u8f7d\u76ee\u5f55\uff1a{0}",
        "Pypi": "PyPI \u7d22\u5f15\uff1a{0}",
        "PythonMirror": "Python \u4e0b\u8f7d\u955c\u50cf\uff1a{0}",
        "PythonDefault": "Python \u4e0b\u8f7d\u6cbf\u7528 uv \u7684\u914d\u7f6e\uff1b\u5982\u9700\u52a0\u901f\uff0c\u53ef\u901a\u8fc7 -PythonInstallMirror \u6307\u5b9a\u955c\u50cf\uff08PyPI \u955c\u50cf\u4e0d\u5305\u542b Python \u89e3\u91ca\u5668\uff09\u3002",
        "InstallingUv": "\u672a\u627e\u5230 uv\uff0c\u6b63\u5728\u5b89\u88c5\u2026",
        "TryingMirror": "\u6b63\u5728\u5c1d\u8bd5 uv \u955c\u50cf\uff1a{0}",
        "MirrorFailed": "uv \u955c\u50cf\u4e0b\u8f7d\u5931\u8d25\uff1a{0}",
        "OfficialFallback": "\u6b63\u5728\u5c1d\u8bd5 uv \u5b98\u65b9\u5b89\u88c5\u5668\u2026",
        "UnsupportedArch": "\u4e0d\u652f\u6301\u7684 Windows \u67b6\u7784\uff1a{0}",
        "InvalidChecksum": "uv \u538b\u7f29\u5305 SHA256 \u6821\u9a8c\u5931\u8d25\u3002",
        "MissingBinaries": "uv \u538b\u7f29\u5305\u4e2d\u7f3a\u5c11 uv.exe \u6216 uvx.exe\u3002",
        "UvFailed": "uv \u5b89\u88c5\u5931\u8d25\u3002\u8bf7\u53c2\u8003 https://docs.astral.sh/uv/getting-started/installation/",
        "InstallingLKMBot": "\u6b63\u5728\u4f7f\u7528 Python 3.12 \u5b89\u88c5 LKMBot\u2026",
        "BotFailed": "LKMBot \u5b89\u88c5\u5931\u8d25\uff08\u9000\u51fa\u7801 {0}\uff09\u3002\u8bf7\u68c0\u67e5\u4e0a\u65b9\u7684\u4e0b\u8f7d\u6216\u4f9d\u8d56\u9519\u8bef\u3002",
        "Installed": "LKMBot \u5b89\u88c5\u5b8c\u6210\u3002",
        "ReloadShell": "\u8bf7\u6839\u636e\u4f60\u4f7f\u7528\u7684\u7ec8\u7aef\u91cd\u65b0\u52a0\u8f7d\u73af\u5883\uff1a",
        "RunCommands": "\u91cd\u65b0\u52a0\u8f7d\u540e\uff0c\u8bf7\u624b\u52a8\u8fd0\u884c\uff1a"
    },
    "en-US": {
        "ChooseSource": "Select a download source",
        "China": "Mainland China accelerated download mirrors",
        "Official": "Official sources",
        "Custom": "Custom",
        "Choice": "Enter a number, or press Enter to use the default",
        "InvalidChoice": "Please enter 1, 2, or 3.",
        "UvUrl": "uv ZIP download directory URL (without the filename)",
        "PypiUrl": "PyPI simple index URL",
        "CustomRequired": "Custom sources require both UvDownloadBaseUrl and PypiIndexUrl.",
        "InvalidUrl": "Download sources must be valid HTTPS URLs: {0}",
        "WindowsOnly": "Run this PowerShell script on Windows; use install.sh on Linux/macOS.",
        "Source": "Download source: {0}",
        "UvMirror": "uv download directory: {0}",
        "Pypi": "PyPI index: {0}",
        "PythonMirror": "Python download mirror: {0}",
        "PythonDefault": "Python downloads use the existing uv configuration. Set -PythonInstallMirror to use a mirror (PyPI mirrors do not include Python interpreters).",
        "InstallingUv": "uv was not found. Installing uv...",
        "TryingMirror": "Trying uv mirror: {0}",
        "MirrorFailed": "uv mirror download failed: {0}",
        "OfficialFallback": "Trying the official uv installer...",
        "UnsupportedArch": "Unsupported Windows architecture: {0}",
        "InvalidChecksum": "uv archive SHA256 verification failed.",
        "MissingBinaries": "The uv archive is missing uv.exe or uvx.exe.",
        "UvFailed": "Failed to install uv. See https://docs.astral.sh/uv/getting-started/installation/",
        "InstallingLKMBot": "Installing LKMBot with Python 3.12...",
        "BotFailed": "LKMBot installation failed (exit code {0}). Check the download or dependency errors above.",
        "Installed": "LKMBot has been installed.",
        "ReloadShell": "Reload the environment for your shell:",
        "RunCommands": "Then run these commands manually:"
    }
}
'@
$Text = $messages.$Language

if (-not $DownloadSource) {
    $DownloadSource = if ($Language -eq 'zh-CN') { 'china' } else { 'official' }
    if ($CanPrompt) {
        $defaultChoice = if ($Language -eq 'zh-CN') { '1' } else { '2' }
        Write-Host "`n$Indent$($Text.ChooseSource)"
        Write-Host "$Indent  1. $($Text.China)"
        Write-Host "$Indent  2. $($Text.Official)"
        Write-Host "$Indent  3. $($Text.Custom)"
        do {
            $choice = Read-Host "$Indent$($Text.Choice) [$defaultChoice]"
            if ($choice -eq '') { $choice = $defaultChoice }
            if ($choice -notin @('1', '2', '3')) { Write-Host "$Indent$($Text.InvalidChoice)" }
        } until ($choice -in @('1', '2'))
        $DownloadSource = @('china', 'official', 'custom')[[int]$choice - 1]
    }
}

$uvMirrorUrls = @()
switch ($DownloadSource) {
    'china' {
        # Use uv mirrors listed by MirrorZ; the release-directory layout is not GitHub's layout.
        $uvMirrorUrls = @(
            'https://mirrors.ustc.edu.cn/github-release/astral-sh/uv/LatestRelease'
        )
        $PypiIndexUrl = 'https://mirrors.cernet.edu.cn/pypi/web/simple/'
    }
    'official' { $PypiIndexUrl = 'https://pypi.org/simple/' }
    'custom' {
        if ($CanPrompt) {
            if (-not $UvDownloadBaseUrl) { $UvDownloadBaseUrl = Read-Host "$Indent$($Text.UvUrl)" }
            if (-not $PypiIndexUrl) { $PypiIndexUrl = Read-Host "$Indent$($Text.PypiUrl)" }
        }
        if (-not $UvDownloadBaseUrl -or -not $PypiIndexUrl) {
            throw $Text.CustomRequired
        }
        $uvMirrorUrls = @($UvDownloadBaseUrl.TrimEnd('/'))
    }
}

if (-not $PythonInstallMirror) {
    if ($env:UV_PYTHON_INSTALL_MIRROR) {
        $PythonInstallMirror = $env:UV_PYTHON_INSTALL_MIRROR
    } elseif ($DownloadSource -eq 'china') {
        $PythonInstallMirror = 'https://mirrors.ustc.edu.cn/github-release/astral-sh/python-build-standalone/'
    }
}

foreach ($url in @($uvMirrorUrls) + @($PypiIndexUrl, $PythonInstallMirror)) {
    if (-not $url) { continue }
    $parsedUrl = $null
    if (-not [Uri]::TryCreate($url, [UriKind]::Absolute, [ref]$parsedUrl) -or
        $parsedUrl.Scheme -ne 'https' -or -not $parsedUrl.Host -or $parsedUrl.UserInfo -or
        $parsedUrl.Query -or $parsedUrl.Fragment) {
        throw ($Text.InvalidUrl -f $url)
    }
}

if ($env:OS -ne 'Windows_NT') { throw $Text.WindowsOnly }

function Write-Status {
    param(
        [string]$Prefix,
        [string]$Message,
        [string]$Color
    )

    if ($UseColor) {
        Write-Host "$Indent$Prefix    $Message" -ForegroundColor $Color
    } else {
        Write-Host "$Indent$Prefix    $Message"
    }
}

function Info { Write-Status "[INFO]" "$args" "Cyan" }
function Warn { Write-Status "[WARN]" "$args" "Yellow" }
function Ok { Write-Status "[OK]" "$args" "Green" }
function Err { Write-Status "[ERROR]" "$args" "Red" }

function Test-Command {
    param([string]$Name)
    $null = Get-Command $Name -ErrorAction SilentlyContinue
    return $?
}

function Test-ZipArchive {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path) -or (Get-Item -LiteralPath $Path).Length -lt 4) {
        return $false
    }

    $stream = [System.IO.File]::OpenRead($Path)
    try {
        return $stream.ReadByte() -eq 0x50 -and
            $stream.ReadByte() -eq 0x4B -and
            $stream.ReadByte() -eq 0x03 -and
            $stream.ReadByte() -eq 0x04
    } finally {
        $stream.Dispose()
    }
}

function Invoke-UvToolInstall {
    param([string[]]$ArgumentList)

    $uvCommand = Get-Command 'uv' -ErrorAction Stop
    if ($uvCommand.CommandType -ne 'Application') {
        # This path also makes the installer straightforward to test without
        # downloading packages or changing the developer machine.
        & $uvCommand @ArgumentList
        return $LASTEXITCODE
    }

    # PowerShell may otherwise give console programs their own window in some
    # hosts. Explicitly keep uv and all of its installation output in this one.
    # Do not inherit the current directory: it may have been removed while an
    # existing PowerShell session was still located there.
    $workingDirectory = [System.IO.Path]::GetTempPath()
    $process = Start-Process -FilePath $uvCommand.Source -ArgumentList $ArgumentList `
        -WorkingDirectory $workingDirectory -NoNewWindow -Wait -PassThru
    return $process.ExitCode
}

function Update-UvPath {
    $candidateDirs = @()

    if ($HOME) {
        $candidateDirs += Join-Path $HOME ".local\bin"
    }
    if ($env:USERPROFILE) {
        $candidateDirs += Join-Path $env:USERPROFILE ".local\bin"
    }
    if ($env:LOCALAPPDATA) {
        $candidateDirs += Join-Path $env:LOCALAPPDATA "uv"
    }
    foreach ($dir in @($env:UV_INSTALL_DIR, $env:UV_UNMANAGED_INSTALL, $env:XDG_BIN_HOME)) {
        if ($dir) { $candidateDirs += $dir }
    }
    if ($env:XDG_DATA_HOME) {
        $candidateDirs += Join-Path $env:XDG_DATA_HOME '..\bin'
    }

    foreach ($dir in $candidateDirs) {
        if ((Test-Path $dir) -and (($env:PATH -split ';') -notcontains $dir)) {
            $env:PATH = "$dir;$env:PATH"
        }
    }
}

function Install-Uv {
    Info $Text.InstallingUv

    $tempDir = Join-Path ([System.IO.Path]::GetTempPath()) ([guid]::NewGuid().ToString())
    $null = New-Item -ItemType Directory -Path $tempDir
    try {
        # PROCESSOR_ARCHITEW6432 identifies the native OS when PowerShell is emulated.
        $nativeArch = if ($env:PROCESSOR_ARCHITEW6432) { $env:PROCESSOR_ARCHITEW6432 } else { $env:PROCESSOR_ARCHITECTURE }
        $arch = switch ($nativeArch) {
            'AMD64' { 'x86_64' }
            'x86' { 'i686' }
            'ARM64' { 'aarch64' }
            default { throw ($Text.UnsupportedArch -f $_) }
        }
        $artifact = "uv-$arch-pc-windows-msvc.zip"
        $officialBaseUrl = 'https://github.com/astral-sh/uv/releases/latest/download'
        foreach ($baseUrl in @($uvMirrorUrls) + $officialBaseUrl) {
            try {
                if ($baseUrl -eq $officialBaseUrl) {
                    Info "$($Text.Official): $officialBaseUrl"
                } else {
                    Info ($Text.TryingMirror -f $baseUrl)
                }
                $archive = Join-Path $tempDir $artifact
                $checksumFile = Join-Path $tempDir "$artifact.sha256"
                Invoke-WebRequest -Uri "$baseUrl/$artifact" -OutFile $archive -TimeoutSec 90 -UseBasicParsing

                # USTC protects larger release assets with a lightweight
                # JavaScript cookie check. Reproduce that single documented
                # browser step when its verification page is returned.
                if (-not (Test-ZipArchive $archive) -and
                    ([Uri]$baseUrl).Host -eq 'mirrors.ustc.edu.cn') {
                    $challengePage = [System.IO.File]::ReadAllText($archive)
                    $addressMatch = [regex]::Match(
                        $challengePage,
                        'document\.cookie\s*=\s*"addr=([0-9A-Fa-f:.]+);'
                    )
                    if ($addressMatch.Success) {
                        # Windows PowerShell 5.1 silently drops a manually set
                        # Cookie header. A WebRequestSession works in both 5.1
                        # and modern PowerShell.
                        $webSession = New-Object Microsoft.PowerShell.Commands.WebRequestSession
                        $cookie = New-Object System.Net.Cookie(
                            'addr',
                            $addressMatch.Groups[1].Value,
                            '/',
                            'mirrors.ustc.edu.cn'
                        )
                        $webSession.Cookies.Add($cookie)
                        Invoke-WebRequest -Uri "$baseUrl/$artifact" -OutFile $archive `
                            -WebSession $webSession `
                            -TimeoutSec 90 -UseBasicParsing
                    }
                }

                if (-not (Test-ZipArchive $archive)) {
                    throw $Text.InvalidChecksum
                }
                Invoke-WebRequest -Uri "$baseUrl/$artifact.sha256" -OutFile $checksumFile -TimeoutSec 30 -UseBasicParsing
                $expectedHash = ((Get-Content $checksumFile -Raw).Trim() -split '\s+')[0]
                if ($expectedHash -notmatch '^[a-fA-F0-9]{64}$' -or
                    (Get-FileHash $archive -Algorithm SHA256).Hash -ne $expectedHash) {
                    throw $Text.InvalidChecksum
                }
                $unpacked = Join-Path $tempDir 'unpacked'
                Expand-Archive -Path $archive -DestinationPath $unpacked -Force
                if (-not (Test-Path (Join-Path $unpacked 'uv.exe')) -or
                    -not (Test-Path (Join-Path $unpacked 'uvx.exe'))) {
                    throw $Text.MissingBinaries
                }
            } catch {
                Warn ($Text.MirrorFailed -f $_.Exception.Message)
                Remove-Item (Join-Path $tempDir 'unpacked') -Recurse -Force -ErrorAction SilentlyContinue
                continue
            }

            # Respect uv's installation directory settings for every source.
            $installDir = if ($env:UV_INSTALL_DIR) { $env:UV_INSTALL_DIR }
                elseif ($env:UV_UNMANAGED_INSTALL) { $env:UV_UNMANAGED_INSTALL }
                elseif ($env:XDG_BIN_HOME) { $env:XDG_BIN_HOME }
                elseif ($env:XDG_DATA_HOME) { Join-Path $env:XDG_DATA_HOME '..\bin' }
                else { Join-Path $env:USERPROFILE '.local\bin' }
            $null = New-Item -ItemType Directory -Path $installDir -Force
            Copy-Item (Join-Path $unpacked '*.exe') -Destination $installDir -Force
            $userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
            if (-not $env:UV_UNMANAGED_INSTALL -and -not $env:INSTALLER_NO_MODIFY_PATH -and
                ($userPath -split ';') -notcontains $installDir) {
                [Environment]::SetEnvironmentVariable('Path', "$installDir;$userPath", 'User')
            }
            Update-UvPath
            return
        }
        throw $Text.UvFailed
    } catch {
        Err $_.Exception.Message
        Err $Text.UvFailed
        exit 1
    } finally {
        Remove-Item $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Info ($Text.Source -f $(switch ($DownloadSource) {
    'china' { $Text.China }
    'official' { $Text.Official }
    'custom' { $Text.Custom }
}))
foreach ($url in $uvMirrorUrls) { Info ($Text.UvMirror -f $url) }
Info ($Text.Pypi -f $PypiIndexUrl)

# Recover an existing installation before downloading uv again.
Update-UvPath
if (-not (Test-Command "uv")) {
    Install-Uv
}

Update-UvPath

if (-not (Test-Command "uv")) {
    Err $Text.UvFailed
    exit 1
}

Ok (& uv --version)
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

if ($PythonInstallMirror) {
    Info ($Text.PythonMirror -f $PythonInstallMirror)
} else {
    Info $Text.PythonDefault
}

$previousPythonMirror = $env:UV_PYTHON_INSTALL_MIRROR
try {
    if ($PythonInstallMirror) { $env:UV_PYTHON_INSTALL_MIRROR = $PythonInstallMirror }
    Info $Text.InstallingLKMBot
    $toolInstallExitCode = Invoke-UvToolInstall -ArgumentList @(
        'tool', 'install', '--python', '3.12', 'git+https://github.com/Alma1314/LKM-bot.git', '--default-index', $PypiIndexUrl
    )
    if ($toolInstallExitCode -ne 0 -and $PythonInstallMirror) {
        $pythonFallback = switch ($Language) {
            'zh-CN' { $bootstrapText.PythonFallbackChinese }
            default { $bootstrapText.PythonFallbackEnglish }
        }
        Warn $pythonFallback
        $env:UV_PYTHON_INSTALL_MIRROR = $null
        $toolInstallExitCode = Invoke-UvToolInstall -ArgumentList @(
            'tool', 'install', '--python', '3.12', 'git+https://github.com/Alma1314/LKM-bot.git', '--default-index', $PypiIndexUrl
        )
    }
    if ($toolInstallExitCode -ne 0) {
        Err ($Text.BotFailed -f $toolInstallExitCode)
        exit $toolInstallExitCode
    }
    Ok $Text.Installed
    try { & uv tool update-shell *> $null } catch { }
    Write-Host "`n$Indent$($Text.ReloadShell)"
    Write-Host "$($Indent)  Bash: source ~/.bashrc"
    Write-Host "$($Indent)  Zsh: source ~/.zshrc"
    Write-Host "$($Indent)  Fish: source ~/.config/fish/config.fish"
    Write-Host "$($Indent)  PowerShell: . `$PROFILE"
    Write-Host "`n$Indent$($Text.RunCommands)"
    Write-Host "$($Indent)$($Indent)lkmbot init" -ForegroundColor Cyan
    Write-Host "$($Indent)$($Indent)lkmbot run" -ForegroundColor Cyan
} finally {
    $env:UV_PYTHON_INSTALL_MIRROR = $previousPythonMirror
}

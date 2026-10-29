[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [ValidateSet("Install", "Uninstall", "Plan")]
    [string]$Action = "Install",
    [string]$PackageSource = "stagemesh @ git+https://github.com/saketvishal/stagemesh.git@b6023f6733bb090ff75a65a47808660afb0a1bbd",
    [ValidatePattern("^[0-9a-fA-F]{64}$")]
    [string]$WheelSha256 = "",
    [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "StageMesh"),
    [string]$ProgramsRoot = (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
    [string]$Project = (Get-Location).Path,
    [string]$PythonExecutable = "",
    [switch]$Json
)

$ErrorActionPreference = "Stop"
$OwnerText = "StageMesh user install v1"
$OwnerFileName = ".stagemesh-user-install"
$ManifestName = "install.json"
$ShortcutName = "StageMesh.lnk"

function Get-FullSafePath([string]$Value, [string]$Label) {
    if ([string]::IsNullOrWhiteSpace($Value)) { throw "$Label must not be empty" }
    $full = [System.IO.Path]::GetFullPath($Value)
    $root = [System.IO.Path]::GetPathRoot($full)
    if ($full -eq $root -or (Split-Path -Leaf $full).Length -lt 3) { throw "$Label is too broad: $full" }
    return $full.TrimEnd([System.IO.Path]::DirectorySeparatorChar, [System.IO.Path]::AltDirectorySeparatorChar)
}

function Read-InstallManifest([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    try { return Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json }
    catch { return $null }
}

function Test-ShortcutMatches([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkingDirectory) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $link = $shell.CreateShortcut($Path)
        $sameTarget = [string]::Equals(
            [System.IO.Path]::GetFullPath($link.TargetPath), [System.IO.Path]::GetFullPath($Target),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        $sameWorking = [string]::Equals(
            [System.IO.Path]::GetFullPath($link.WorkingDirectory), [System.IO.Path]::GetFullPath($WorkingDirectory),
            [System.StringComparison]::OrdinalIgnoreCase
        )
        return $sameTarget -and $sameWorking -and $link.Arguments -eq $Arguments
    } catch { return $false }
}

function Assert-PinnedSource([string]$Source, [string]$Hash) {
    $gitMatch = [regex]::Match($Source, "^stagemesh\s*@\s*git\+(https://\S+)@([0-9a-fA-F]{40})(?:#\S+)?$")
    if ($gitMatch.Success) {
        $gitUri = [Uri]$gitMatch.Groups[1].Value
        if ($gitUri.UserInfo) { throw "PackageSource must not contain credentials" }
        return "git"
    }
    $wheelMatch = [regex]::Match($Source, "^stagemesh\s*@\s*((?:https|file)://\S+\.whl(?:\?\S+)?)$")
    if ($wheelMatch.Success -and $Hash) {
        if (([Uri]$wheelMatch.Groups[1].Value).UserInfo) { throw "PackageSource must not contain credentials" }
        return "wheel"
    }
    throw "PackageSource must pin a 40-character Git commit or a wheel with -WheelSha256"
}

function Get-SourceKey([string]$Source, [string]$Hash) {
    $bytes = [Text.Encoding]::UTF8.GetBytes("$Source`n$Hash")
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace("-", "").Substring(0, 16).ToLowerInvariant() }
    finally { $sha.Dispose() }
}

function Write-Result([hashtable]$Result) {
    if ($Json) { $Result | ConvertTo-Json -Depth 4 -Compress }
    else {
        Write-Host ("StageMesh {0}: {1}" -f $Result.action, $Result.status)
        if ($Result.shortcut) { Write-Host ("Shortcut: {0}" -f $Result.shortcut) }
        if ($Result.runtime) { Write-Host ("Runtime: {0}" -f $Result.runtime) }
    }
}

function Resolve-Python([string]$Requested) {
    if ($Requested) { return @((Get-Command $Requested -ErrorAction Stop).Source) }
    $python = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($python) { return @($python.Source) }
    $launcher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($launcher) { return @($launcher.Source, "-3") }
    throw "Python 3 was not found; install Python for the current user and retry"
}

function Invoke-Checked([string[]]$Command, [string[]]$Arguments) {
    if ($Command.Count -gt 1) { & $Command[0] @($Command[1..($Command.Count - 1)]) @Arguments | Out-Null }
    else { & $Command[0] @Arguments | Out-Null }
    if ($LASTEXITCODE -ne 0) { throw "Command failed with exit code $LASTEXITCODE" }
}

$InstallRoot = Get-FullSafePath $InstallRoot "InstallRoot"
$ProgramsRoot = Get-FullSafePath $ProgramsRoot "ProgramsRoot"
$Project = [System.IO.Path]::GetFullPath($Project)
if (-not (Test-Path -LiteralPath $Project -PathType Container)) { throw "Project must be an existing directory: $Project" }
$ownerFile = Join-Path $InstallRoot $OwnerFileName
$manifestFile = Join-Path $InstallRoot $ManifestName
$shortcutFile = Join-Path $ProgramsRoot $ShortcutName

if ($Action -eq "Uninstall") {
    if (-not (Test-Path -LiteralPath $InstallRoot)) {
        Write-Result @{ action = "uninstall"; status = "already absent"; shortcut = $shortcutFile }
        exit 0
    }
    if (-not (Test-Path -LiteralPath $ownerFile) -or (Get-Content -LiteralPath $ownerFile -Raw).Trim() -ne $OwnerText) {
        throw "Refusing to remove an install root without the StageMesh ownership marker: $InstallRoot"
    }
    $shortcutStatus = "absent"
    $installed = Read-InstallManifest $manifestFile
    if ($installed -and $installed.shortcut -and (Test-Path -LiteralPath ([string]$installed.shortcut))) {
        $recordedShortcut = Get-FullSafePath ([string]$installed.shortcut) "Recorded shortcut"
        if ((Split-Path -Leaf $recordedShortcut) -ne $ShortcutName) {
            $shortcutStatus = "preserved invalid recorded path"
        } elseif (Test-ShortcutMatches $recordedShortcut ([string]$installed.target) ([string]$installed.arguments) ([string]$installed.working_directory)) {
            if ($PSCmdlet.ShouldProcess($recordedShortcut, "Remove owned StageMesh Start Menu shortcut")) {
                Remove-Item -LiteralPath $recordedShortcut -Force
            }
            $shortcutStatus = "removed"
        } else {
            $shortcutStatus = "preserved foreign or changed shortcut"
        }
    }
    if ($PSCmdlet.ShouldProcess($InstallRoot, "Remove owned StageMesh per-user installation")) {
        Remove-Item -LiteralPath $InstallRoot -Recurse -Force
    }
    Write-Result @{ action = "uninstall"; status = "removed"; shortcut = $shortcutFile; shortcut_status = $shortcutStatus }
    exit 0
}

$sourceKind = Assert-PinnedSource $PackageSource $WheelSha256
$sourceKey = Get-SourceKey $PackageSource $WheelSha256
$runtime = Join-Path (Join-Path $InstallRoot "runtimes") $sourceKey
$runtimePython = Join-Path $runtime "Scripts\python.exe"
$runtimeCli = Join-Path $runtime "Scripts\stagemesh.exe"
$shortcutArguments = "--project `"$Project`" ui"
$plan = @{
    action = if ($Action -eq "Plan") { "plan" } else { "install" }
    status = if (Test-Path -LiteralPath $runtimeCli) { "refresh" } else { "new" }
    install_root = $InstallRoot
    runtime = $runtime
    shortcut = $shortcutFile
    package_source = $PackageSource
    project = $Project
    source_key = $sourceKey
    per_user = $true
    requires_admin = $false
}
if ($Action -eq "Plan") { Write-Result $plan; exit 0 }
if (Test-Path -LiteralPath $shortcutFile) {
    $previous = Read-InstallManifest $manifestFile
    $ownedShortcut = $previous -and [string]::Equals(
        [System.IO.Path]::GetFullPath([string]$previous.shortcut), $shortcutFile,
        [System.StringComparison]::OrdinalIgnoreCase
    ) -and (Test-ShortcutMatches $shortcutFile ([string]$previous.target) ([string]$previous.arguments) ([string]$previous.working_directory))
    if (-not $ownedShortcut) { throw "Refusing to overwrite a foreign or changed Start Menu shortcut: $shortcutFile" }
}
if (-not $PSCmdlet.ShouldProcess($InstallRoot, "Install pinned StageMesh runtime for the current user")) {
    $plan.status = "not changed"; Write-Result $plan; exit 0
}

if (Test-Path -LiteralPath $InstallRoot) {
    if (-not (Test-Path -LiteralPath $ownerFile) -or (Get-Content -LiteralPath $ownerFile -Raw).Trim() -ne $OwnerText) {
        throw "InstallRoot exists but is not owned by the StageMesh installer"
    }
} else {
    New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null
    Set-Content -LiteralPath $ownerFile -Value $OwnerText -Encoding UTF8
}

if ((Test-Path -LiteralPath $runtime) -and -not (Test-Path -LiteralPath $runtimePython)) {
    throw "Pinned runtime is incomplete; uninstall the owned StageMesh installation before reinstalling: $runtime"
}

if (-not (Test-Path -LiteralPath $runtimePython)) {
    $python = Resolve-Python $PythonExecutable
    try {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $runtime) | Out-Null
        Invoke-Checked $python @("-m", "venv", $runtime)
        if ($sourceKind -eq "wheel") {
            Invoke-Checked @($runtimePython) @(
                "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-index", "--no-deps",
                "$PackageSource#sha256=$($WheelSha256.ToLowerInvariant())"
            )
        } else {
            Invoke-Checked @($runtimePython) @("-m", "pip", "install", "--disable-pip-version-check", "--no-input", "--no-deps", $PackageSource)
        }
        & $runtimeCli --version | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Installed StageMesh entry point failed validation" }
        & $runtimeCli ui --help | Out-Null
        if ($LASTEXITCODE -ne 0) { throw "Installed StageMesh runtime does not provide the ui command" }
        Set-Content -LiteralPath (Join-Path $runtime $OwnerFileName) -Value $OwnerText -Encoding UTF8
    } finally {
        if ((Test-Path -LiteralPath $runtime) -and -not (Test-Path -LiteralPath (Join-Path $runtime $OwnerFileName))) {
            Remove-Item -LiteralPath $runtime -Recurse -Force
        }
    }
} else {
    if (-not (Test-Path -LiteralPath (Join-Path $runtime $OwnerFileName))) { throw "Existing runtime is missing its ownership marker: $runtime" }
    & $runtimeCli --version | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Existing pinned StageMesh entry point failed validation: $runtimeCli" }
    & $runtimeCli ui --help | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Existing pinned StageMesh runtime does not provide the ui command: $runtimeCli" }
}

New-Item -ItemType Directory -Force -Path $ProgramsRoot | Out-Null
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutFile)
$shortcut.TargetPath = $runtimeCli
$shortcut.Arguments = $shortcutArguments
$shortcut.WorkingDirectory = $Project
$shortcut.Description = "Open the StageMesh operator dashboard"
$shortcut.Save()

$manifest = @{
    schema_version = 1; package_source = $PackageSource; wheel_sha256 = $WheelSha256.ToLowerInvariant()
    source_key = $sourceKey; runtime = $runtime; shortcut = $shortcutFile; project = $Project
    target = $runtimeCli; arguments = $shortcutArguments; working_directory = $Project
    installed_at = [DateTimeOffset]::UtcNow.ToString("o")
}
$temporaryManifest = "$manifestFile.tmp-$PID"
$manifest | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath $temporaryManifest -Encoding UTF8
Move-Item -LiteralPath $temporaryManifest -Destination $manifestFile -Force
$plan.status = "installed"
Write-Result $plan

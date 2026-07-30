<#
.SYNOPSIS
    Phase 4 acceptance preflight. Checks that this machine is ready for a manual
    functional test of offline transcription, and changes nothing.

.DESCRIPTION
    Read-only by design. This script runs the same checks an operator would run by
    hand, in order, and prints one verdict at the end.

    What it deliberately does NOT do, and why:

      * It never provisions a model. Provisioning needs network access and is a
        separate, deliberate command; a preflight that quietly downloaded 2 GB would
        be the opposite of a preflight.
      * It never touches the production data root. That root is guarded explicitly
        below, because "run the acceptance script against production" is a mistake
        somebody will make exactly once.
      * It never deletes, cleans or recreates anything.
      * It never stops Docker, WSL or any of your applications.
      * It never opens the microphone. Device discovery enumerates without opening a
        stream; only an explicit calibration does that, and this script does not.
      * It does not require administrator rights.

.PARAMETER DataDir
    Runtime data root to check. Defaults to the Phase 4 acceptance root.

.PARAMETER SkipAsrSmoke
    Skip the real-model offline smoke test. That step loads a 464 MiB model and takes
    roughly 10-20 seconds; everything else is fast.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\phase4_acceptance_preflight.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\phase4_acceptance_preflight.ps1 -DataDir 'E:\MoM-Acceptance'

.NOTES
    Exit codes:
      0  ready, or ready with warnings that are expected at this phase
      1  an engineering FAIL: something is broken and manual testing would be invalid
      2  refused: the target is the production data root, or the environment is unusable
#>

[CmdletBinding()]
param(
    [string]$DataDir = 'D:\MoM-IGD-Models-Phase4',
    [switch]$SkipAsrSmoke
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Continue'

# The production root. Refused as a target, always. Acceptance testing writes
# transcripts and working copies, and doing that to the production database on the way
# to "just trying it out" is how real meeting data gets mixed with test data.
$ProductionRoot = 'D:\MoM-IGD-Data'

$RepoRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Python = Join-Path $RepoRoot '.venv\Scripts\python.exe'

$script:Results = @()
$script:FailCount = 0
$script:WarnCount = 0

function Add-Result {
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][ValidateSet('PASS', 'WARN', 'FAIL')][string]$Status,
        [Parameter(Mandatory)][string]$Detail
    )
    $script:Results += [pscustomobject]@{ Name = $Name; Status = $Status; Detail = $Detail }
    if ($Status -eq 'FAIL') { $script:FailCount++ }
    if ($Status -eq 'WARN') { $script:WarnCount++ }
    $colour = switch ($Status) { 'PASS' { 'Green' } 'WARN' { 'Yellow' } 'FAIL' { 'Red' } }
    $label = '[{0}]' -f $Status.PadRight(4)
    Write-Host $label -ForegroundColor $colour -NoNewline
    Write-Host (' {0}' -f $Name.PadRight(26)) -NoNewline
    Write-Host $Detail
}

function Invoke-Mom {
    <#
        Runs one `python -m mom_igd ...` command against the chosen data root and
        returns its output plus exit code. stderr is merged deliberately: the CLI writes
        its refusals there, and a preflight that hid them would be useless.
    #>
    param([Parameter(Mandatory)][string[]]$MomArgs)
    $all = @('-m', 'mom_igd') + $MomArgs + @('--data-dir', $DataDir)
    $output = & $Python @all 2>&1 | Out-String
    return [pscustomobject]@{ ExitCode = $LASTEXITCODE; Output = $output }
}

Write-Host ''
Write-Host 'MoM-IGD - Phase 4 acceptance preflight' -ForegroundColor Cyan
Write-Host '======================================'
Write-Host ("Repository : {0}" -f $RepoRoot)
Write-Host ("Data root  : {0}" -f $DataDir)
Write-Host 'Read-only  : this script changes nothing, downloads nothing, and opens no microphone.'
Write-Host ''

# ---------------------------------------------------------------------------
# 0. Refuse the production root, before anything else runs
# ---------------------------------------------------------------------------

$normalisedTarget = $DataDir.TrimEnd('\', '/')
$normalisedProduction = $ProductionRoot.TrimEnd('\', '/')
if ($normalisedTarget -ieq $normalisedProduction) {
    Write-Host '[FAIL] production_root_refused' -ForegroundColor Red
    Write-Host ''
    Write-Host ("REFUSED: {0} is the production data root." -f $ProductionRoot) -ForegroundColor Red
    Write-Host 'Acceptance testing writes transcripts, working copies and database rows.'
    Write-Host 'Run it against the acceptance root instead:'
    Write-Host ''
    Write-Host '    powershell -ExecutionPolicy Bypass -File .\scripts\phase4_acceptance_preflight.ps1'
    Write-Host ''
    exit 2
}

# ---------------------------------------------------------------------------
# 1. Environment
# ---------------------------------------------------------------------------

if (-not (Test-Path -LiteralPath $Python)) {
    Add-Result 'virtualenv' 'FAIL' ("no interpreter at {0}. Create it with: py -3.12 -m venv .venv" -f $Python)
    Write-Host ''
    Write-Host 'Cannot continue without the virtual environment.' -ForegroundColor Red
    exit 2
}

$version = (& $Python --version 2>&1 | Out-String).Trim()
if ($version -match 'Python 3\.12\.') {
    Add-Result 'python_version' 'PASS' $version
} else {
    Add-Result 'python_version' 'FAIL' ("expected Python 3.12.x, found '{0}'" -f $version)
}

if (Test-Path -LiteralPath $DataDir) {
    Add-Result 'data_root' 'PASS' 'exists'
} else {
    Add-Result 'data_root' 'FAIL' ("{0} does not exist. Create it with: python -m mom_igd db init --data-dir '{1}'" -f $DataDir, $DataDir)
}

# Free disk on the volume that holds the data root. A working copy is about 115 MB per
# hour of audio, plus the models already on disk.
try {
    $qualifier = (Split-Path -Qualifier (Resolve-Path -LiteralPath $DataDir -ErrorAction Stop))
    $drive = Get-PSDrive -Name $qualifier.TrimEnd(':') -ErrorAction Stop
    $freeGb = [math]::Round($drive.Free / 1GB, 1)
    if ($freeGb -ge 10) {
        Add-Result 'free_disk' 'PASS' ("{0} GB free on {1}" -f $freeGb, $qualifier)
    } elseif ($freeGb -ge 2) {
        Add-Result 'free_disk' 'WARN' ("only {0} GB free on {1}; enough for a short test, not for a long meeting" -f $freeGb, $qualifier)
    } else {
        Add-Result 'free_disk' 'FAIL' ("{0} GB free on {1} is below the 2 GB minimum" -f $freeGb, $qualifier)
    }
} catch {
    Add-Result 'free_disk' 'WARN' ("could not measure free space: {0}" -f $_.Exception.Message)
}

# Available RAM. The measured worst-case worker is 1 910 MiB, and the shell plus the
# API need their own room.
try {
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction Stop
    $freeRamGb = [math]::Round($os.FreePhysicalMemory / 1MB, 1)
    if ($freeRamGb -ge 4) {
        Add-Result 'free_ram' 'PASS' ("{0} GB available" -f $freeRamGb)
    } elseif ($freeRamGb -ge 2.5) {
        Add-Result 'free_ram' 'WARN' ("{0} GB available; the pass-2 model alone peaks near 1.9 GB, so close other applications before a long test" -f $freeRamGb)
    } else {
        Add-Result 'free_ram' 'FAIL' ("{0} GB available is not enough for the pass-2 model (measured peak 1.9 GB)" -f $freeRamGb)
    }
} catch {
    Add-Result 'free_ram' 'WARN' ("could not measure available memory: {0}" -f $_.Exception.Message)
}

# ---------------------------------------------------------------------------
# 2. Database
# ---------------------------------------------------------------------------

$dbVersion = Invoke-Mom @('db', 'version')
if ($dbVersion.ExitCode -eq 0 -and $dbVersion.Output -match 'Up to date\s*:\s*True') {
    $schema = if ($dbVersion.Output -match 'Schema version\s*:\s*(\S+)') { $Matches[1] } else { 'unknown' }
    Add-Result 'db_schema' 'PASS' ("schema {0}, up to date" -f $schema)
} else {
    Add-Result 'db_schema' 'FAIL' ("schema is not at head. Run: python -m mom_igd db init --data-dir '{0}'" -f $DataDir)
}

$dbVerify = Invoke-Mom @('db', 'verify')
if ($dbVerify.ExitCode -eq 0) {
    $chain = if ($dbVerify.Output -match 'Audit chain\s*:\s*(\S+)') { $Matches[1] } else { 'unknown' }
    Add-Result 'db_integrity' 'PASS' ("pragmas verified, audit chain {0}" -f $chain)
} else {
    Add-Result 'db_integrity' 'FAIL' 'db verify failed; see the output above'
}

# ---------------------------------------------------------------------------
# 3. Environment diagnostics
# ---------------------------------------------------------------------------

$doctor = Invoke-Mom @('doctor')
$summary = if ($doctor.Output -match 'Summary:\s*(.+)') { $Matches[1].Trim() } else { 'no summary' }
if ($doctor.ExitCode -eq 0) {
    Add-Result 'doctor' 'PASS' $summary
} else {
    Add-Result 'doctor' 'FAIL' ("{0}. Run `python -m mom_igd doctor --data-dir '{1}'` to see which check failed." -f $summary, $DataDir)
}

# Doctor's own WARN lines are reproduced so the operator can tell an expected
# future-phase warning from something that matters here.
foreach ($line in ($doctor.Output -split "`r?`n")) {
    if ($line -match '^\[WARN\]\s+(\S+)\s+(.*)$') {
        $key = $Matches[1]
        if ($key -in @('usb_conference_microphone', 'consent_text', 'audio_stale_recordings')) {
            $label = 'doctor:' + $key
            if ($label.Length -gt 25) { $label = $label.Substring(0, 25) }
            Add-Result $label 'WARN' ($Matches[2].Substring(0, [math]::Min(120, $Matches[2].Length)))
        }
    }
}

# ---------------------------------------------------------------------------
# 4. Models
# ---------------------------------------------------------------------------

$models = Invoke-Mom @('asr', 'models')
if ($models.Output -match 'Provisioned\s*:\s*none') {
    Add-Result 'asr_models' 'FAIL' ("no model is provisioned. Run once, with network access: python -m mom_igd asr provision all --data-dir '{0}'" -f $DataDir)
} elseif ($models.ExitCode -eq 0) {
    $ready = ([regex]::Matches($models.Output, '(?m)^\s+OK\s+(\S+)')).Count
    Add-Result 'asr_models' 'PASS' ("{0} model(s) provisioned and recorded ready" -f $ready)
} else {
    Add-Result 'asr_models' 'FAIL' 'asr models failed; see the output above'
}

$verify = Invoke-Mom @('asr', 'verify')
if ($verify.ExitCode -eq 0 -and $verify.Output -match 'Every byte was re-hashed') {
    Add-Result 'asr_integrity' 'PASS' 'every model file re-hashed from disk and matches its manifest'
} else {
    Add-Result 'asr_integrity' 'FAIL' 'deep verification failed. Do not test with an unverified model; re-provision it.'
}

# ---------------------------------------------------------------------------
# 5. Audio devices (enumerates only; opens no stream)
# ---------------------------------------------------------------------------

$devices = Invoke-Mom @('audio', 'devices')
if ($devices.ExitCode -eq 0) {
    # Match the TRANSPORT column of a device row, not the word "USB" anywhere in the
    # output: the listing ends with an explanation of why a USB microphone is wanted,
    # and grepping the whole text reported a USB device on a machine that has none.
    $deviceRows = [regex]::Matches($devices.Output, '(?m)^\s*\d+\*?\s+.{2,}?\s+(USB|INTERNAL|BLUETOOTH|UNKNOWN)\s+[0-9a-f]{32}\s*$')
    $transports = @($deviceRows | ForEach-Object { $_.Groups[1].Value })
    if ($transports.Count -eq 0) {
        Add-Result 'audio_devices' 'FAIL' 'no capture device was listed. Recording cannot be tested.'
    } elseif ($transports -contains 'USB') {
        Add-Result 'audio_devices' 'PASS' ("{0} device(s), including a verified USB one" -f $transports.Count)
    } else {
        Add-Result 'audio_devices' 'WARN' ("{0} device(s), transports: {1}. No USB conference microphone: the internal array is acceptable for a FUNCTIONAL test and is not acceptable as accuracy evidence -- its beamforming suppresses speakers who are not facing the laptop." -f $transports.Count, ($transports -join ', '))
    }
} else {
    Add-Result 'audio_devices' 'FAIL' 'no usable capture device. Recording cannot be tested.'
}

# ---------------------------------------------------------------------------
# 6. Real-model offline smoke
# ---------------------------------------------------------------------------

if ($SkipAsrSmoke) {
    Add-Result 'asr_smoke' 'WARN' 'skipped by -SkipAsrSmoke; the model load and offline decode were not exercised'
} else {
    Write-Host '       running the offline ASR smoke (loads a model; ~10-20 s) ...'
    $smoke = Invoke-Mom @('asr', 'smoke')
    if ($smoke.ExitCode -eq 0 -and $smoke.Output -match 'PASS \((\d+)/(\d+) steps\)') {
        Add-Result 'asr_smoke' 'PASS' ("{0}/{1} steps, zero outbound attempts recorded" -f $Matches[1], $Matches[2])
    } else {
        Add-Result 'asr_smoke' 'FAIL' 'the offline smoke did not pass; transcription will not work'
    }
}

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host '--------------------------------------------------------------'
Write-Host ("Summary: {0} PASS, {1} WARN, {2} FAIL" -f
    ($script:Results | Where-Object Status -eq 'PASS').Count,
    $script:WarnCount, $script:FailCount)
Write-Host ''

if ($script:FailCount -gt 0) {
    Write-Host 'NOT READY - fix the FAIL items above before manual testing.' -ForegroundColor Red
    Write-Host 'Testing against a broken environment produces evidence about the environment,'
    Write-Host 'not about the application.'
    Write-Host ''
    exit 1
}

Write-Host 'READY FOR MANUAL FUNCTIONAL TESTING' -ForegroundColor Green
Write-Host ''
Write-Host 'A WARN above is expected at this phase and does not block the functional test.'
Write-Host 'Accuracy has NOT been measured: no reference transcript exists, so nothing here'
Write-Host 'says the transcription is correct - only that the machinery runs.'
Write-Host ''
Write-Host 'Open the application with:' -ForegroundColor Cyan
Write-Host ''
Write-Host ("    .\.venv\Scripts\python.exe -m mom_igd shell --data-dir `"{0}`"" -f $DataDir)
Write-Host ''
Write-Host 'Then follow docs\phase-4-manual-acceptance.md.'
Write-Host ''
exit 0

# Monthly report update task -- run generate_report.py, commit + push if
# the numbers look sane. Meant to be triggered by Windows Task Scheduler.
# Logs to monthly_report_task.log next to this script.
#
# Note: deliberately does NOT set $ErrorActionPreference = "Stop" and does
# NOT redirect native commands' stderr with 2>&1 -- PowerShell 5.1 wraps
# every stderr line from a native exe (git, python) in a NativeCommandError
# under those conditions, so even a successful `git push` (which prints its
# progress to stderr) looks like a crash. Native command failures are
# checked explicitly via $LASTEXITCODE instead.

Set-Location $PSScriptRoot
$logFile = Join-Path $PSScriptRoot "monthly_report_task.log"

function Log($msg) {
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $msg"
    Add-Content -Path $logFile -Value $line -Encoding utf8
    Write-Output $line
}

Log "=== Monthly report task starting ==="

$reportOutput = & python generate_report.py 2>&1
$reportOutput | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) {
    Log "generate_report.py FAILED (exit $LASTEXITCODE) -- aborting, nothing committed."
    exit 1
}

# sanity-check: history.json must exist and its last entry must have numeric fields
$historyPath = Join-Path $PSScriptRoot "docs\history.json"
if (-not (Test-Path $historyPath)) {
    Log "docs\history.json not found after running generate_report.py -- aborting, nothing committed."
    exit 1
}
try {
    $history = Get-Content $historyPath -Raw | ConvertFrom-Json
    $last = $history[$history.Count - 1]
    $fields = @('tiered_ma_pct','ma_filter_pct','grid_pct','emaatr_pct','buy_hold_pct')
    foreach ($f in $fields) {
        $v = $last.$f
        if ($null -eq $v -or [double]::IsNaN([double]$v)) {
            Log "Sanity check failed: $f is missing or NaN in the latest history entry -- aborting, nothing committed."
            exit 1
        }
    }
    Log "Sanity check passed: $($last | ConvertTo-Json -Compress)"
} catch {
    Log "Could not parse docs\history.json ($_) -- aborting, nothing committed."
    exit 1
}

$status = git status --porcelain -- docs/
if (-not $status) {
    Log "No changes in docs/ (numbers identical to last run) -- nothing to commit."
    Log "=== Monthly report task finished (no-op) ==="
    exit 0
}

git add docs/
$dateStr = Get-Date -Format "yyyy-MM-dd"
$commitOutput = & git commit -m "Monthly report update: $dateStr" 2>&1
$commitOutput | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) {
    Log "git commit FAILED (exit $LASTEXITCODE) -- aborting before push."
    exit 1
}

$pushOutput = & git push 2>&1
$pushOutput | ForEach-Object { Log $_ }
if ($LASTEXITCODE -ne 0) {
    Log "git push FAILED (exit $LASTEXITCODE) -- commit was made locally but NOT pushed. Check manually."
    exit 1
}

Log "=== Monthly report task finished (pushed) ==="

[CmdletBinding()]
param()

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$composeProject = "litigation-copilot-smoke-$PID"
$timeoutSentinel = "4321"
$previousApiExtras = [Environment]::GetEnvironmentVariable("LITIGATION_API_EXTRAS", "Process")
$previousNoticeTimeout = [Environment]::GetEnvironmentVariable(
    "LITIGATION_NOTICE_REQUEST_TIMEOUT_SECONDS",
    "Process"
)
$composeWasInvoked = $false
$succeeded = $false

function Invoke-Compose {
    $composeArguments = @("compose", "--project-name", $script:composeProject) + $args
    & docker @composeArguments
    if ($LASTEXITCODE -ne 0) {
        throw "docker $($composeArguments -join ' ') failed with exit code $LASTEXITCODE."
    }
}

function Restore-ProcessEnvironment {
    param(
        [Parameter(Mandatory)]
        [string]$Name,
        [AllowNull()]
        [string]$Value
    )

    if ($null -eq $Value) {
        Remove-Item -LiteralPath "Env:$Name" -ErrorAction SilentlyContinue
    }
    else {
        Set-Item -LiteralPath "Env:$Name" -Value $Value
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker CLI was not found. Install Docker Desktop or another Docker Engine first."
}

& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is required (the 'docker compose' command)."
}

try {
    # Keep the smoke test independent of a developer's potentially much larger
    # local-embedding build configured in .env. The volume behavior is the same.
    $env:LITIGATION_API_EXTRAS = "ocr"
    $env:LITIGATION_NOTICE_REQUEST_TIMEOUT_SECONDS = $timeoutSentinel

    Write-Host "[1/5] Building isolated API and frontend images..."
    $composeWasInvoked = $true
    Invoke-Compose build api frontend

    Write-Host "[2/5] Writing a JUÁ-cache sentinel under the API container's HF_HOME..."
    $seedCache = @'
import os
from pathlib import Path

home = Path(os.environ["HF_HOME"])
home.mkdir(parents=True, exist_ok=True)
(home / ".jua-cache-smoke").write_text(
    "persisted-across-api-image-rebuild",
    encoding="utf-8",
)
print(f"HF_HOME={home}")
'@
    Invoke-Compose run --rm --no-deps api python -c $seedCache

    Write-Host "[3/5] Rebuilding the API image without the Docker layer cache..."
    Invoke-Compose build --no-cache api

    Write-Host "[4/5] Reading the JUÁ-cache sentinel from a container using the rebuilt image..."
    $verifyCache = @'
import os
from pathlib import Path

home = Path(os.environ["HF_HOME"])
assert str(home) == "/app/data/huggingface", home
value = (home / ".jua-cache-smoke").read_text(encoding="utf-8")
assert value == "persisted-across-api-image-rebuild", value
print(f"PASS HF_HOME={home} persisted across the API image rebuild.")
'@
    Invoke-Compose run --rm --no-deps api python -c $verifyCache

    Write-Host "[5/5] Asserting the frontend container receives the notice timeout..."
    $verifyFrontend = @'
import os

name = "LITIGATION_NOTICE_REQUEST_TIMEOUT_SECONDS"
value = os.environ.get(name)
assert value == "4321", f"{name}={value!r}; expected '4321'"
print(f"PASS {name}={value} reached the frontend container.")
'@
    Invoke-Compose run --rm --no-deps frontend python -c $verifyFrontend
    $succeeded = $true
}
finally {
    if ($composeWasInvoked) {
        & docker compose --project-name $composeProject down --volumes --remove-orphans
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "The isolated Compose project could not be completely removed."
        }
    }
    Restore-ProcessEnvironment -Name "LITIGATION_API_EXTRAS" -Value $previousApiExtras
    Restore-ProcessEnvironment `
        -Name "LITIGATION_NOTICE_REQUEST_TIMEOUT_SECONDS" `
        -Value $previousNoticeTimeout
}

if ($succeeded) {
    Write-Host "PASS Docker runtime configuration smoke test completed."
}

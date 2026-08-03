#Requires -Version 5.1
param(
    [string]$HostName = $(if ($env:HOST) { $env:HOST } else { "127.0.0.1" }),
    [int]$Port = $(if ($env:PORT) { [int]$env:PORT } else { 8000 })
)

$ErrorActionPreference = "Stop"
if (Get-Variable PSNativeCommandUseErrorActionPreference -ErrorAction SilentlyContinue) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectRoot = (Resolve-Path -LiteralPath (Join-Path $ScriptDir "..")).Path
$VenvDir = Join-Path $ProjectRoot ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"

function ConvertTo-CmdArgument {
    param(
        [string]$Value
    )

    return '"' + ($Value -replace '"', '\"') + '"'
}

function Invoke-NativeCommand {
    param(
        [string]$FilePath,
        [string[]]$Arguments = @(),
        [switch]$SuppressOutput
    )

    $commandLine = (@($FilePath) + $Arguments | ForEach-Object { ConvertTo-CmdArgument $_ }) -join " "
    if ($SuppressOutput) {
        $commandLine = "$commandLine >NUL 2>NUL"
    }

    & cmd.exe /d /s /c $commandLine
    return $LASTEXITCODE
}

function Test-PythonCommand {
    param(
        [string]$Command,
        [string[]]$Arguments = @()
    )

    $testArguments = $Arguments + @("-c", "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)")
    return (Invoke-NativeCommand -FilePath $Command -Arguments $testArguments -SuppressOutput) -eq 0
}

function New-ProjectVenv {
    if (Get-Command py -ErrorAction SilentlyContinue) {
        if (Test-PythonCommand -Command "py" -Arguments @("-3.11")) {
            $exitCode = Invoke-NativeCommand -FilePath "py" -Arguments @("-3.11", "-m", "venv", $VenvDir)
            if ($exitCode -ne 0) {
                throw "Failed to create virtual environment at $VenvDir."
            }
            return
        }
    }

    if (Get-Command python -ErrorAction SilentlyContinue) {
        if (Test-PythonCommand -Command "python") {
            $exitCode = Invoke-NativeCommand -FilePath "python" -Arguments @("-m", "venv", $VenvDir)
            if ($exitCode -ne 0) {
                throw "Failed to create virtual environment at $VenvDir."
            }
            return
        }
    }

    throw "Python 3.11 or newer is required to create $VenvDir. Install Python 3.11+ or make it available as 'python'."
}

Set-Location -LiteralPath $ProjectRoot

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    New-ProjectVenv
}

if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Virtual environment Python was not found at $Python."
}

$exitCode = Invoke-NativeCommand -FilePath $Python -Arguments @("-c", "import akshare; import fastapi; import pandas; import uvicorn") -SuppressOutput
if ($exitCode -ne 0) {
    $exitCode = Invoke-NativeCommand -FilePath $Python -Arguments @("-m", "pip", "install", "-r", "requirements.txt")
    if ($exitCode -ne 0) {
        throw "Failed to install dependencies from requirements.txt."
    }
}

$exitCode = Invoke-NativeCommand -FilePath $Python -Arguments @("-m", "uvicorn", "app:app", "--host", $HostName, "--port", "$Port")
exit $exitCode

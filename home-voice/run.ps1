#Requires -Version 7.0
[CmdletBinding()]
param(
    [ValidateSet('run', 'collect', 'convert', 'asr', 'prepare', 'translate', 'export', 'status')]
    [string]$Stage = 'run',
    [string[]]$Only = @(),
    [ValidateRange(0, 100000)]
    [int]$Limit = 0,
    [switch]$RedoAsr
)

$ErrorActionPreference = 'Stop'
$configuration = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'config.json') -Raw | ConvertFrom-Json
$python = Join-Path $configuration.moss_repo '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "OpenMOSS Python 不存在：$python"
}
[string[]]$arguments = @('-u', (Join-Path $PSScriptRoot 'pipeline.py'), $Stage)
if ($Only.Count -gt 0) { $arguments += @('--only') + $Only }
if ($Limit -gt 0) { $arguments += @('--limit', $Limit.ToString()) }
if ($RedoAsr) { $arguments += @('--redo-asr') }
$previousUtf8 = [Environment]::GetEnvironmentVariable('PYTHONUTF8', 'Process')
try {
    $env:PYTHONUTF8 = '1'
    & $python @arguments
    if ($LASTEXITCODE -ne 0) { throw "语音流水线失败，退出码：$LASTEXITCODE" }
}
finally {
    [Environment]::SetEnvironmentVariable('PYTHONUTF8', $previousUtf8, 'Process')
}

# Load .env into the current PowerShell session (for coral.exe CLI, etc.)
$envFile = Join-Path $PSScriptRoot ".." ".env" | Resolve-Path -ErrorAction Stop

Get-Content $envFile | ForEach-Object {
    $line = $_.Trim()
    if (-not $line -or $line.StartsWith("#")) { return }
    $eq = $line.IndexOf("=")
    if ($eq -lt 1) { return }
    $name = $line.Substring(0, $eq).Trim()
    $value = $line.Substring($eq + 1).Trim().Trim('"').Trim("'")
    Set-Item -Path "Env:$name" -Value $value
}

Write-Host "Loaded env from .env: GROQ_API_KEY, INTERCOM_ACCESS_TOKEN, STRIPE_SECRET_KEY"

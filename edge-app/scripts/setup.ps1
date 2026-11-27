<#
.SYNOPSIS
  Guided, one-step-at-a-time setup of the beta edge Worker (docs/saas/beta-edge.md).

.DESCRIPTION
  Every step says what it does and shows the exact command, then asks before running it. Answer anything but
  "y" to skip a step. Safe to run again: steps whose result is already in place say so.

  It works only on:
    edge-app\wrangler.toml      (fills in the D1 database id and the VPC service id)
    the SaaS env file           ($env:SAAS_ENV_FILE, default .env.saas at the repo root; never the owner's .env)
    the Cloudflare account      (new D1 database "doomscroller-relay", new VPC service, new Worker "doomscroller-app")

  It never touches edge\ (the live marketing site and waitlist, Pages project "doomscroller-studio") or its
  database "doomscroller-waitlist". Secrets are never printed; they reach wrangler through a temporary JSON file
  that is deleted straight after.

.PARAMETER ShowOnly
  Print every step and command without running anything.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File edge-app\scripts\setup.ps1 -ShowOnly
  powershell -ExecutionPolicy Bypass -File edge-app\scripts\setup.ps1
#>
[CmdletBinding()]
param([switch]$ShowOnly)

$ErrorActionPreference = "Stop"
$EdgeApp = Split-Path -Parent $PSScriptRoot
$Repo = Split-Path -Parent $EdgeApp
$Toml = Join-Path $EdgeApp "wrangler.toml"
$EnvName = if ($env:SAAS_ENV_FILE) { $env:SAAS_ENV_FILE } else { ".env.saas" }
$EnvSaas = if ([IO.Path]::IsPathRooted($EnvName)) { $EnvName } else { Join-Path $Repo $EnvName }
$Zero = "00000000-0000-0000-0000-000000000000"
$UuidRe = "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
$Utf8 = New-Object System.Text.UTF8Encoding $false

if ([IO.Path]::GetFileName($EnvSaas) -eq ".env") { throw "Refusing to use the owner's .env as the SaaS env file." }

# ---- helpers ---------------------------------------------------------------------------------------
function Write-Step([string]$Number, [string]$Title, [string]$What) {
  Write-Host ""
  Write-Host "== Step $Number. $Title" -ForegroundColor Cyan
  Write-Host $What
}

function Confirm-Run([string]$Command) {
  Write-Host "   > $Command" -ForegroundColor Yellow
  if ($ShowOnly) { return $false }
  return ((Read-Host "   Run it? [y/N]") -eq "y")
}

function Invoke-Native([string]$Exe, [string[]]$Arguments) {
  # Interactive: output goes straight to the console so wrangler's own prompts work.
  Push-Location $EdgeApp
  try {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed (exit $LASTEXITCODE)" }
  } finally { Pop-Location }
}

function Invoke-Captured([string]$Exe, [string[]]$Arguments) {
  # For commands whose output carries an id we need. Not for anything that may prompt.
  Push-Location $EdgeApp
  $previous = $ErrorActionPreference
  $ErrorActionPreference = "Continue"  # wrangler writes notices to stderr; that is not a failure
  try {
    $out = (& $Exe @Arguments 2>&1 | ForEach-Object { "$_" }) -join "`n"
    $code = $LASTEXITCODE
  } finally {
    $ErrorActionPreference = $previous
    Pop-Location
  }
  Write-Host $out
  if ($code -ne 0) { throw "$Exe $($Arguments -join ' ') failed (exit $code)" }
  return $out
}

function Get-TomlId([string]$Key) {
  $m = [regex]::Match([IO.File]::ReadAllText($Toml), "(?m)^\s*$Key\s*=\s*""([^""]*)""")
  if ($m.Success) { return $m.Groups[1].Value }
  return ""
}

function Set-TomlId([string]$Key, [string]$Value) {
  $text = [IO.File]::ReadAllText($Toml)
  $new = [regex]::Replace($text, "(?m)^(\s*$Key\s*=\s*"")$Zero("")", "`${1}$Value`${2}")
  if ($new -eq $text) { throw "Could not find a placeholder $Key in $Toml" }
  [IO.File]::WriteAllText($Toml, $new, $Utf8)
  Write-Host "   wrote $Key = $Value into edge-app\wrangler.toml" -ForegroundColor Green
}

function Get-EnvValue([string]$Name) {
  if (-not (Test-Path $EnvSaas)) { return "" }
  foreach ($line in [IO.File]::ReadAllLines($EnvSaas)) {
    if ($line -match "^\s*$Name\s*=\s*(.*)$") { return $Matches[1].Trim().Trim('"').Trim("'") }
  }
  return ""
}

function Add-EnvValue([string]$Name, [string]$Value, [switch]$Secret) {
  $current = Get-EnvValue $Name
  if ($current) {
    if ($current -ne $Value) { Write-Warning "$Name is already set in $EnvName to something else; left as it is. Check it." }
    else { Write-Host "   $Name already set" }
    return
  }
  $prefix = ""
  if ((Test-Path $EnvSaas) -and (Get-Item $EnvSaas).Length -gt 0) {
    if (-not [IO.File]::ReadAllText($EnvSaas).EndsWith("`n")) { $prefix = "`r`n" }
  }
  [IO.File]::AppendAllText($EnvSaas, "$prefix$Name=$Value`r`n", $Utf8)
  if ($Secret) { Write-Host "   added $Name (value not shown) to $EnvName" -ForegroundColor Green }
  else { Write-Host "   added $Name=$Value to $EnvName" -ForegroundColor Green }
}

function New-HexSecret {
  $bytes = New-Object byte[] 32
  $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
  try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
  return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

function Invoke-WithSecretsFile([hashtable]$Secrets, [scriptblock]$Action) {
  $tmp = Join-Path ([IO.Path]::GetTempPath()) ("doomscroller-edge-" + [guid]::NewGuid().ToString("N") + ".json")
  try {
    [IO.File]::WriteAllText($tmp, ($Secrets | ConvertTo-Json -Compress), $Utf8)
    & $Action $tmp
  } finally {
    if (Test-Path $tmp) { Remove-Item $tmp -Force }
  }
}

function Read-Secret([string]$Prompt) {
  $secure = Read-Host $Prompt -AsSecureString
  $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
  try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr).Trim() }
  finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
}

Write-Host "Beta edge setup. Worker dir: $EdgeApp"
Write-Host "SaaS env file: $EnvSaas"
Write-Host "Nothing here touches edge\ (the live Pages site) or the doomscroller-waitlist database."
if ($ShowOnly) { Write-Host "-ShowOnly: printing steps, running nothing." -ForegroundColor Yellow }

# ---- 1. tools ------------------------------------------------------------------------------------
Write-Step "1" "Tools and login" "Installs wrangler + miniflare into edge-app\node_modules (pinned versions), runs the local tests, then signs wrangler in to your Cloudflare account (browser, once)."
if (Confirm-Run "npm install") { Invoke-Native "npm" @("install", "--no-audit", "--no-fund") }
if (Confirm-Run "npm test") { Invoke-Native "npm" @("test") }
if (Confirm-Run "npx wrangler login") { Invoke-Native "npx" @("wrangler", "login") }
if (Confirm-Run "npx wrangler whoami") { Invoke-Native "npx" @("wrangler", "whoami") }

# ---- 2. laptop secrets ---------------------------------------------------------------------------
Write-Step "2" "Shared secrets" "Generates SAAS_EDGE_SECRET (the Worker signs every request with it) and SAAS_BMC_RELAY_TOKEN (the laptop's key to the relay), 64 random hex characters each, and appends them to $EnvName unless already set. Step 6 uploads the same values to the Worker."
if (Confirm-Run "append SAAS_EDGE_SECRET and SAAS_BMC_RELAY_TOKEN to $EnvName") {
  if (-not (Get-EnvValue "SAAS_EDGE_SECRET")) { Add-EnvValue "SAAS_EDGE_SECRET" (New-HexSecret) -Secret } else { Write-Host "   SAAS_EDGE_SECRET already set" }
  if (-not (Get-EnvValue "SAAS_BMC_RELAY_TOKEN")) { Add-EnvValue "SAAS_BMC_RELAY_TOKEN" (New-HexSecret) -Secret } else { Write-Host "   SAAS_BMC_RELAY_TOKEN already set" }
}

# ---- 3. D1 ---------------------------------------------------------------------------------------
Write-Step "3" "Relay database (D1)" "Creates a NEW D1 database 'doomscroller-relay' (never the waitlist's), writes its id into wrangler.toml, then creates the bmc_events table from edge-app\migrations."
if ((Get-TomlId "database_id") -eq $Zero) {
  if (Confirm-Run "npx wrangler d1 create doomscroller-relay") {
    $out = Invoke-Captured "npx" @("wrangler", "d1", "create", "doomscroller-relay")
    $m = [regex]::Match($out, "database_id[""']?\s*[:=]\s*[""']($UuidRe)")
    if (-not $m.Success) { $m = [regex]::Match($out, $UuidRe) }
    $id = if ($m.Success) { $m.Groups[$m.Groups.Count - 1].Value } else { Read-Host "   Paste the database_id it printed" }
    if ($id -notmatch "^$UuidRe$") { throw "Not a database id: $id" }
    Set-TomlId "database_id" $id
  }
} else { Write-Host "   wrangler.toml already has a database_id ($(Get-TomlId 'database_id'))." }
if (Confirm-Run "npx wrangler d1 migrations apply DB --remote") { Invoke-Native "npx" @("wrangler", "d1", "migrations", "apply", "DB", "--remote") }

# ---- 4. tunnel + VPC service ---------------------------------------------------------------------
Write-Step "4" "Tunnel and VPC service" @"
4a. Install cloudflared 2025.7.0 or later (it does not update itself on Windows):
    https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.msi
    The firewall must allow outbound UDP 7844 (QUIC).
4b. Cloudflare dashboard -> Workers VPC -> Tunnels -> Create. Name it doomscroller-laptop and save.
    Copy the token from the install command it shows, then in an ADMINISTRATOR Command Prompt run:
        cloudflared.exe service install <TUNNEL_TOKEN>
    Wait for the tunnel to show Healthy, then copy its Tunnel ID. No public hostname or domain is needed.
4c. This script then registers the laptop app (127.0.0.1:8700, plain HTTP over loopback) as a VPC service
    and writes the service id into wrangler.toml. --ipv4 127.0.0.1 is untested on Cloudflare: if
    'npx wrangler tail' later shows destination_ip_prohibited, see docs\saaseta-edge.md step 4c for the
    --hostname localhost alternative.
"@
if ((Get-TomlId "service_id") -eq $Zero) {
  if (-not $ShowOnly) { $tunnel = (Read-Host "   Tunnel ID (Enter to skip this step)").Trim() } else { $tunnel = "<TUNNEL_ID>" }
  if ($tunnel) {
    if (-not $ShowOnly -and $tunnel -notmatch "^$UuidRe$") { throw "Not a tunnel id: $tunnel" }
    $vpcArgs = @("wrangler", "vpc", "service", "create", "doomscroller-laptop", "--type", "http", "--tunnel-id", $tunnel,
                 "--ipv4", "127.0.0.1", "--http-port", "8700")
    if (Confirm-Run ("npx " + ($vpcArgs -join " "))) {
      $out = Invoke-Captured "npx" $vpcArgs
      $ids = [regex]::Matches($out, $UuidRe) | ForEach-Object { $_.Value } | Where-Object { $_ -ne $tunnel } | Select-Object -Unique
      $id = if (@($ids).Count -eq 1) { @($ids)[0] } else { Read-Host "   Paste the VPC service id it printed" }
      if ($id -notmatch "^$UuidRe$") { throw "Not a service id: $id" }
      Set-TomlId "service_id" $id
    }
  }
} else { Write-Host "   wrangler.toml already has a VPC service_id ($(Get-TomlId 'service_id'))." }

# ---- 5. check ------------------------------------------------------------------------------------
Write-Step "5" "Check before deploying" "Runs the tests and a dry-run bundle (uploads nothing). The dry run must list env.STUDIO as a VPC Service with your service id and env.DB (doomscroller-relay)."
if (Confirm-Run "npm test") { Invoke-Native "npm" @("test") }
if (Confirm-Run "npx wrangler deploy --dry-run --outdir .wrangler\dry-run") { Invoke-Native "npx" @("wrangler", "deploy", "--dry-run", "--outdir", ".wrangler\dry-run") }

# ---- 6. deploy -----------------------------------------------------------------------------------
Write-Step "6" "Deploy the Worker with its secrets" "Uploads Worker 'doomscroller-app' to workers.dev with EDGE_SECRET and RELAY_TOKEN taken from $EnvName (through a temporary file, deleted afterwards). /webhooks/bmc answers 503 until step 8."
$workerUrl = ""
if ((Get-TomlId "service_id") -eq $Zero -or (Get-TomlId "database_id") -eq $Zero) {
  Write-Warning "wrangler.toml still has a placeholder id: finish steps 3 and 4 first."
} elseif (Confirm-Run "npx wrangler deploy --secrets-file <temporary file>") {
  $edge = Get-EnvValue "SAAS_EDGE_SECRET"
  $token = Get-EnvValue "SAAS_BMC_RELAY_TOKEN"
  if ($edge.Length -lt 32 -or $token.Length -lt 32) { throw "SAAS_EDGE_SECRET and SAAS_BMC_RELAY_TOKEN must be set (32+ characters) in ${EnvName}: run step 2." }
  Invoke-WithSecretsFile @{ EDGE_SECRET = $edge; RELAY_TOKEN = $token } { param($file) Invoke-Native "npx" @("wrangler", "deploy", "--secrets-file", $file) }
}
if (-not $ShowOnly) {
  $workerUrl = (Read-Host "   The Worker URL it printed, e.g. https://doomscroller-app.<account>.workers.dev (Enter to skip)").Trim().TrimEnd("/")
  if ($workerUrl -and $workerUrl -notmatch "^https://doomscroller-app\.[a-z0-9-]+\.workers\.dev$") { throw "Unexpected Worker URL: $workerUrl" }
}

# ---- 7. laptop env -------------------------------------------------------------------------------
Write-Step "7" "Point the laptop app at the edge" "Adds SAAS_TRUST_PROXY=edge, SAAS_PUBLIC_URL and SAAS_BMC_RELAY_URL (= the Worker URL) and SAAS_MAX_UPLOAD_MB=95 (Cloudflare's free plan refuses request bodies over 100 MB) to $EnvName. Restart scripts\saas_serve.py afterwards. From then on the app answers only through the Worker URL, /admin included."
if ($workerUrl -and (Confirm-Run "append the four settings to $EnvName")) {
  Add-EnvValue "SAAS_TRUST_PROXY" "edge"
  Add-EnvValue "SAAS_PUBLIC_URL" $workerUrl
  Add-EnvValue "SAAS_BMC_RELAY_URL" $workerUrl
  Add-EnvValue "SAAS_MAX_UPLOAD_MB" "95"
}

# ---- 8. BMC --------------------------------------------------------------------------------------
$hookUrl = if ($workerUrl) { "$workerUrl/webhooks/bmc" } else { "https://doomscroller-app.<account>.workers.dev/webhooks/bmc" }
Write-Step "8" "Buy Me a Coffee webhook" @"
In Buy Me a Coffee, add a webhook:
    URL:    $hookUrl
    Events: membership.*, extra_purchase.*, donation.*
Copy its Signing Secret, then paste it below. This uploads it to the Worker as BMC_WEBHOOK_SECRET and adds
SAAS_BMC_WEBHOOK_SECRET to $EnvName (the two must be equal). Do this right after creating the webhook:
BMC disables a webhook after 10 failed deliveries in a row.
"@
if (Confirm-Run "npx wrangler secret bulk <temporary file with BMC_WEBHOOK_SECRET>") {
  $bmc = Read-Secret "   BMC Signing Secret (input hidden)"
  if (-not $bmc) { throw "Empty secret." }
  Invoke-WithSecretsFile @{ BMC_WEBHOOK_SECRET = $bmc } { param($file) Invoke-Native "npx" @("wrangler", "secret", "bulk", $file) }
  Add-EnvValue "SAAS_BMC_WEBHOOK_SECRET" $bmc -Secret
  Write-Host "   Restart scripts\saas_serve.py, then use 'Send test event' in BMC (it is stored, pulled, and acked as ignored)."
}

# ---- 9. verify -----------------------------------------------------------------------------------
$base = if ($workerUrl) { $workerUrl } else { "https://doomscroller-app.<account>.workers.dev" }
Write-Step "9" "Verify (read-only)" @"
    curl.exe -s -o NUL -w "%{http_code}\n" $base/login        200 with the laptop on, 503 with it off
    curl.exe -s -H "Authorization: Bearer <SAAS_BMC_RELAY_TOKEN>" $base/_relay/bmc/events
    npx wrangler d1 execute DB --remote --command "SELECT seq, type, live_mode, result, failures, last_error FROM bmc_events ORDER BY seq DESC LIMIT 10"
    npx wrangler tail
    (repo root) .venv-saas\Scripts\python.exe -m saas.billing.bmc_relay --once
"@
Write-Host ""
Write-Host "Done. Full notes: docs\saas\beta-edge.md" -ForegroundColor Green

# Export the marketing site and deploy it to Cloudflare Pages (production).
# Every value below is public (it is printed on the website), so this file is safe to commit.
# Secrets (IP_HASH_SALT, ADMIN_PASSWORD, TURNSTILE_SECRET_KEY) live in Cloudflare, set with
#   cd edge; npx wrangler pages secret put <NAME> --project-name doomscroller
# Usage (from the repo root):  powershell -ExecutionPolicy Bypass -File scripts\deploy_site.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$env:SAAS_ENV_FILE = ""
$env:SAAS_BRAND_NAME = "doomscroller"
$env:SAAS_PUBLIC_URL = "https://doomscroller-epy.pages.dev"
$env:SAAS_SUPPORT_EMAIL = "doomscroller.ops@proton.me"
$env:SAAS_OPERATOR_NAME = "Sarthak Parulekar"
$env:SAAS_OPERATOR_COUNTRY = "India"
$env:SAAS_LEGAL_EFFECTIVE_DATE = "27 November 2026"
# Fill these in when you have them (empty = that script is left out of the pages).
$env:GA4_MEASUREMENT_ID = ""        # G-XXXXXXXXXX  (Google Analytics 4)
$env:CLARITY_PROJECT_ID = ""        # e.g. abcd1234ef (Microsoft Clarity)
$env:SAAS_TURNSTILE_SITE_KEY = ""   # 0x4AAAA... (Cloudflare Turnstile; its secret goes in Cloudflare, not here)

& "$root\.venv-saas\Scripts\python.exe" scripts\saas_export_static.py
if ($LASTEXITCODE -ne 0) { throw "export failed" }

Set-Location "$root\edge"
npx --no-install wrangler pages deploy public --project-name doomscroller --branch main --commit-dirty=true
if ($LASTEXITCODE -ne 0) { throw "deploy failed" }

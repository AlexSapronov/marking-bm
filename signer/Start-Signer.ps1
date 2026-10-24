param(
    [Parameter(Mandatory=$true)][string]$Server,
    [Parameter(Mandatory=$true)][string]$Thumbprint,
    [switch]$Diagnose,
    [switch]$Once
)
$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
if (-not $Server.StartsWith('https://')) { throw 'Server must use HTTPS' }
$Server = $Server.TrimEnd('/')
$Thumbprint = ($Thumbprint -replace '\s','').ToUpperInvariant()
if ($Thumbprint -notmatch '^[A-F0-9]{40}$') { throw 'Expected a 40-character certificate SHA-1 thumbprint, not the connection token.' }

# Windows PowerShell's COM dynamic binder fails on some CryptoPro installations.
# The working Windows Script Host CAdESCOM path is isolated to Sign-Payload.vbs.
# Use redirected stdin/stdout: no payload, CMS, PIN or bearer token is written to disk.
$signerScript = Join-Path $PSScriptRoot 'Sign-Payload.vbs'
if (-not (Test-Path -LiteralPath $signerScript -PathType Leaf)) {
    throw "Missing Sign-Payload.vbs next to Start-Signer.ps1. Download both scripts from the same revision."
}

$cert = Get-ChildItem Cert:\CurrentUser\My |
    Where-Object { (($_.Thumbprint -replace '\s','').ToUpperInvariant()) -eq $Thumbprint } |
    Select-Object -First 1

if (-not $cert -or -not $cert.HasPrivateKey -or
    $cert.NotAfter -lt (Get-Date) -or $cert.NotBefore -gt (Get-Date)) {
    throw 'Configured certificate or private key unavailable in Current User\My. Check Rutoken and certificate.'
}
Write-Host ('Certificate found; valid until: ' + $cert.NotAfter)

function Invoke-LocalCadesSigner {
    param([Parameter(Mandatory=$true)][string]$PayloadBase64,
          [Parameter(Mandatory=$true)][bool]$Detached)
    if (-not $PayloadBase64 -or $PayloadBase64 -notmatch '^[A-Za-z0-9+/]*={0,2}$') {
        throw 'Invalid Base64 input for CAdES signing.'
    }

    $cscript = Join-Path $env:windir 'System32\cscript.exe'
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $cscript
    if ($signerScript.Contains('"')) { throw 'Unsupported quote in signer script path.' }
    $mode = if ($Detached) { 'detached' } else { 'attached' }
    $psi.Arguments = '//nologo "' + $signerScript + '" ' + $Thumbprint + ' ' + $mode
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardInput = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    # Windows PowerShell 5.1 (.NET Framework) has no ProcessStartInfo.StandardInputEncoding.
    # Input is Base64 ASCII, so StreamWriter's default encoding is sufficient.
    $psi.StandardOutputEncoding = [Text.Encoding]::ASCII
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8

    $proc = New-Object System.Diagnostics.Process
    $proc.StartInfo = $psi
    try {
        if (-not $proc.Start()) { throw 'Failed to start Windows Script Host.' }
        # Read asynchronously to avoid pipe-buffer deadlocks with large CMS output.
        $outTask = $proc.StandardOutput.ReadToEndAsync()
        $errTask = $proc.StandardError.ReadToEndAsync()
        $proc.StandardInput.WriteLine($PayloadBase64)
        $proc.StandardInput.Close()
        $proc.WaitForExit()
        $signature = $outTask.GetAwaiter().GetResult().Trim()
        $failure = $errTask.GetAwaiter().GetResult().Trim()
        if ($proc.ExitCode -ne 0) {
            # Error text reports stage/HRESULT only; never print a signature or input.
            throw ('CryptoPro signing failed: ' + $failure)
        }
        $signatureBytes = [Convert]::FromBase64String($signature)
        if ($signatureBytes.Length -lt 64 -or $signatureBytes.Length -gt 750000) {
            throw 'Unexpected CAdES signature length.'
        }
        return $signature
    }
    finally {
        if (-not $proc.HasExited) {
            try { $proc.Kill() } catch {}
        }
        $proc.Dispose()
    }
}

if ($Diagnose) {
    # Purely local test; no token, network calls or paid GIS MT/OMS actions.
    $testData = [Convert]::ToBase64String(
        [Text.Encoding]::UTF8.GetBytes('marking-bm local signing availability check')
    )
    $null = Invoke-LocalCadesSigner -PayloadBase64 $testData -Detached $true
    Write-Host 'Local detached CAdES-BES sign/verify: OK.'
    $null = Invoke-LocalCadesSigner -PayloadBase64 $testData -Detached $false
    Write-Host 'Local attached CAdES-BES sign/verify: OK.'
    return
}

$allowed = @('auth_true','auth_oms','order','report','atk','transform')
$connectionToken = $null
$headers = $null
function Send-Json($Path, $Body) {
    Invoke-RestMethod -Method Post -Uri ($Server + $Path) -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 20 -Compress)))
}

# Keep the server bearer credential out of process arguments, files and logs.
$secret = Read-Host 'Signer connection token' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $connectionToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
$secret = $null
$headers = @{ Authorization = 'Bearer ' + $connectionToken }

try {
    do {
        try {
            Send-Json '/api/signer/heartbeat' @{
                thumbprint=$Thumbprint;
                expires=$cert.NotAfter.ToUniversalTime().ToString('o');
                status='Ready'
            } | Out-Null
            $job = Send-Json '/api/signer/next' @{}
            if ($job -and $job.id) {
                if ($allowed -cnotcontains $job.kind) { throw 'Unsupported signing job' }
                if (($job.thumbprint -replace '\s','').ToUpperInvariant() -ne $Thumbprint) { throw 'Certificate mismatch' }
                if ([bool]$job.detached -ne (-not $job.kind.StartsWith('auth_'))) { throw 'Invalid signature format for job type' }
                $bytes = [Convert]::FromBase64String($job.payload_base64)
                $sha = [Security.Cryptography.SHA256]::Create()
                try {
                    $actual = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant()
                } finally { $sha.Dispose() }
                if ($actual -cne $job.sha256) { throw 'Payload checksum mismatch' }
                if ($cert.NotAfter -lt (Get-Date)) { throw 'Certificate expired' }

                # The VBS helper signs exact bytes via Base64 and locally verifies CMS.
                $signature = Invoke-LocalCadesSigner -PayloadBase64 $job.payload_base64 -Detached ([bool]$job.detached)
                Send-Json '/api/signer/result' @{
                    id=$job.id;lease=$job.lease;sha256=$job.sha256;
                    signature=$signature;thumbprint=$Thumbprint
                } | Out-Null
                Write-Host ('Signed job ' + $job.id + ' / ' + $job.kind)
            }
        }
        catch {
            # No HTTP body, payload, CMS, PIN, server token or stack dump in logs.
            Write-Warning 'Signing paused. Check server connection, CryptoPro, Rutoken and PIN. No operation is resent by this agent.'
            try {
                Send-Json '/api/signer/heartbeat' @{
                    thumbprint=$Thumbprint;
                    expires=$cert.NotAfter.ToUniversalTime().ToString('o');
                    status='Needs attention: connection or Rutoken'
                } | Out-Null
            } catch {}
        }
        if (-not $Once) { Start-Sleep -Seconds 5 }
    } while (-not $Once)
}
finally {
    $connectionToken = $null
    $headers = $null
}

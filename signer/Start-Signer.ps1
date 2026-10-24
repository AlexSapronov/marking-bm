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
# Keep the machine credential out of arguments, files, process listings and logs.
$secret = Read-Host 'Signer connection token' -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
try { $connectionToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
$headers = @{ Authorization = 'Bearer ' + $connectionToken }
$allowed = @('auth_true','auth_oms','order','report','atk','transform')
function Send-Json($Path, $Body) {
    Invoke-RestMethod -Method Post -Uri ($Server + $Path) -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 20 -Compress)))
}
$store = New-Object -ComObject CAdESCOM.Store
$store.Open(2, 'My', 2)
try {
    $cert = $null
    for ($i=1; $i -le $store.Certificates.Count; $i++) {
        $candidate = $store.Certificates.Item($i)
        if (($candidate.Thumbprint -replace '\s','').ToUpperInvariant() -eq $Thumbprint) { $cert = $candidate; break }
    }
    if (-not $cert -or -not $cert.HasPrivateKey() -or $cert.ValidToDate -lt (Get-Date) -or $cert.ValidFromDate -gt (Get-Date)) { throw 'Configured certificate or private key is unavailable. Check Rutoken and Current User certificate store.' }
    Write-Host ('Certificate: ' + $cert.SubjectName)
    Write-Host ('Valid until: ' + $cert.ValidToDate)
    if ($Diagnose) {
        # Local proof of concept. Does not send anything to GIS MT or order paid codes.
        $probe = New-Object -ComObject CAdESCOM.CadesSignedData
        $probe.ContentEncoding = 1
        $probe.Content = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('marking-bm local signing availability check'))
        $cpSigner = New-Object -ComObject CAdESCOM.CPSigner
        $cpSigner.Certificate = $cert
        $cpSigner.CheckCertificate = $true
        $cms = $probe.SignCades($cpSigner, 1, $true)
        $verifier = New-Object -ComObject CAdESCOM.CadesSignedData
        $verifier.ContentEncoding = 1
        $verifier.Content = $probe.Content
        $verifier.VerifyCades($cms, 1, $true)
        Write-Host 'Local detached CAdES-BES sign/verify: OK. Observe whether PIN/touch was requested.'
        return
    }
    do {
        try {
            Send-Json '/api/signer/heartbeat' @{thumbprint=$Thumbprint;expires=$cert.ValidToDate.ToUniversalTime().ToString('o');status='Ready'} | Out-Null
            $job = Send-Json '/api/signer/next' @{}
            if ($job -and $job.id) {
                if ($allowed -cnotcontains $job.kind) { throw 'Unsupported signing job' }
                if (($job.thumbprint -replace '\s','').ToUpperInvariant() -ne $Thumbprint) { throw 'Certificate mismatch' }
                if ($job.detached -ne (-not $job.kind.StartsWith('auth_'))) { throw 'Invalid signature format for job type' }
                $bytes = [Convert]::FromBase64String($job.payload_base64)
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $actual = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() } finally { $sha.Dispose() }
                if ($actual -cne $job.sha256) { throw 'Payload checksum mismatch' }
                if ($cert.ValidToDate -lt (Get-Date)) { throw 'Certificate expired' }
                $cpSigner = New-Object -ComObject CAdESCOM.CPSigner
                $cpSigner.Certificate = $cert
                $cpSigner.CheckCertificate = $true
                $data = New-Object -ComObject CAdESCOM.CadesSignedData
                $data.ContentEncoding = 1
                $data.Content = $job.payload_base64
                # PIN and private key are handled by the installed CryptoPro provider only.
                $signature = $data.SignCades($cpSigner, 1, [bool]$job.detached)
                $verify = New-Object -ComObject CAdESCOM.CadesSignedData
                $verify.ContentEncoding = 1
                if ($job.detached) { $verify.Content = $job.payload_base64 }
                $verify.VerifyCades($signature, 1, [bool]$job.detached)
                Send-Json '/api/signer/result' @{id=$job.id;lease=$job.lease;sha256=$job.sha256;signature=($signature -replace '\s','');thumbprint=$Thumbprint} | Out-Null
                Write-Host ('Signed job ' + $job.id + ' / ' + $job.kind)
            }
        } catch {
            # Do not dump HTTP responses, signed bodies, PIN or connection credentials.
            Write-Warning 'Signing paused. Check server connection, Rutoken, certificate and PIN prompt. No operation is resent by this agent.'
            try { Send-Json '/api/signer/heartbeat' @{thumbprint=$Thumbprint;expires=$cert.ValidToDate.ToUniversalTime().ToString('o');status='Needs attention: connection or Rutoken'} | Out-Null } catch {}
        }
        if (-not $Once) { Start-Sleep -Seconds 5 }
    } while (-not $Once)
} finally {
    $store.Close()
    $connectionToken = $null
    $headers = $null
}

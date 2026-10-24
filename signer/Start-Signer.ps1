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

# Some CryptoPro CAdESCOM installs expose COM IDispatch objects that cause
# Windows PowerShell's dynamic COM binder to throw NullReferenceException while
# reading ITypeInfo. Reflection invokes IDispatch without that dynamic scan.
# These wrappers are used for EVERY CAdESCOM property and method.
$script:ComType = [System.__ComObject]

function Invoke-CadesMethod {
    param([object]$Object, [string]$Name, [object[]]$Arguments = @())
    try {
        # Match the proven direct InvokeMember call: no parameters means a null
        # argument array. Preserve COM collections returned by invoked members.
        [object[]]$callArgs = $Arguments
        if ($callArgs.Count -eq 0) { $callArgs = $null }
        $result = $script:ComType.InvokeMember(
            $Name, [System.Reflection.BindingFlags]::InvokeMethod,
            $null, $Object, $callArgs
        )
        Write-Output -NoEnumerate $result
    }
    catch {
        throw "CAdESCOM method '$Name' failed: $($_.Exception.Message)"
    }
}
function Get-CadesProperty {
    param([object]$Object, [string]$Name, [object[]]$Arguments = @())
    try {
        # Do not pass an empty array for zero-argument COM properties.
        # Also do not let PowerShell enumerate the Certificates collection on
        # return: otherwise Count is called on a Certificate instead.
        [object[]]$callArgs = $Arguments
        if ($callArgs.Count -eq 0) { $callArgs = $null }
        $result = $script:ComType.InvokeMember(
            $Name, [System.Reflection.BindingFlags]::GetProperty,
            $null, $Object, $callArgs
        )
        Write-Output -NoEnumerate $result
    }
    catch {
        throw "CAdESCOM property '$Name' failed: $($_.Exception.Message)"
    }
}
function Set-CadesProperty {
    param([object]$Object, [string]$Name, [object]$Value)
    try {
        [void]$script:ComType.InvokeMember(
            $Name, [System.Reflection.BindingFlags]::SetProperty,
            $null, $Object, @($Value)
        )
    }
    catch {
        throw "CAdESCOM property '$Name' (set) failed: $($_.Exception.Message)"
    }
}

$allowed = @('auth_true','auth_oms','order','report','atk','transform')
$connectionToken = $null
$headers = $null
function Send-Json($Path, $Body) {
    Invoke-RestMethod -Method Post -Uri ($Server + $Path) -Headers $headers -ContentType 'application/json; charset=utf-8' -Body ([Text.Encoding]::UTF8.GetBytes(($Body | ConvertTo-Json -Depth 20 -Compress)))
}

$store = $null
try {
    $store = New-Object -ComObject CAdESCOM.Store
    Invoke-CadesMethod $store 'Open' @(2, 'My', 0) | Out-Null
    $certificates = Get-CadesProperty $store 'Certificates'
    $certCount = [int](Get-CadesProperty $certificates 'Count')
    $cert = $null
    for ($i=1; $i -le $certCount; $i++) {
        $candidate = Get-CadesProperty $certificates 'Item' @($i)
        if (((Get-CadesProperty $candidate 'Thumbprint') -replace '\s','').ToUpperInvariant() -eq $Thumbprint) {
            $cert = $candidate
            break
        }
    }
    if (-not $cert) { throw 'Configured certificate was not found in Current User / My. Check the thumbprint and Rutoken.' }
    $validTo = [datetime](Get-CadesProperty $cert 'ValidToDate')
    $validFrom = [datetime](Get-CadesProperty $cert 'ValidFromDate')
    if (-not (Invoke-CadesMethod $cert 'HasPrivateKey') -or $validTo -lt (Get-Date) -or $validFrom -gt (Get-Date)) {
        throw 'Configured certificate or private key is unavailable. Check Rutoken and Current User certificate store.'
    }
    Write-Host ('Certificate: ' + (Get-CadesProperty $cert 'SubjectName'))
    Write-Host ('Valid until: ' + $validTo)

    if ($Diagnose) {
        # Purely local sign/verify. No connection credential or server request.
        $probeContent = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('marking-bm local signing availability check'))
        $probe = New-Object -ComObject CAdESCOM.CadesSignedData
        Set-CadesProperty $probe 'ContentEncoding' 1
        Set-CadesProperty $probe 'Content' $probeContent
        $cpSigner = New-Object -ComObject CAdESCOM.CPSigner
        Set-CadesProperty $cpSigner 'Certificate' $cert
        Set-CadesProperty $cpSigner 'CheckCertificate' $true
        $cms = Invoke-CadesMethod $probe 'SignCades' @($cpSigner, 1, $true)
        $verifier = New-Object -ComObject CAdESCOM.CadesSignedData
        Set-CadesProperty $verifier 'ContentEncoding' 1
        Set-CadesProperty $verifier 'Content' $probeContent
        Invoke-CadesMethod $verifier 'VerifyCades' @($cms, 1, $true) | Out-Null
        Write-Host 'Local detached CAdES-BES sign/verify: OK. Observe whether PIN/touch was requested.'
        return
    }

    # Keep the connection secret out of command-line arguments and files.
    $secret = Read-Host 'Signer connection token' -AsSecureString
    $ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secret)
    try { $connectionToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr) }
    $secret = $null
    $headers = @{ Authorization = 'Bearer ' + $connectionToken }

    do {
        try {
            Send-Json '/api/signer/heartbeat' @{thumbprint=$Thumbprint;expires=$validTo.ToUniversalTime().ToString('o');status='Ready'} | Out-Null
            $job = Send-Json '/api/signer/next' @{}
            if ($job -and $job.id) {
                if ($allowed -cnotcontains $job.kind) { throw 'Unsupported signing job' }
                if (($job.thumbprint -replace '\s','').ToUpperInvariant() -ne $Thumbprint) { throw 'Certificate mismatch' }
                if ($job.detached -ne (-not $job.kind.StartsWith('auth_'))) { throw 'Invalid signature format for job type' }
                $bytes = [Convert]::FromBase64String($job.payload_base64)
                $sha = [Security.Cryptography.SHA256]::Create()
                try { $actual = ([BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '').ToLowerInvariant() } finally { $sha.Dispose() }
                if ($actual -cne $job.sha256) { throw 'Payload checksum mismatch' }
                if ($validTo -lt (Get-Date)) { throw 'Certificate expired' }
                $cpSigner = New-Object -ComObject CAdESCOM.CPSigner
                Set-CadesProperty $cpSigner 'Certificate' $cert
                Set-CadesProperty $cpSigner 'CheckCertificate' $true
                $data = New-Object -ComObject CAdESCOM.CadesSignedData
                Set-CadesProperty $data 'ContentEncoding' 1
                Set-CadesProperty $data 'Content' $job.payload_base64
                # PIN and private key are handled by the installed CryptoPro provider only.
                $signature = Invoke-CadesMethod $data 'SignCades' @($cpSigner, 1, [bool]$job.detached)
                $verify = New-Object -ComObject CAdESCOM.CadesSignedData
                Set-CadesProperty $verify 'ContentEncoding' 1
                if ($job.detached) { Set-CadesProperty $verify 'Content' $job.payload_base64 }
                Invoke-CadesMethod $verify 'VerifyCades' @($signature, 1, [bool]$job.detached) | Out-Null
                Send-Json '/api/signer/result' @{id=$job.id;lease=$job.lease;sha256=$job.sha256;signature=($signature -replace '\s','');thumbprint=$Thumbprint} | Out-Null
                Write-Host ('Signed job ' + $job.id + ' / ' + $job.kind)
            }
        } catch {
            # Do not dump HTTP responses, signed bodies, PIN or connection credentials.
            Write-Warning 'Signing paused. Check server connection, Rutoken, certificate and PIN prompt. No operation is resent by this agent.'
            try { Send-Json '/api/signer/heartbeat' @{thumbprint=$Thumbprint;expires=$validTo.ToUniversalTime().ToString('o');status='Needs attention: connection or Rutoken'} | Out-Null } catch {}
        }
        if (-not $Once) { Start-Sleep -Seconds 5 }
    } while (-not $Once)
} finally {
    if ($null -ne $store) {
        try { Invoke-CadesMethod $store 'Close' | Out-Null } catch {}
    }
    $connectionToken = $null
    $headers = $null
}

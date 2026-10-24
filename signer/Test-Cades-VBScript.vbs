' Local CryptoPro CAdESCOM proof of concept. No network, server token, paid orders or API calls.
' Usage: cscript.exe //nologo Test-Cades-VBScript.vbs <40-character SHA1 certificate thumbprint>
Option Explicit
Dim store, certs, cert, candidate, signer, signedData, verifier
Dim thumbprint, i, signature, stage

If WScript.Arguments.Count <> 1 Then
    WScript.Echo "Usage: cscript //nologo Test-Cades-VBScript.vbs THUMBPRINT"
    WScript.Quit 2
End If

thumbprint = UCase(Replace(Replace(WScript.Arguments(0), " ", ""), vbTab, ""))
If Len(thumbprint) <> 40 Then
    WScript.Echo "FAILED: Expected 40-character certificate thumbprint, not a connection token."
    WScript.Quit 2
End If

On Error Resume Next

Sub CheckResult(label)
    If Err.Number <> 0 Then
        WScript.Echo "FAILED at " & label & ": 0x" & Hex(Err.Number) & " - " & Err.Description
        WScript.Quit 1
    End If
End Sub

Set store = CreateObject("CAdESCOM.Store")
CheckResult "Store.CreateObject"
store.Open 2, "My", 0
CheckResult "Store.Open"

Set certs = store.Certificates
CheckResult "Store.Certificates"
Set cert = Nothing
For i = 1 To certs.Count
    Set candidate = certs.Item(i)
    CheckResult "Certificates.Item"
    If UCase(Replace(candidate.Thumbprint, " ", "")) = thumbprint Then
        Set cert = candidate
        Exit For
    End If
Next
If cert Is Nothing Then
    WScript.Echo "FAILED: Certificate not found in Current User\My."
    WScript.Quit 1
End If
WScript.Echo "1. Certificate found"

If Not cert.HasPrivateKey Then
    CheckResult "Certificate.HasPrivateKey"
    WScript.Echo "FAILED: Private key unavailable."
    WScript.Quit 1
End If
CheckResult "Certificate.HasPrivateKey"

Set signer = CreateObject("CAdESCOM.CPSigner")
CheckResult "CPSigner.CreateObject"
signer.Certificate = cert
CheckResult "CPSigner.Certificate assignment"
WScript.Echo "2. CPSigner.Certificate assigned"

signer.CheckCertificate = True
CheckResult "CPSigner.CheckCertificate"
Set signedData = CreateObject("CAdESCOM.CadesSignedData")
CheckResult "CadesSignedData.CreateObject"
signedData.ContentEncoding = 1
CheckResult "CadesSignedData.ContentEncoding"
signedData.Content = "dGVzdA=="
CheckResult "CadesSignedData.Content"

signature = signedData.SignCades(signer, 1, True)
CheckResult "CadesSignedData.SignCades"
If Len(signature) = 0 Then
    WScript.Echo "FAILED: Empty signature."
    WScript.Quit 1
End If
WScript.Echo "3. CAdES-BES detached signature created"

Set verifier = CreateObject("CAdESCOM.CadesSignedData")
CheckResult "Verifier.CreateObject"
verifier.ContentEncoding = 1
CheckResult "Verifier.ContentEncoding"
verifier.Content = "dGVzdA=="
CheckResult "Verifier.Content"
verifier.VerifyCades signature, 1, True
CheckResult "Verifier.VerifyCades"
WScript.Echo "4. Local signature verified"
WScript.Echo "SUCCESS: real local sign/verify through Windows Script Host"

store.Close

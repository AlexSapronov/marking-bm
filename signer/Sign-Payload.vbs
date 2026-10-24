' Real CAdES-BES signing helper for marking-bm.
' Called by Start-Signer.ps1 via redirected stdin/stdout, not directly by employees.
' Exact input bytes are supplied as one Base64 line over STDIN, never as an argument.
' The certificate's private key stays in CryptoPro/Rutoken. Stdout contains ONLY CMS.
Option Explicit
Dim store, certs, cert, candidate, cpSigner, signedData, verifier
Dim thumbprint, detached, payloadBase64, signature, i
Dim stage

On Error Resume Next

Sub FailAt(point)
    If Err.Number <> 0 Then
        WScript.StdErr.WriteLine "CAdES " & point & ": 0x" & Hex(Err.Number) & " - " & Err.Description
        WScript.Quit 1
    End If
End Sub

If WScript.Arguments.Count <> 2 Then
    WScript.StdErr.WriteLine "Usage: cscript //nologo Sign-Payload.vbs THUMBPRINT detached|attached"
    WScript.Quit 2
End If

thumbprint = UCase(Replace(Replace(WScript.Arguments(0), " ", ""), vbTab, ""))
If Len(thumbprint) <> 40 Then
    WScript.StdErr.WriteLine "Invalid certificate thumbprint."
    WScript.Quit 2
End If

Select Case LCase(WScript.Arguments(1))
    Case "detached"
        detached = True
    Case "attached"
        detached = False
    Case Else
        WScript.StdErr.WriteLine "Invalid signature mode."
        WScript.Quit 2
End Select

payloadBase64 = WScript.StdIn.ReadLine
FailAt "reading input"
If Len(payloadBase64) = 0 Then
    WScript.StdErr.WriteLine "Empty input."
    WScript.Quit 2
End If

Set store = CreateObject("CAdESCOM.Store")
FailAt "creating store"
store.Open 2, "My", 0
FailAt "opening store"
Set certs = store.Certificates
FailAt "listing certificates"

Set cert = Nothing
For i = 1 To certs.Count
    Set candidate = certs.Item(i)
    FailAt "reading certificate"
    If UCase(Replace(candidate.Thumbprint, " ", "")) = thumbprint Then
        Set cert = candidate
        Exit For
    End If
Next
If cert Is Nothing Then
    WScript.StdErr.WriteLine "Certificate not found in Current User\My."
    WScript.Quit 1
End If

If Not cert.HasPrivateKey Then
    FailAt "checking private key"
    WScript.StdErr.WriteLine "Certificate private key not available."
    WScript.Quit 1
End If
FailAt "checking private key"
If cert.ValidToDate < Now Or cert.ValidFromDate > Now Then
    WScript.StdErr.WriteLine "Certificate expired or not yet valid."
    WScript.Quit 1
End If
FailAt "checking certificate validity"

Set cpSigner = CreateObject("CAdESCOM.CPSigner")
FailAt "creating CPSigner"
' Confirmed with real Rutoken: VBScript assignment here MUST NOT use Set.
cpSigner.Certificate = cert
FailAt "assigning CPSigner.Certificate"
cpSigner.CheckCertificate = True
FailAt "setting certificate verification"

Set signedData = CreateObject("CAdESCOM.CadesSignedData")
FailAt "creating CadesSignedData"
signedData.ContentEncoding = 1
FailAt "setting ContentEncoding"
signedData.Content = payloadBase64
FailAt "setting signed content"
signature = signedData.SignCades(cpSigner, 1, detached)
FailAt "signing CAdES-BES"
If Len(signature) = 0 Then
    WScript.StdErr.WriteLine "CryptoPro returned empty signature."
    WScript.Quit 1
End If

Set verifier = CreateObject("CAdESCOM.CadesSignedData")
FailAt "creating signature verifier"
verifier.ContentEncoding = 1
FailAt "setting verifier ContentEncoding"
If detached Then
    verifier.Content = payloadBase64
    FailAt "setting verifier content"
End If
verifier.VerifyCades signature, 1, detached
FailAt "verifying signature"

store.Close
FailAt "closing store"
' No informational output on stdout; parent expects a single Base64 CMS value.
WScript.StdOut.WriteLine Replace(Replace(signature, vbCr, ""), vbLf, "")

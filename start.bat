@echo off
title Phone AV Bridge Windows Hub

if not exist "cert.pem" (
    echo [*] Generating Self-Signed SSL Certificate...
    powershell -Command "New-SelfSignedCertificate -DnsName 'PhoneBridge' -CertStoreLocation 'cert:\CurrentUser\My' | Out-Null"
    openssl req -x509 -newkey rsa:2048 -keyout key.pem -out cert.pem -days 365 -nodes -subj "/CN=PhoneBridge" 2>nul
)

if not exist "%USERPROFILE%\Downloads\PhoneBridge_Transfers" (
    mkdir "%USERPROFILE%\Downloads\PhoneBridge_Transfers"
)

start https://localhost:8443/
python server.py
pause

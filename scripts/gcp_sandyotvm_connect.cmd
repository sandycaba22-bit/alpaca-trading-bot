@echo off
setlocal
set "GC=C:\Users\sandy\AppData\Local\Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
set "PROJECT=project-fc0005e1-75a0-4dfa-aef"
set "ZONE=us-east4-a"
set "VM=oxowold@sandyotvm"

if not exist "%GC%" (
  echo ERROR: gcloud.cmd no encontrado en:
  echo   %GC%
  exit /b 1
)

echo === Cuentas credenciadas ===
"%GC%" auth list
echo.
echo Si NO aparece oxowold@gmail.com ACTIVE, ejecuta SOLO esto y termina el login en el navegador:
echo   "%GC%" auth login oxowold@gmail.com
echo.
pause

"%GC%" config set account oxowold@gmail.com
"%GC%" config set project %PROJECT%

echo === Lectura remota (solo git log + logs sweep) ===
"%GC%" compute ssh %VM% --zone=%ZONE% --project=%PROJECT% --command="cd ~/alpaca-trading-bot && git log -1 --oneline && ls -la logs/stocks_entry_sweep*"
if errorlevel 1 exit /b 1

echo === Actualizar ~/.ssh/config via gcloud ===
"%GC%" compute config-ssh --project=%PROJECT%

echo === Prueba ssh alias ===
ssh oxowold@sandyotvm "echo ok"
endlocal

@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"

set "LOG_DIR=logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

if not exist "%LOG_DIR%\.gitignore" (
    echo * > "%LOG_DIR%\.gitignore"
    echo ^!.gitignore >> "%LOG_DIR%\.gitignore"
)

for /f "usebackq delims=" %%a in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd_HHmmss" 2^>nul`) do set "TS=%%a"
if not defined TS set "TS=install"
set "LOG_FILE=%LOG_DIR%\install_%TS%.log"

echo =============================================== >> "%LOG_FILE%"
echo Instalacao iniciada em %date% %time% >> "%LOG_FILE%"
echo =============================================== >> "%LOG_FILE%"

echo Instalacao iniciada.
echo Log: %LOG_FILE%

REM Limpa venv antiga com Python 3.14 para evitar conflitos de versao
if exist ".venv\pyvenv.cfg" (
    findstr /C:"version = 3.14" .venv\pyvenv.cfg >nul
    if not errorlevel 1 (
        echo Detectada venv antiga com Python 3.14. Removendo para usar Python 3.11... >> "%LOG_FILE%"
        echo Detectada venv antiga com Python 3.14. Removendo para usar Python 3.11...
        rmdir /s /q .venv >> "%LOG_FILE%" 2>&1
    )
)

REM Tenta encontrar Python 3.11 ou 3.12 (que possuem binarios prontos para PyMuPDF)
set "PYTHON_CMD="

py -3.11 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.11"
    goto PYTHON_FOUND
)

py -3.12 --version >nul 2>&1
if not errorlevel 1 (
    set "PYTHON_CMD=py -3.12"
    goto PYTHON_FOUND
)

echo Python 3.11 ou 3.12 nao encontrado. Tentando instalar Python 3.11 via winget... >> "%LOG_FILE%"
echo Python 3.11 ou 3.12 nao encontrado. Tentando instalar Python 3.11 via winget...
winget install --id Python.Python.3.11 -e --silent --accept-source-agreements --accept-package-agreements >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERRO] Falha ao instalar Python 3.11. >> "%LOG_FILE%"
    echo [ERRO] Falha ao instalar Python 3.11. Instale manualmente e rode novamente.
    goto FAIL
)

echo Python 3.11 instalado. Recarregando ambiente... >> "%LOG_FILE%"
echo Python 3.11 instalado. Recarregando ambiente...
set "PATH=%LOCALAPPDATA%\Programs\Python\Python311;%LOCALAPPDATA%\Programs\Python\Python311\Scripts;%PATH%"
set "PYTHON_CMD=py -3.11"

:PYTHON_FOUND
echo Comando Python selecionado: %PYTHON_CMD% >> "%LOG_FILE%"
call %PYTHON_CMD% --version >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERRO] Comando Python falhou. >> "%LOG_FILE%"
    echo [ERRO] Comando Python falhou.
    goto FAIL
)

if not exist ".venv\Scripts\activate.bat" (
    echo Criando ambiente virtual com %PYTHON_CMD%... >> "%LOG_FILE%"
    echo Criando ambiente virtual com %PYTHON_CMD%...
    call %PYTHON_CMD% -m venv .venv >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [ERRO] Falha ao criar ambiente virtual. >> "%LOG_FILE%"
        echo [ERRO] Falha ao criar ambiente virtual.
        goto FAIL
    )
) else (
    echo Ambiente virtual ja existe. >> "%LOG_FILE%"
    echo Ambiente virtual ja existe.
)

call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERRO] Falha ao ativar ambiente virtual. >> "%LOG_FILE%"
    echo [ERRO] Falha ao ativar ambiente virtual.
    goto FAIL
)

if not exist "requirements.txt" (
    echo [ERRO] Arquivo requirements.txt nao encontrado. >> "%LOG_FILE%"
    echo [ERRO] Arquivo requirements.txt nao encontrado.
    goto FAIL
)

echo Atualizando pip... >> "%LOG_FILE%"
echo Atualizando pip...
python -m pip install --upgrade pip >> "%LOG_FILE%" 2>&1

echo Instalando dependencias do requirements.txt... >> "%LOG_FILE%"
echo Instalando dependencias do requirements.txt...
python -m pip install -r requirements.txt >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERRO] Falha ao instalar dependencias. >> "%LOG_FILE%"
    echo [ERRO] Falha ao instalar dependencias.
    goto FAIL
)

where tesseract >nul 2>nul
if errorlevel 1 (
    echo Tesseract OCR nao encontrado. Tentando instalar via winget... >> "%LOG_FILE%"
    echo Tesseract OCR nao encontrado. Tentando instalar via winget...
    winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-source-agreements --accept-package-agreements >> "%LOG_FILE%" 2>&1
)

echo =============================================== >> "%LOG_FILE%"
echo Instalacao concluida com sucesso. >> "%LOG_FILE%"
echo =============================================== >> "%LOG_FILE%"
echo Instalacao concluida com sucesso.
echo Agora execute executar.bat.
pause
exit /b 0

:FAIL
echo =============================================== >> "%LOG_FILE%"
echo Instalacao falhou. Verifique o log. >> "%LOG_FILE%"
echo =============================================== >> "%LOG_FILE%"
echo Instalacao falhou. Verifique o log: %LOG_FILE%
pause
exit /b 1
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

where winget >nul 2>nul
if errorlevel 1 (
    echo [AVISO] winget nao encontrado. >> "%LOG_FILE%"
    echo [AVISO] winget nao encontrado.
    goto CHECK_PYTHON
)

where python >nul 2>nul
if errorlevel 1 (
    echo Python nao encontrado. Tentando instalar Python via winget... >> "%LOG_FILE%"
    echo Python nao encontrado. Tentando instalar Python via winget...
    winget install --id Python.Python.3.11 -e --silent --accept-source-agreements --accept-package-agreements >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [ERRO] Falha ao instalar Python. >> "%LOG_FILE%"
        echo [ERRO] Falha ao instalar Python.
        goto FAIL
    )
    echo Python instalado. Se o script nao detectar Python agora, feche esta janela e execute instalar.bat novamente. >> "%LOG_FILE%"
    echo Python instalado. Se o script nao detectar Python agora, feche esta janela e execute instalar.bat novamente.
) else (
    echo Python ja esta instalado. >> "%LOG_FILE%"
    echo Python ja esta instalado.
)

:CHECK_PYTHON
set "PYTHON_CMD="

where py >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=py -3"
    goto PYTHON_FOUND
)

where python >nul 2>nul
if not errorlevel 1 (
    set "PYTHON_CMD=python"
    goto PYTHON_FOUND
)

if exist "%SystemRoot%\py.exe" (
    set "PYTHON_CMD="%SystemRoot%\py.exe" -3"
    goto PYTHON_FOUND
)

if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" (
    set "PYTHON_CMD="%LOCALAPPDATA%\Programs\Python\Python311\python.exe""
    goto PYTHON_FOUND
)

if exist "%ProgramFiles%\Python311\python.exe" (
    set "PYTHON_CMD="%ProgramFiles%\Python311\python.exe""
    goto PYTHON_FOUND
)

echo [ERRO] Python nao encontrado apos tentativa de instalacao. >> "%LOG_FILE%"
echo [ERRO] Python nao encontrado apos tentativa de instalacao.
echo [ERRO] Instale Python manualmente e execute instalar.bat novamente.
goto FAIL

:PYTHON_FOUND
echo Comando Python selecionado: %PYTHON_CMD% >> "%LOG_FILE%"
call %PYTHON_CMD% --version >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERRO] Comando Python falhou. >> "%LOG_FILE%"
    echo [ERRO] Comando Python falhou.
    goto FAIL
)

if not exist ".venv\Scripts\activate.bat" (
    echo Criando ambiente virtual... >> "%LOG_FILE%"
    echo Criando ambiente virtual...
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
if errorlevel 1 (
    echo [ERRO] Falha ao atualizar pip. >> "%LOG_FILE%"
    echo [ERRO] Falha ao atualizar pip.
    goto FAIL
)

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
    where winget >nul 2>nul
    if not errorlevel 1 (
        echo Tesseract OCR nao encontrado. Tentando instalar via winget... >> "%LOG_FILE%"
        echo Tesseract OCR nao encontrado. Tentando instalar via winget...
        winget install --id UB-Mannheim.TesseractOCR -e --silent --accept-source-agreements --accept-package-agreements >> "%LOG_FILE%" 2>&1
        if errorlevel 1 (
            echo [AVISO] Falha ao instalar Tesseract OCR. >> "%LOG_FILE%"
            echo [AVISO] Falha ao instalar Tesseract OCR.
        )
    ) else (
        echo [AVISO] Tesseract OCR nao encontrado e winget nao esta disponivel. >> "%LOG_FILE%"
        echo [AVISO] Tesseract OCR nao encontrado e winget nao esta disponivel.
    )
) else (
    echo Tesseract OCR ja esta instalado. >> "%LOG_FILE%"
    echo Tesseract OCR ja esta instalado.
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
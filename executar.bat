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
if not defined TS set "TS=run"
set "LOG_FILE=%LOG_DIR%\run_%TS%.log"

echo =============================================== >> "%LOG_FILE%"
echo Execucao iniciada em %date% %time% >> "%LOG_FILE%"
echo =============================================== >> "%LOG_FILE%"

echo Iniciando execucao do projeto.
echo Log: %LOG_FILE%

if not exist "app.py" (
    echo [ERRO] Arquivo app.py nao encontrado. >> "%LOG_FILE%"
    echo [ERRO] Arquivo app.py nao encontrado.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\activate.bat" (
    echo [ERRO] Ambiente virtual nao encontrado. Execute instalar.bat primeiro. >> "%LOG_FILE%"
    echo [ERRO] Ambiente virtual nao encontrado. Execute instalar.bat primeiro.
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat" >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [ERRO] Falha ao ativar ambiente virtual. >> "%LOG_FILE%"
    echo [ERRO] Falha ao ativar ambiente virtual.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo [ERRO] Python nao encontrado no ambiente virtual. >> "%LOG_FILE%"
    echo [ERRO] Python nao encontrado no ambiente virtual.
    pause
    exit /b 1
)

python -m streamlit --version >> "%LOG_FILE%" 2>&1
if errorlevel 1 (
    echo [AVISO] Streamlit nao encontrado. Instalando dependencias... >> "%LOG_FILE%"
    echo [AVISO] Streamlit nao encontrado. Instalando dependencias...

    if not exist "requirements.txt" (
        echo [ERRO] Arquivo requirements.txt nao encontrado. >> "%LOG_FILE%"
        echo [ERRO] Arquivo requirements.txt nao encontrado.
        pause
        exit /b 1
    )

    python -m pip install -r requirements.txt >> "%LOG_FILE%" 2>&1
    if errorlevel 1 (
        echo [ERRO] Falha ao instalar dependencias. >> "%LOG_FILE%"
        echo [ERRO] Falha ao instalar dependencias.
        pause
        exit /b 1
    )
)

set NO_COLOR=1
set PYTHONUNBUFFERED=1

echo Executando Streamlit... >> "%LOG_FILE%"
python -m streamlit run app.py --logger.level=info >> "%LOG_FILE%" 2>&1
set "EXIT_CODE=%ERRORLEVEL%"

echo =============================================== >> "%LOG_FILE%"
echo Streamlit finalizado com codigo %EXIT_CODE% >> "%LOG_FILE%"
echo =============================================== >> "%LOG_FILE%"

echo Streamlit finalizado com codigo %EXIT_CODE%.
pause
exit /b %EXIT_CODE%
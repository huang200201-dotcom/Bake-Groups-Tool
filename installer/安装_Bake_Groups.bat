@echo off
setlocal
chcp 65001 >nul
title Bake Master Installer

set "SOURCE=%~dp0Bake_Groups"
set "MAYA_BASE="
set "DOCS="
set "HAD_TARGET=0"

if defined MAYA_APP_DIR set "MAYA_BASE=%MAYA_APP_DIR%"
if defined MAYA_BASE goto :maya_base_ready

for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('MyDocuments')" 2^>nul`) do set "DOCS=%%I"
if not defined DOCS goto :path_error
set "MAYA_BASE=%DOCS%\maya"

:maya_base_ready
if not defined MAYA_BASE goto :path_error
for %%I in ("%SOURCE%") do set "SOURCE=%%~fI"
for %%I in ("%MAYA_BASE%") do set "MAYA_BASE=%%~fI"
set "TARGET=%MAYA_BASE%\scripts\Bake_Groups"
set "STAGING=%MAYA_BASE%\scripts\.Bake_Groups_install_%RANDOM%_%RANDOM%"
set "BACKUP=%MAYA_BASE%\scripts\.Bake_Groups_backup_%RANDOM%_%RANDOM%"
set "FAILED=%MAYA_BASE%\scripts\.Bake_Groups_failed_%RANDOM%_%RANDOM%"

call :verify_plugin "%SOURCE%"
if errorlevel 1 goto :source_error

if /I "%SOURCE%"=="%TARGET%" (
    echo.
    echo 安装失败：安装源和目标目录不能相同。
    echo "%TARGET%"
    echo.
    pause
    exit /b 1
)

if exist "%STAGING%\" goto :temporary_error
if exist "%BACKUP%\" goto :temporary_error
if exist "%FAILED%\" goto :temporary_error

if not exist "%TARGET%\" goto :stage_copy
dir /b /a "%TARGET%" 2>nul | findstr . >nul
if errorlevel 1 goto :stage_copy
if not exist "%TARGET%\launcher.py" goto :target_error
if not exist "%TARGET%\active_version.json" goto :target_error

:stage_copy
if not exist "%MAYA_BASE%\scripts\" mkdir "%MAYA_BASE%\scripts"
if not exist "%MAYA_BASE%\scripts\" goto :copy_error
mkdir "%STAGING%"
if not exist "%STAGING%\" goto :copy_error

robocopy "%SOURCE%" "%STAGING%" /E /XD __pycache__ /XF *.pyc desktop.ini >nul
if errorlevel 8 goto :stage_copy_error
call :verify_plugin "%STAGING%"
if errorlevel 1 goto :stage_copy_error

if not exist "%TARGET%\" goto :activate_staging
move "%TARGET%" "%BACKUP%" >nul
if errorlevel 1 goto :backup_error
set "HAD_TARGET=1"

:activate_staging
move "%STAGING%" "%TARGET%" >nul
if errorlevel 1 goto :activate_error
call :verify_plugin "%TARGET%"
if errorlevel 1 goto :verify_error

if exist "%BACKUP%\" rmdir /S /Q "%BACKUP%"
if exist "%BACKUP%\" goto :success_with_backup

:success
echo.
echo Bake Master 已安装到：
echo "%TARGET%"
echo.
echo 下一步：
echo 1. 启动或重启 Maya。
echo 2. 如果工具架没有 BAKE GROUPS 按钮，请把“创建工具架按钮.py”拖进 Maya 视口。
echo.
pause
exit /b 0

:success_with_backup
echo.
echo Bake Master 已安装到：
echo "%TARGET%"
echo.
echo 旧版本备份未能自动删除，可在关闭 Maya 后手动删除：
echo "%BACKUP%"
echo.
pause
exit /b 0

:path_error
echo.
echo 安装失败：无法确定 Maya 用户目录。
echo 请确认 MAYA_APP_DIR 设置正确，或确认 Windows“文档”目录可以正常访问。
echo.
pause
exit /b 1

:source_error
echo.
echo 安装包不完整：缺少 Bake_Groups 的必要文件或 Maya 二进制文件。
echo 请重新解压完整 ZIP 后再安装。
echo.
pause
exit /b 1

:target_error
echo.
echo 安装已停止：目标目录已存在，但其中没有 Bake Master 插件标记。
echo 为避免误删其他文件，没有覆盖该目录：
echo "%TARGET%"
echo.
pause
exit /b 1

:temporary_error
echo.
echo 安装已停止：临时目录名称发生冲突。
echo 请重新双击安装程序。
echo.
pause
exit /b 1

:stage_copy_error
if exist "%STAGING%\" rmdir /S /Q "%STAGING%"
goto :copy_error

:backup_error
if exist "%STAGING%\" rmdir /S /Q "%STAGING%"
echo.
echo 安装已停止：旧插件正在被 Maya 占用，无法创建安全备份。
echo 请完全关闭 Maya 后再重试。
echo.
pause
exit /b 1

:activate_error
if "%HAD_TARGET%"=="1" move "%BACKUP%" "%TARGET%" >nul
if exist "%STAGING%\" rmdir /S /Q "%STAGING%"
if "%HAD_TARGET%"=="1" if not exist "%TARGET%\" goto :rollback_error
goto :copy_error

:verify_error
move "%TARGET%" "%FAILED%" >nul
if exist "%TARGET%\" goto :rollback_error
if "%HAD_TARGET%"=="1" move "%BACKUP%" "%TARGET%" >nul
if "%HAD_TARGET%"=="1" if not exist "%TARGET%\" goto :rollback_error
if exist "%FAILED%\" rmdir /S /Q "%FAILED%"
goto :copy_error

:rollback_error
echo.
echo 安装失败，并且自动恢复未能完成。
echo 请不要删除以下目录，并在关闭 Maya 后检查：
echo 目标："%TARGET%"
echo 备份："%BACKUP%"
echo 临时："%STAGING%"
echo.
pause
exit /b 1

:copy_error
echo.
echo 安装失败，插件文件未能安全安装到：
echo "%TARGET%"
echo 请关闭 Maya 后重试，并确认该目录有写入权限。
echo.
pause
exit /b 1

:verify_plugin
if not exist "%~1\launcher.py" exit /b 1
if not exist "%~1\active_version.json" exit /b 1
if not exist "%~1\versions\1.3.15\bg_main_window.py" exit /b 1
for %%V in (2022 2023 2024 2025 2026 2027) do if not exist "%~1\versions\1.3.15\bin\%%V\bg_math_core.pyd" exit /b 1
exit /b 0

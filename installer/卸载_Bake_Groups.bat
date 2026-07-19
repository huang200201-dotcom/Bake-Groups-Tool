@echo off
setlocal
chcp 65001 >nul
title Uninstall Bake Master

set "MAYA_BASE="
set "DOCS="

if defined MAYA_APP_DIR set "MAYA_BASE=%MAYA_APP_DIR%"
if defined MAYA_BASE goto :maya_base_ready

for /f "usebackq delims=" %%I in (`powershell.exe -NoProfile -Command "[Environment]::GetFolderPath('MyDocuments')" 2^>nul`) do set "DOCS=%%I"
if not defined DOCS goto :path_error
set "MAYA_BASE=%DOCS%\maya"

:maya_base_ready
if not defined MAYA_BASE goto :path_error
for %%I in ("%MAYA_BASE%") do set "MAYA_BASE=%%~fI"
set "TARGET=%MAYA_BASE%\scripts\Bake_Groups"

if not exist "%TARGET%\" (
    echo 未找到安装目录：
    echo "%TARGET%"
    pause
    exit /b 0
)

if not exist "%TARGET%\launcher.py" goto :not_plugin
if not exist "%TARGET%\active_version.json" goto :not_plugin

echo 将删除：
echo "%TARGET%"
echo.
choice /C YN /N /M "确认卸载？[Y/N] "
if errorlevel 2 exit /b 0

rmdir /S /Q "%TARGET%"
if exist "%TARGET%\" goto :delete_error

echo.
echo 插件文件已删除。工具架按钮可在 Maya 中右键删除。
echo.
pause
exit /b 0

:path_error
echo.
echo 卸载失败：无法确定 Maya 用户目录。
echo 请确认 MAYA_APP_DIR 设置正确，或确认 Windows“文档”目录可以正常访问。
echo.
pause
exit /b 1

:not_plugin
echo.
echo 卸载已停止：目标目录中没有找到 Bake Master 插件标记。
echo 为避免误删其他文件，没有删除该目录：
echo "%TARGET%"
echo.
pause
exit /b 1

:delete_error
echo.
echo 卸载失败，仍有文件无法删除：
echo "%TARGET%"
echo 请关闭 Maya 和 Bake Master 后重试，并确认该目录有删除权限。
echo.
pause
exit /b 1

param(
    [Parameter(Mandatory = $true)][string]$MayaRoot,
    [Parameter(Mandatory = $true)][string]$MayaVersion,
    [string]$RuntimeSource = '',
    [string]$PybindInclude = 'D:\Maya_SDK\pybind11-2.13\pybind11\include',
    [string]$PythonLib = '',
    [string]$NativeGuardKeyFile = 'D:\Bake_Groups_License_Keys\native_guard.key'
)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
if ([string]::IsNullOrWhiteSpace($RuntimeSource)) {
    $RuntimeSource = Join-Path $repoRoot 'plugin\Bake_Groups\versions\1.3.12'
}
$vcvars = 'C:\Program Files (x86)\Microsoft Visual Studio\2022\BuildTools\VC\Auxiliary\Build\vcvarsall.bat'
if (!(Test-Path $vcvars)) { throw "Visual Studio Build Tools not found: $vcvars" }
$nativeGuardKey = (Get-Content -LiteralPath $NativeGuardKeyFile -Raw).Trim().ToLowerInvariant()
if ($nativeGuardKey -notmatch '^[0-9a-f]{64}$') { throw "Native guard key is invalid: $NativeGuardKeyFile" }
$mayaRoot = (Resolve-Path $MayaRoot).Path
$source = (Resolve-Path (Join-Path $RuntimeSource 'bg_math_core.cpp')).Path
$outDir = Join-Path (Resolve-Path $RuntimeSource).Path ("bin\{0}" -f $MayaVersion)
$pythonIncludeCandidates = Get-ChildItem (Join-Path $mayaRoot 'include') -Directory | Where-Object Name -like 'Python*'
if (![string]::IsNullOrWhiteSpace($PythonLib)) {
    $pythonDigits = [regex]::Match([IO.Path]::GetFileNameWithoutExtension($PythonLib), '\d+').Value
    $pythonIncludeRoot = $pythonIncludeCandidates | Where-Object Name -eq ("Python{0}" -f $pythonDigits) | Select-Object -First 1
} else {
    $pythonIncludeRoot = $pythonIncludeCandidates | Sort-Object Name -Descending | Select-Object -First 1
}
if (!$pythonIncludeRoot) { throw "Python include directory not found under $mayaRoot\include" }
$include = Join-Path $pythonIncludeRoot.FullName 'Py_'
if (!(Test-Path (Join-Path $include 'Python.h'))) { $include = Join-Path $pythonIncludeRoot.FullName 'Python' }
if (!(Test-Path (Join-Path $include 'Python.h'))) { throw "Python.h not found under $($pythonIncludeRoot.FullName)" }
$libDir = Join-Path $mayaRoot 'lib'
if ([string]::IsNullOrWhiteSpace($PythonLib)) {
    $pythonLibFile = Get-ChildItem $libDir -Filter 'python*.lib' | Select-Object -First 1
} else {
    $pythonLibFile = Get-Item $PythonLib
    $libDir = $pythonLibFile.DirectoryName
}
if (!$pythonLibFile) { throw "Maya Python import library not found under $libDir" }
New-Item -ItemType Directory -Force -Path $outDir | Out-Null
$guardDefine = '/DBG_NATIVE_GUARD_KEY_HEX=\"{0}\"' -f $nativeGuardKey
$cmd = "call `"$vcvars`" x64 && cl /nologo /LD /EHsc /std:c++17 $guardDefine /I`"$PybindInclude`" /I`"$include`" `"$source`" /link /LIBPATH:`"$libDir`" $($pythonLibFile.Name) /OUT:`"$(Join-Path $outDir 'bg_math_core.pyd')`""
cmd.exe /d /c $cmd
if ($LASTEXITCODE -ne 0) { throw "Native build failed for Maya $MayaVersion" }
Write-Output "Built native guard: $(Join-Path $outDir 'bg_math_core.pyd')"

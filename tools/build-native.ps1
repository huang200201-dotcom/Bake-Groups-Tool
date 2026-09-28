<#
.SYNOPSIS
Build the pure geometry extension for one Maya/CPython ABI on Windows x64.
.DESCRIPTION
Requires Visual Studio C++ Build Tools, pybind11 headers (2.13 tested), and Python
headers/import library matching Maya. Autodesk devkits commonly omit the import
library; supply -PythonLibrary from the matching CPython installation in that case.
No Maya libraries, credentials, network access, or private build inputs are used.
.EXAMPLE
./tools/build-native.ps1 -MayaVersion 2027 -MayaSdkRoot C:/SDK/Maya2027/devkitBase -PythonLibrary C:/Python313/libs/python313.lib -PybindInclude C:/SDK/pybind11/include
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('2022', '2023', '2024', '2025', '2026', '2027')]
    [string]$MayaVersion,
    [string]$MayaSdkRoot,
    [string]$PythonInclude,
    [string]$PythonLibrary,
    [Parameter(Mandatory = $true)][string]$PybindInclude,
    [string]$VcVarsAll,
    [string]$OutputDirectory
)
$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonAbi = @{'2022' = '37'; '2023' = '39'; '2024' = '310'; '2025' = '311'; '2026' = '311'; '2027' = '313'}[$MayaVersion]

function Resolve-BuildFile([string]$Path, [string]$Label) {
    if (!$Path -or !(Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "$Label not found: $Path"
    }
    return (Resolve-Path -LiteralPath $Path).Path
}

if (!$PythonInclude) {
    if (!$MayaSdkRoot) { throw 'Supply -MayaSdkRoot or -PythonInclude.' }
    $candidates = @(
        (Join-Path $MayaSdkRoot "include/Python$pythonAbi/Py_"),
        (Join-Path $MayaSdkRoot "include/Python$pythonAbi/Python"),
        (Join-Path $MayaSdkRoot "include/Python$pythonAbi"),
        (Join-Path $MayaSdkRoot 'include')
    )
    $PythonInclude = $candidates | Where-Object { Test-Path -LiteralPath (Join-Path $_ 'Python.h') -PathType Leaf } | Select-Object -First 1
}
if (!$PythonInclude) { throw "Matching Python$pythonAbi headers were not found; supply -PythonInclude." }
$pythonHeader = Resolve-BuildFile (Join-Path $PythonInclude 'Python.h') 'Python header'
$PythonInclude = Split-Path $pythonHeader
$patchLevel = Get-Content -LiteralPath (Resolve-BuildFile (Join-Path $PythonInclude 'patchlevel.h') 'Python ABI header') -Raw
$major = [regex]::Match($patchLevel, '(?m)^#define\s+PY_MAJOR_VERSION\s+(\d+)').Groups[1].Value
$minor = [regex]::Match($patchLevel, '(?m)^#define\s+PY_MINOR_VERSION\s+(\d+)').Groups[1].Value
if ("$major$minor" -ne $pythonAbi) { throw "Maya $MayaVersion requires CPython $pythonAbi; headers are $major.$minor." }

if (!$PythonLibrary -and $MayaSdkRoot) {
    $PythonLibrary = Join-Path $MayaSdkRoot "lib/python$pythonAbi.lib"
}
$PythonLibrary = Resolve-BuildFile $PythonLibrary 'Python import library (use -PythonLibrary)'
if ([IO.Path]::GetFileName($PythonLibrary) -ne "python$pythonAbi.lib") {
    throw "Expected python$pythonAbi.lib for Maya $MayaVersion."
}
$pybindHeader = Resolve-BuildFile (Join-Path $PybindInclude 'pybind11/pybind11.h') 'pybind11 header'
$PybindInclude = Split-Path (Split-Path $pybindHeader)

if (!$VcVarsAll) {
    $vswhere = Get-Command 'vswhere.exe' -ErrorAction SilentlyContinue
    $vswherePath = if ($vswhere) { $vswhere.Source } else {
        Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
    }
    $vswherePath = Resolve-BuildFile $vswherePath 'Visual Studio locator (or supply -VcVarsAll)'
    $visualStudio = & $vswherePath -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if (!$visualStudio) { throw 'Visual Studio C++ x64 build tools were not found.' }
    $VcVarsAll = Join-Path ($visualStudio | Select-Object -First 1) 'VC/Auxiliary/Build/vcvarsall.bat'
}
$VcVarsAll = Resolve-BuildFile $VcVarsAll 'Visual Studio environment script'
if (!$OutputDirectory) { $OutputDirectory = Join-Path $repoRoot "build/native/$MayaVersion" }
$OutputDirectory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Path $OutputDirectory -Force | Out-Null
$source = Resolve-BuildFile (Join-Path $repoRoot 'native/bg_math_core.cpp') 'Native source'
$binary = Join-Path $OutputDirectory 'bg_math_core.pyd'
$response = Join-Path $OutputDirectory 'compile.rsp'
$arguments = @(
    '/nologo', '/LD', '/EHsc', '/std:c++17', '/O2', '/GL', '/Gy', '/MD', '/DNDEBUG', '/utf-8',
    ('/Fo"{0}"' -f (Join-Path $OutputDirectory 'bg_math_core.obj')),
    ('/I"{0}"' -f $PybindInclude), ('/I"{0}"' -f $PythonInclude), ('"{0}"' -f $source),
    '/link', '/LTCG', '/OPT:REF', '/OPT:ICF', '/MACHINE:X64',
    ('/IMPLIB:"{0}"' -f (Join-Path $OutputDirectory 'bg_math_core.lib')),
    ('"{0}"' -f $PythonLibrary), ('/OUT:"{0}"' -f $binary)
)
# cl.exe accepts a Unicode response file, keeping paths out of shell syntax.
($arguments -join ' ') | Set-Content -LiteralPath $response -Encoding Unicode
if ($VcVarsAll -match '["\r\n%]' -or $response -match '["\r\n%]') {
    throw 'Compiler environment and response-file paths cannot contain quotes, newlines or percent signs.'
}
$savedCompilerOptions = @{CL = $env:CL; '_CL_' = $env:_CL_}
try {
    # Build only from the reviewed response file, regardless of caller CL flags.
    $env:CL = ''
    $env:_CL_ = ''
    $command = 'call "{0}" x64 > nul && cl.exe "@{1}"' -f $VcVarsAll, $response
    & $env:ComSpec /d /s /c $command
    if ($LASTEXITCODE -ne 0) { throw "Native compilation failed for Maya $MayaVersion (exit $LASTEXITCODE)." }
}
finally {
    $env:CL = $savedCompilerOptions.CL
    $env:_CL_ = $savedCompilerOptions['_CL_']
}
if (!(Test-Path -LiteralPath $binary -PathType Leaf)) { throw 'Compiler did not produce the native extension.' }
Write-Output "Built Maya $MayaVersion / CPython $major.$minor x64: $binary"

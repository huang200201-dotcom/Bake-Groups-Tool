# Bake Master

Bake Master 是 Autodesk Maya 的高低模烘焙分组插件。本仓库为私有源码仓库，保存可维护源码、构建脚本、安装器和自动化测试。

## 当前版本

- 插件与运行时：`1.3.15`
- Maya：2022-2027（Windows x64）
- Python ABI：3.7 / 3.9 / 3.10 / 3.11 / 3.13
- 授权：Ed25519 签名许可证、机器绑定、Windows DPAPI、14 天离线宽限
- 原生保护：六个 Maya 版本均包含 C++ 原生授权门禁

## 仓库结构

- `plugin/Bake_Groups/`：启动器、配置和版本化运行时
- `installer/`：完整安装包入口
- `scripts/`：原生模块与热更新包构建脚本
- `test_*.py`：更新、授权、回滚、算法和性能回归测试

## 发布方式

源码仓库保持私有。用户端不需要 GitHub Token，独立签名的更新清单和经 SHA-256 校验的 ZIP 发布在公开仓库 `huang200201-dotcom/Bake-Groups-License-Status`。发布包只包含运行代码和六个 `.pyd`，不包含 C++ 源码、Token、许可证私钥或原生证明密钥文件。

构建热更新包：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\build-release.ps1 `
  -Version 1.3.15 -RuntimeVersion 1.3.15 `
  -Owner huang200201-dotcom -Repository Bake-Groups-License-Status
```

构建后必须用 `D:\Bake_Groups_License_Manager\license_admin.py sign-update` 对 `dist\stable.json` 签名，并在上传前运行 `verify-update`。更新签名私钥只保存在授权管理器的受限 `private` 目录中。

原生模块必须在管理员持有密钥的受控机器上构建。密钥文件禁止提交到任何 Git 仓库。

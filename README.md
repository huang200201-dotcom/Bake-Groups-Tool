# Bake Master

Bake Master 是 Autodesk Maya 的高低模烘焙分组插件。本仓库保存私有源码、构建脚本和内部发布资料。

## 当前版本

- 发布版本：1.3.10（运行时 1.3.9）
- Maya：2022–2027（Windows x64）
- 授权：Ed25519 签名许可证、机器绑定、Windows DPAPI、14 天离线宽限
- 原生保护：六个 Maya 版本均包含 C++ 原生门禁和 Windows CNG SHA-256 会话验证

## 公开热更新

用户端不需要 GitHub Token。插件从公开仓库获取签名状态和稳定更新清单：

- 发布仓库：`huang200201-dotcom/Bake-Groups-License-Status`
- 更新清单：`updates/stable.json`
- 当前发布标签：`v1.3.10`

公开仓库只包含发布包、SHA256、更新清单和签名授权状态，不包含源码、许可证私钥或 GitHub Token。

## 安全要求

许可证私钥仅保存在管理员电脑中，禁止提交到任何 Git 仓库或打包进插件。发布包必须通过 SHA256、文件清单和原生门禁审计。

# Bake Groups Tool 私有源码仓库

本仓库保存插件源码快照和内部发布资料，仓库权限保持 **Private**。

## 公开分发仓库

用户端不再输入 GitHub Token。插件从公开发布仓库下载经过 SHA256 校验的安装包：

- 发布仓库：[Bake-Groups-License-Status](https://github.com/huang200201-dotcom/Bake-Groups-License-Status)
- 更新清单：`updates/stable.json`
- 当前版本：`v1.3.9`

公开仓库不包含源码、许可证私钥或 GitHub Token。

## 授权机制

插件使用 Ed25519 签名许可证绑定机器指纹，并使用 Windows DPAPI 保存本地许可证。每次启动会检查签名授权状态；联网失败时最多允许 14 天离线使用。管理员可通过公开仓库中的签名 `status.json` 停用许可证。

许可证私钥只保存在管理员电脑的 `D:\Bake_Groups_License_Keys\ed25519_private.pem`，禁止上传到任何仓库。
# Bake Groups Tool 私有源码仓库

本仓库保存插件源码快照和内部发布资料，仓库权限保持 **Private**。

## 公开分发仓库

用户端不再输入 GitHub Token。插件从公开发布仓库下载经过 SHA256 校验的安装包：

- 发布仓库：[Bake-Groups-License-Status](https://github.com/huang200201-dotcom/Bake-Groups-License-Status)
- 更新清单：`updates/stable.json`
- 当前版本：`v1.3.8-license-final`

公开仓库不包含源码、许可证私钥或 GitHub Token。

## 授权机制

插件使用 Ed25519 签名许可证绑定机器指纹，并使用 Windows DPAPI 保存本地许可证。每次启动会检查签名授权状态；联网失败时最多允许 14 天离线使用。管理员可通过公开仓库中的签名 `status.json` 停用许可证。

许可证私钥只保存在管理员电脑的 `D:\Bake_Groups_License_Keys\ed25519_private.pem`，禁止上传到任何仓库。

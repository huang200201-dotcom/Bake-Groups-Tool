# Bake Groups Tool 私有更新仓库

此仓库用于 Bake Groups Tool 的私有版本发布和自动更新。

## 当前通道

- 稳定清单：`updates/stable.json`
- Release：`v1.3.8`
- 更新包：`Bake_Groups_Private_Release.zip`
- 仓库权限：Private

## 发布新版本

1. 从本地发布框架生成新的 ZIP。
2. 在 Releases 新建版本标签并上传 ZIP。
3. 计算 ZIP 的 SHA256 和文件大小。
4. 更新 `updates/stable.json` 中的版本、标签、文件名、SHA256 与大小。
5. 在 Maya 插件中执行“检查更新”验证。

## Token 安全

插件不包含明文 Token。用户首次使用更新功能时输入只读 fine-grained PAT，插件使用 Windows DPAPI 加密保存到当前 Windows 用户目录。PAT 仅授予本仓库 `Contents: Read-only` 权限。

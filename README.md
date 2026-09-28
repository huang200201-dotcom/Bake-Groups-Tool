# Bake Master 1.0.1

面向 Autodesk Maya 的开源高低模烘焙分组与资产任务工具。自动整理 HP / LP，检查最终分组，生成 Cage，并导出用于烘焙的 FBX。

[下载安装包](https://github.com/huang200201-dotcom/Bake-Groups-Tool/releases/latest) · [安装与使用](docs/usage.md) · [源码构建](docs/building.md) · [报告问题](https://github.com/huang200201-dotcom/Bake-Groups-Tool/issues)

## 功能

- **自动分组**：高低模归属匹配、分组策略、章节管理和最终检查。
- **资产任务**：组织批量资产工作，复用现有场景任务流程。
- **烘焙导出**：HP / LP / Cage FBX、独立岛顶点色 ID、ZBrush 高模处理。
- **场景保护**：在导出副本上处理几何；保留原模型及导出前的显隐、选择和平滑预览状态。
- **原生计算**：C++ 点云索引、空间匹配与形状分析，保留现有几何加速。

开源版本无需激活、机器绑定、账号或许可证文件。界面保留“自动分组”和“资产任务”两个页面。默认启用的自动更新从公开 GitHub Releases 获取新版，不需要登录或令牌；可以取消勾选。“访问仓库网站”打开项目仓库首页。

## 安装

支持 **Windows x64 / Maya 2022–2027，使用 Python 3**。请下载 Release 中的 `Bake_Master_1.0.1_Windows_x64.zip`，其中包含六个版本的原生模块。GitHub 自动生成的 Source code 压缩包是开发源码，需要先构建原生模块。

1. 完整解压安装包。
2. 如果本次 Maya 会话打开过旧插件，先重启 Maya，暂时不要打开插件。
3. 将解压目录内的 `安装到Maya.py` 拖入 Maya 视口，按提示安装。
4. 点击当前工具架的 **BAKE MASTER** 按钮。

安装器会校验文件摘要，完整替换 `scripts/Bake_Groups` 插件目录，并在文件替换失败时恢复原目录。它不按版本数字判断升级，因此可以从旧商业版本迁移到开源版本。安装目录内自行修改的代码应事先单独保存。

## 更新

**1.0.0 没有内置更新器，需要按上述步骤手动安装一次 1.0.1。** 此后，“自动更新”默认勾选，可在插件空闲时下载并应用新版；“手动更新”可以手动检查、下载和应用。

Python 文件变化可在空闲时热重载。如果新版需要替换已加载的原生模块，更新会暂存并提示重启；重启 Maya 后打开插件时自动应用。更新不会为此自动重启 Maya。断网或检查失败不影响使用已安装版本。源码检出目录不自动更新，以免覆盖开发修改；安装包内的个人修改也应另行备份。

## 仓库结构

```text
src/Bake_Groups/   单一 Python 源码、界面资源与本地化
native/           C++ 几何模块源码
installer/        Maya 拖放安装入口
tools/            无密钥构建与打包工具
tests/            单元、原生数学及 Maya 集成回归
docs/             使用、构建与维护说明
.github/          持续集成和反馈模板
```

`build/` 和 `dist/` 是本机构建产物，不提交到源码仓库。每个正式版本对应一个 Git 标签与一个 Release；安装包放入相应 Release，不在源码目录堆放历史版本或 ZIP。

## 开发

```powershell
python -m unittest discover -s tests -p "test_*.py" -q
```

普通 Python 会跳过需要 Maya 或指定原生模块的测试。原生 ABI 对照、Maya 集成测试和打包方法见 [构建说明](docs/building.md)。

## 开源与致谢

本项目基于 [Veteraros AI / Bake Groups Tool](https://github.com/veteraros-ai/Bake-Groups-Tool) 演进，按 **GNU GPL v3** 发布，保留上游署名与许可证。Bake Master 的改动包括中文工作流、分组与导出修复、场景状态保护及开源版整理。

完整条款见 [LICENSE](LICENSE)，第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。Autodesk Maya、其 SDK 和 Qt / PySide 由各自权利人提供，本仓库不分发这些产品。

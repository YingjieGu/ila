# ILA 开发任务

## 任务描述
测试: SKILL.md 加一行注释

## 变更清单
- [feature] 测试: SKILL.md 加一行注释 (文件: launcher.py, launcher_platform.py, cli.py, __init__.py, launcher_manager.py)

## 测试要求
- 功能: 对象能正常加载和调用

## 约束
1. 只修改任务相关的文件，不碰触其他文件
2. 遵循现有代码风格
3. 为新增功能编写测试
4. 完成后检查代码完整性（不要运行测试命令）
5. 不要修改 .git 目录
6. **【验证标记】修改 HTML 元素时必须加 data-verification-modified="true" 属性**:
   - 每次修改或新增 dashboard.html 中的 HTML 板块、控件、section 时, 在对应元素上添加 `data-verification-modified="true"` 属性
   - 例: `<div class="section" data-module="dashboard" data-verification-modified="true">`
   - 不要删除已有的 data-verification-modified 属性
   - 这个属性用于部署验证阶段自动高亮新修改的内容

## 当前版本
1.5.0

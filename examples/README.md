# 原创示例

在项目根目录安装基础依赖后运行 `python examples/create_demo.py`。它在 `origin/demo.pdf` 生成三页文档，包含连续段落、一个合并表格、科学计数法、数字分数、字母脚标和两个印刷卷标题。

使用 `python -m document_converter origin/demo.pdf -o output/demo.md` 转换，随后执行根 README 中的独立检查命令。生成器不下载外部材料，默认拒绝覆盖现有同名输入；`--overwrite` 用于明确重新生成。

所有文字和排版由脚本创建，数值仅展示格式。用户实际文档及所有运行输出均留在 Git 忽略目录。

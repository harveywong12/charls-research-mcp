# 验收方法与范围

当前实现：`2026-10-11.research-2`。2026-10-11 完成功能验收，结果汇总见 `validation-summary.json`；真实资料的期望文件、锚点、完整报告和客户端日志仅保存在本机私有目录。

## 本次结果

| 检查 | 结果 |
|---|---|
| 自动化合成测试 | 111 项通过 |
| MCP stdio，research / compat | 全部 13 / 17 工具，34 / 38 次调用通过 |
| legacy stdio 三种启动方式 | 117 次调用通过 |
| 四种客户端模板 | 工具发现、概览与允许列表通过 |
| Positron Zoo Code 独立任务 | 10 次实际 MCP 调用成功 |
| 真实资料专项核查 | 372 项检查、40 次调用通过；90 条事实核对变量、来源组、哈希与精确锚点 |
| 独立元数据复核 | 127 个文件、54,109 个变量名及标签，0 项不一致 |

测试覆盖缩写边界、严格参数、证据状态、编号随年度变化、比较证据缺口、批量共同年份、分页完整性、过期游标、文档隔离、旧库只读与新增目录库发布。Zoo 实测覆盖概览、变量搜索、证据读取、跨年比较、批量查询续页、文档检索和实际定位读取。

修复了旧测试对默认子串误匹配的预期，补充 research-2 合成回归测试，并修正验收脚本的显式 profile、目录库参数和结果变量命名。功能验收期间没有更改服务的检索实现或 NAS 资料。

## 结果边界

这些结果验证所列软件行为、元数据一致性与来源定位，不证明所有科学定义正确或跨年可直接合并。两份 pilot 文件的编码解析限制仍保留为未确认；文档证据、推断、未确认及冲突分别呈现。真实事实经过逐项来源复核，本次未进行新的独立人工科学裁决。

文档定位读取按字符窗口截断；需要命中附近的上下文时传入命中的定位与字符偏移，或继续读取。共同候选年份不证明共同受访者样本；同一构念不证明编码、问法、采样或单位等价。当前批量查询仍返回 `analysis_ready=false`，跨年等价仍须单独核实。

## 复现与发布

```sh
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_client_configs.py
.venv/bin/python scripts/research_smoke.py --catalog /absolute/private/research-catalog.sqlite --profile research
.venv/bin/python scripts/verify_research_metadata.py --catalog /absolute/private/research-catalog.sqlite
```

真实资料验收的期望须按实际资料版本独立固定在本地；不把旧 build 的 P@k、MRR 或盲评分数套用到新版本。公开测试使用合成内容。

发布清单独立审核；原始资料、派生事实、索引和本机配置不上传。新仓库只含代码及原创说明，Git 历史独立建立，不继承旧仓库历史；旧仓库不在本次发布中改写。

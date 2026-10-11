# CHARLS Research MCP

本地、只读的 CHARLS 元数据与文档检索服务，帮助 agent 查变量、核对年度测量口径并建立研究可行性证据表。源码使用 MIT License；第三方数据、文档、全文、派生事实和真实资料验收索引留在本地。见 [资料边界](docs/DATA-RIGHTS.md)。

## 三种入口

| profile | 工具数 | 用途 |
|---|---:|---|
| `research` | 13 | 默认研究入口，含批量研究可行性；只需研究索引 |
| `compat` | 17 | 研究入口与旧版 Harmonized D 四工具 |
| `legacy` | 4 | 原 Harmonized D 服务 |

使用研究索引时默认选择 `research`。旧工具保留于兼容入口；需要旧版构建原文时选择 `compat`。

## 本地安装与启动

需要 Python 3.11–3.14、uv、SQLite FTS5。运行依赖由 `uv.lock` 锁定。无需模型 API key、向量数据库或 GPU。

```sh
UV_CACHE_DIR="$PWD/.cache/uv" uv sync --frozen
.venv/bin/python -m charls_codebook_mcp.server \
  --profile research \
  --catalog /absolute/private/research-catalog.sqlite \
  --aliases /absolute/project/config/research-aliases.json
```

研究索引须由自己有权使用的本地资料构建。构建依赖单独安装，运行服务不导入 Stata/PDF 解析器：

```sh
uv pip install --python .venv/bin/python -r requirements-catalog.txt
.venv/bin/charls-codebook catalog-build \
  --data-root /absolute/private/CHARLS-data-and-codebooks \
  --output /absolute/private/research-catalog.sqlite
```

目录需要原始下载 manifest；补充文档用 `--supplement-manifest`、`--review-record`，核实事实用 `--evidence-file`。构建器检查来源和锚点，但不会自动证明科学释义正确。旧 Word 文档的本地提取目前需要 macOS `textutil`。

旧 D 库：使用已有真实 PDF，通过 `charls-codebook fetch --import-pdf /absolute/private/Harmonized_CHARLS_D.pdf` 导入，再运行 `charls-codebook build`。兼容服务同时传 `--db` 和 `--catalog`；旧版服务传 `--profile legacy --db ...`。

## 接入 Zoo Code 等客户端

```sh
.venv/bin/python scripts/configure_clients.py \
  --profile research --catalog /absolute/private/research-catalog.sqlite
```

将生成模板中的 `charls_codebook` 条目合并到客户端设置并重新加载。模板含本机绝对路径，位于被 Git 忽略的 `config/generated/`。工具允许列表来自 `registry.py` 的同一 profile；`scripts/check_client_configs.py` 可在需要验收时检查配置。

旧的 `scripts/configure_codex.py` 入口转到同一模板生成器，也接受 `--profile` 与 `--catalog`，不再另行维护或写入项目客户端设置。

## 研究使用流程

1. `get_research_overview` 查看模块、版本和逐字段证据覆盖。
2. `assess_research_feasibility` 批量查暴露、结局、协变量及指定年份。
3. 用 `get_data_variable`、`get_variable_evidence` 核对实际变量、编码和原文定位。
4. 用 `compare_variables` 查问法、单位、回忆期、构建等字段的差异与证据缺口。
5. 需要补证时用 `search_all_codebooks` 与 `read_codebook_page` 阅读完整原文。

未命中不证明未收集；同名、同单位和共同年份不证明跨年等价或样本重叠。样本量、缺失、失访及 ID 合并质量须从实际数据计算。本 MCP 不包含受试者观测行。

详细工具、分页、证据文件和迁移说明见 [RESEARCH](docs/RESEARCH.md)。检查方法与当前验收范围见 [VALIDATION](docs/VALIDATION.md)。

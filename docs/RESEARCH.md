# 研究服务使用说明

## 工具目录

默认 `research` profile 提供 13 个工具：

| 工具 | 用途 |
|---|---|
| get_research_overview | 年度、模块、build、检索版本、逐字段证据覆盖 |
| list_indicator_topics | 主题候选分布；不是确认的测量覆盖 |
| list_datasets | 实际文件及元数据出处 |
| list_dataset_variables | 单文件变量，brief/standard/full，name/file 列序 |
| search_data_variables | 变量名、标签、编码、来源清单与核实事实检索 |
| get_data_variable | 单变量及原始 Stata 值标签 |
| find_indicator_across_years | 各年候选、核实状态与文档定位 |
| search_all_codebooks | 问卷、codebook、补充文档检索 |
| read_codebook_page | PDF 物理页或 Word 文本分段无损续读 |
| list_research_documents | 文档、来源与隔离状态目录 |
| get_variable_evidence | 核实事实、逐字段状态、精确变量名引用 |
| compare_variables | 字段级一致、差异、冲突或证据不足 |
| assess_research_feasibility | 多指标、多年份可行性证据清单 |

`compat` 加入 get_document_info/search_codebook/get_variable/read_source，合计17个；这四个旧工具仅指向 Harmonized D。`legacy` 只提供旧四工具。research 启动不再要求旧 D 数据库。

## 精简返回与续查

`search_data_variables`、`search_all_codebooks` 支持 `fields=brief|standard|full`，默认 standard。standard/brief 将数据集或文档的路径、哈希、URL集中于 `data.datasets` / `data.documents`；逐项 citation 指向对应 ID。full 保留逐项完整出处。

检索及批量清单返回 `returned_count`、`requested_limit`、`has_more`、`truncation_reason`；字节截断时游标按实际条数推进。继续时保留所有原参数并传 `next_cursor`。JSON envelope 的预算为12,000字节；SDK可能重复 text/structured 编码，因此该预算不等于协议报文字节上限。单项无法装入时保留一项以保证进度并标 `oversize_item`。

游标绑定数据 build、检索实现版本、别名文件指纹和全部查询参数。升级服务后应从第一页重新检索，旧游标会返回 stale_cursor。

## 检索控制

- `match_mode=auto`：短大写缩写及已知缩写按完整词元/变量名组件匹配；其他检索词保留子串和词干召回。
- `match_mode=exact`：各检索词按完整词元/短语匹配，不做宽泛子串兜底。
- `match_mode=broad`：恢复宽泛子串检索，用于排查漏检。匹配形态不证明科学相关性。
- `expand=false` 禁止别名展开；概念词默认展开；英文成员默认保持字面，可显式 `expand=true`。
- 变量检索支持 year/namespace/group/module；文档检索支持 group/source_kind/module/year。
- 文档 year 指来源目录年度，不代表一份跨年文档只测量该年；Harmonized 文档请用 group 查询。module 过滤依赖同页精确变量名引用，可能漏掉没有变量名的方法页。
- 文档排序保留前5条字面优先，其余与扩展页混排。核实事实优先于同层普通变量候选，但不自动选定分析变量。

## 逐字段证据

字段：construct、measurement_type、unit、coding、recall_period、eligible_population、construction、timing、sampling。

`get_variable_evidence` 的逐字段记录集中在 `data.field_evidence`；reviewed_fact 与 definition_summary 不重复嵌入同一字段表。

| status | 含义 |
|---|---|
| document_explicit | 来源直接支持该字段；须引用 source_indexes 对应锚点 |
| inferred | 有根据的解释或归类，仍需核对 |
| unconfirmed | 没有足够逐字段证据，保留空值或旧记录原值 |
| conflict | 至少两个来源有冲突，禁止静默选择或转换 |

旧版23条事实仍保留原记录；未作逐字段复核的属性不会自动升级为 document_explicit。verified_local 只表示本地文件哈希核对，并非整份文档或全部科学定义已核实。自动来源验证只核对实际变量、年度/模块、文档信任状态、唯一字符锚点和字段出处完整性。

新增证据文件使用 schema_version=2。每条事实沿用 year/module/namespace/variable_name/construct/construct_key/search_terms/review_status/sources，新增 evidence_fields。字段内包含 value/status/source_indexes/reason。sources 包含 document_sha256/page/唯一 anchor。源文档派生事实只保存在私有目录；公开示例使用虚构变量和文档。

格式示例在 `config/evidence.example.json`；不可直接导入真实目录库。

```sh
.venv/bin/charls-codebook catalog-enrich \
  --catalog /absolute/private/current.sqlite \
  --evidence-file /absolute/private/additional-evidence.json \
  --output /absolute/private/next.sqlite
```

该命令复制当前目录库，追加事实并发布新的 build；不读观测行、不改NAS、不覆盖活跃库。重复事实会失败，避免覆盖已有审核。新库部署时显式更新 --catalog；回滚只需恢复旧配置。

## 批量研究可行性

```json
{
  "indicators": [
    {"query": "抑郁", "role": "exposure"},
    {"query": "认知", "role": "outcome"},
    {"query": "教育", "role": "covariate"}
  ],
  "years": [2011, 2013, 2015, 2018, 2020],
  "namespace": "raw",
  "limit": 2
}
```

一次接受1–12个指标；每页1–3个指标。共同年份始终根据全部输入计算，不仅根据当前返回页。返回 confirmed_present/candidate_only/no_index_match 与各字段缺口；confirmed_present 仅确认选出条目的实际变量及构念，不表示整个概念或量表完整。只展示有限候选，需研究者选择；若 more_candidates=true，继续按年查 search_data_variables。

共同年份不能替代受访者重叠、时间顺序、权重、代答、人群与实际合并检查。analysis_ready 保持 false，因为该服务不进行观测数据分析。compare_variables 的 documented_agreement 只表示对应字段一致；仍不自动认定完整跨年等价或执行单位转换。

旧版公共 config/aliases.json 只包含原创检索词，未核实别名会记录为 unreviewed；私有逐页核实词典可用 build --alias-file /absolute/private/aliases.json 导入。现有旧D数据库中的审核与锚点保持原记录。

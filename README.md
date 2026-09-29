# 🔎 OpenAPI 版本兼容性审查工具

对比新旧两份 **OpenAPI 3.x**（JSON / YAML）文档，递归解析 `$ref`（**支持循环引用**）、合并 `allOf`，按**请求 / 响应两个方向**的语义规则判断每一处变更是否向后兼容，精确定位破坏性变更的 JSON 路径并给出原因；并按 OpenAPI 语义计算每个操作的**有效认证要求**（操作级 `security` 覆盖根级），识别认证收紧（缺少方案 / OAuth scope 变多 / 匿名变需认证）类破坏。支持结果筛选、**根因聚合视图**（有向影响组）、引用展开树浏览、审查报告下载、**单条发现的最小复现包导出**、历史记录与**破坏项对账**（基线审查 vs 当前审查）、**限时豁免**（按 API 标识为破坏项设置带 UTC 到期时间的豁免，"未豁免为零才可放行"）、**组件可达性审计**（从 paths / webhooks 入口沿本地 `$ref` 传递追踪，列出未被入口触达的组件并标出新版独有的未使用组件）、**调用清单影响定位**（在审查详情导入 `method,path,count` 的 CSV，按旧版 paths 匹配每行调用、按操作汇总调用量并关联破坏性发现；歧义不分配、结果仅本次查看），以及**请求样本回放**（提交多条含旧版操作方法 / 路径模板 / Content-Type / JSON 请求体的样本，逐条返回旧版与新版请求体校验状态、失败字段路径与"旧版接受而新版拒绝"的风险汇总；结果仅本次查看），以及 **Link 后续调用参数预览**（在审查详情选择旧 / 新版与源操作，提交一次响应实例：状态码 + JSON 响应体 + 响应头；响应按精确码 → 范围码 → default 匹配，逐条 Link 显示指针、目标方法 / 路径与每个后续调用参数的解析值与缺失 / 未知 / 表达式错误；仅静态预览，不发请求、不入库、不改结论）。

技术栈：**FastAPI + SQLite + 原生 JavaScript**（前端零构建、零框架依赖）。

---

## 🚀 一条命令启动

需要安装 Docker 与 Docker Compose v2：

```bash
docker compose up -d --build
```

启动后访问：

> **http://localhost:8000**

| 入口 | 地址 / 端口 |
| --- | --- |
| 审查界面 | http://localhost:8000 （宿主机 **8000** 端口） |
| 交互式 API 文档（Swagger UI） | http://localhost:8000/docs |
| 健康检查（Docker HEALTHCHECK 探测） | `GET http://localhost:8000/api/health` |
| 容器内监听 | `0.0.0.0:8000`（由 `docker-compose.yml` 的 `"8000:8000"` 映射到宿主机 8000） |

端口说明：容器内监听 `8000`，通过 `docker-compose.yml` 映射到宿主机 **8000**；审查数据保存在 Docker 卷 `review-data`（容器内 `/data/reviews.db`），重启不丢失。镜像内置 `HEALTHCHECK`，周期性探测 `GET /api/health`，可通过 `docker ps` 查看健康状态。

停止 / 更新：

```bash
docker compose down          # 停止（保留数据卷）
docker compose up -d --build # 修改代码后重新构建
```

如需换端口，修改 `docker-compose.yml` 中 `"8000:8000"` 的左侧宿主机端口即可。

### 本地直接运行（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 🧭 使用方式

1. 打开 http://localhost:8000 ，在左右两栏**粘贴**或点击「上传」选择旧版 / 新版 OpenAPI 文件（`.json` / `.yaml` / `.yml` 均可，也可混用）。
2. （可选）在「开始审查」旁的输入框填写 **API 标识**——用于限时豁免匹配；留空则该审查不应用豁免。
2. 可先点击内置示例：**典型破坏性变更 / 循环 $ref / 纯兼容新增 / 枚举约束收窄 / 认证要求收紧**。
3. 点击「开始审查 →」。
4. 结果区提供：
   - 顶部统计卡片与总体结论（✅ 向后兼容 / ❌ N 项破坏性变更）；
   - **限时豁免条**：API 标识设置、已豁免 / 未豁免破坏项数量与"未豁免为零才可放行"结论（详见下文「🛡️ 限时豁免」）；
   - **组件可达性审计**：旧版 / 新版两栏列出未被 paths / webhooks 入口触达的组件（类型 / 名称 / 数量），新版独有的未使用组件带徽标，点击条目定位到对应版本的引用树（详见下文「🧭 组件可达性审计」）；
   - **调用清单影响定位**：导入 `method,path,count` 的 CSV，把真实/预期调用量匹配到旧版操作并关联该操作的破坏性发现（详见下文「📊 调用清单影响定位」）；
   - **请求样本回放**：提交多条请求样本（旧版操作方法 + 路径模板 + Content-Type + JSON 请求体），逐条查看旧版 / 新版请求体校验状态与失败字段路径，汇总"旧版接受而新版拒绝"风险（详见下文「🧪 请求样本回放」）；
   - **Link 后续调用参数预览**：选择旧 / 新版与源操作，提交状态码、JSON 响应体与响应头，逐条预览响应中 OpenAPI Link 指向的下一跳调用参数（方法 / 路径 / 每个参数的取值、请求体、缺失必填与表达式错误；详见下文「🔗 Link 后续调用参数预览」）；
   - **逐条发现 / 根因聚合** 视图切换：聚合视图把同根因的发现折叠为有向影响组（详见下文「🧩 根因聚合视图」）；
   - **级别筛选**：🔴 破坏性 / 🟢 兼容 / 🟡 废弃提示 / ⚪ 信息；
   - **方向筛选**（请求 / 响应 / 结构性）、**分类筛选**、关键字搜索（两种视图均生效）；
   - 每条发现可展开：人类可读定位、旧/新版 JSON Pointer、`$ref` 锚点、旧值 / 新值、规则编号；破坏项另显示**豁免匹配状态、理由与到期时间**，并可就地创建 / 撤销豁免；
   - 点击发现项上的 **🌲旧 / 🌲新** 按钮，右侧「引用展开树」自动切到对应版本并展开到该 `$ref`；
   - 每条发现可点击 **📦 导出最小复现包**，下载仅含该发现现场的最小文档对（ZIP，详见下文）；
   - 「⬇ 下载 Markdown / JSON 报告」获取可归档的审查报告（含豁免匹配状态、理由与到期时间）。
5. 「历史记录」标签页可重新打开、下载或删除保存在 SQLite 中的历史审查；并支持**破坏项对账**：选一条已保存审查作基线，与当前打开的审查比较，输出新增 / 延续 / 已消失三类破坏项（详见下文「⚖️ 破坏项对账」）。

---

## 🧠 判定原理

同一类 schema 变化在两个方向上结论可能相反，工具按方向独立判定：

| 视角 | 核心原则 |
| --- | --- |
| **请求方向**（参数、requestBody） | 客户端原本合法发送的内容**被收紧**即为破坏：必填变多、枚举变少、范围收窄、类型收窄等 |
| **响应方向**（responses） | 客户端原本基于契约的预期**被改变**即为破坏：字段消失、枚举变多、承诺范围扩大、变 nullable、字段变 writeOnly 等 |

部分规则示例：

| 变更 | 请求方向 | 响应方向 |
| --- | :---: | :---: |
| 新增必填字段 | 🔴 破坏 | 🟢 兼容（保证更多数据） |
| 新增可选字段 | 🟢 兼容 | 🟢 兼容（严格模型需确认） |
| 删除字段 | 🟢 兼容* | 🔴 破坏 |
| 可选 → 必填 | 🔴 破坏 | 🟢 兼容 |
| 必填 → 可选 | 🟢 兼容 | 🔴 破坏（字段可能消失） |
| 枚举取值增加 | 🟢 兼容 | 🔴 破坏 |
| 枚举取值减少 | 🔴 破坏 | 🟢 兼容 |
| `minimum` 升高 / `maximum` 降低 | 🔴 破坏（收紧） | 🟢 兼容 |
| `minimum` 降低 / `maximum` 升高 | 🟢 兼容 | 🔴 破坏（承诺漂移） |
| 变 `nullable` | 🟢 兼容 | 🔴 破坏 |
| 字段变 `readOnly` | 🔴 破坏 | 🟢 兼容 |
| 字段变 `writeOnly` | 🟢 兼容 | 🔴 破坏 |
| `additionalProperties: true→false` | 🔴 破坏 | 🟢 兼容 |

\* 请求字段从契约移除时服务端可能开始忽略/拒绝，工具以兼容记录并在原因中注明。

结构层面还覆盖：路径与操作的新增/删除、pathItem 级与操作级参数合并比较、参数必填性 / `style` / `explode` / `deprecated`、请求体必填性变化、媒体类型增删（请求 415 / 响应 Content-Type）、响应状态码增删、`servers` 增删、`operationId` 变化、`oneOf/anyOf` 分支增删等。

### 🔐 认证要求（security）兼容性

对**两版都存在的 paths 操作**（新增 / 删除操作另由 `operation-added` / `operation-removed` 覆盖），按 OpenAPI 语义先计算每侧的**有效认证要求**，再做组合级比较：

- **有效要求**：操作显式声明 `security` 时以**操作级覆盖根级**（哪怕是空数组，也不回落根级）；操作缺省时继承根级 `security`；根级与操作级都缺省视为**允许匿名**。`security` 缺省、空数组 `[]` 与 `[{}]` 都表示允许匿名，三者等价不产生发现。
- **组合语义**：`security` 数组中的对象之间是**「或」**（满足任一即可调用），同一对象内的多个方案是**「且」**（必须同时具备），OAuth2 / openIdConnect 的 **scope 集合须全部满足**。
- **兼容性判定**：对旧版每一个可用认证组合，若在新版存在一个**要求不更严**的组合可被该组合满足（同名方案具备、且新版要求的 scope 都被旧凭据授予的 scope 覆盖；新版组合允许匿名则接受一切），该旧组合即继续可用；
  - 旧版**任一**可用组合在新版找不到这样的组合 → 🔴 破坏（`security-requirements-tightened`）：新增必填方案、追加 OAuth scope、匿名变为需要认证等，持旧凭据的调用方可能收到 401/403；
  - 否则有效要求确有变化（新增可选认证方式、scope 收窄、改为允许匿名、去掉「且」中的方案等）→ 🟢 兼容（`security-requirements-relaxed`）；
  - 规范化后完全相同（如 `[]` ↔ `[{}]`、仅声明位置变化而有效要求不变）不产生发现。
- **定位**：发现标出操作（端点）、双侧 JSON Pointer（操作级为 `/paths{path}/{method}/security`，继承根级为 `/security`，完全缺省侧锚到操作对象）、旧 / 新有效要求（`old_value` / `new_value`：生效来源 `operation` / `root` / `default`、原始 security、规范化后的认证组合）以及每个不可用旧组合的具体缺口（缺少哪个方案 / 哪些 scope）。
- 该类发现归类为 **认证 / 请求方向**，自动接入级别 / 方向 / 分类 / 关键字**筛选**、Markdown / JSON **报告**、破坏项**限时豁免**与**破坏项对账**；最小复现包会保留根级 `security` 以保证可复现。

认证规则示例：

| 旧版有效要求 | 新版有效要求 | 结论 |
| --- | --- | --- |
| 匿名 | `oauth2[read]` | 🔴 旧匿名调用将收到 401/403 |
| `oauth2[read]` | `oauth2[read, write]` | 🔴 追加 OAuth scope |
| `apiKey`（唯一「或」分支） | `oauth2[read]` | 🔴 旧 apiKey 组合无不更严分支 |
| `apiKey` 或 `oauth2[read]` | `oauth2[read]` | 🔴 apiKey 分支消失（oauth 分支仍可用） |
| `apiKey` 且 `oauth2[read]`（同一对象内「且」） | `apiKey` | 🟢 去掉一项合取要求 |
| `oauth2[a,b]` | `oauth2[a]` | 🟢 scope 收窄 |
| `oauth2[read]` | `apiKey` 或 `oauth2[read]` | 🟢 新增可选认证方式 |
| `oauth2[read]` | `[]`（允许匿名） | 🟢 放宽为匿名 |
| `[]` | `[{}]` | — 等价，无发现 |

内置示例「**认证要求收紧**」演示根级继承、操作级覆盖、`[]` 与 `[{}]` 等价、scope 追加、方案替换与新增「或」分支等场景。

> 页面内「规则说明」标签有完整列表；每条发现都带稳定的 `rule_id`（如 `property-became-required`、`enum-value-added`），便于在 CI 中按规则处理。

### `$ref` 与循环引用处理

- 参数、请求体、响应、schema 中的 `$ref` 在比较前先解引用；`allOf` 按合取语义合并（属性深度合并、`required` 取并集、min 取大 / max 取小、枚举取交集）。
- schema 递归比较同时维护 **DFS 活跃栈**与**已比较节点对集合**：遇到 A→B→A 形式的自引用 / 互相引用时立即终止该分支，且同一条差异不会沿环路重复刷屏；循环节点在展开树中以 **⟳ 循环引用** 高亮。
- 兼容 OpenAPI 3.0（`nullable: true`）与 3.1（`type: ["string","null"]`）两种可空写法。

发现项给出**双重定位**：

- `location`：载荷语义路径，如 `requestBody > application/json → 请求体.address.zip`
- `old_json_path` / `new_json_path`：文档内 JSON Pointer（引用组件时锚点指向 `/components/schemas/...`），可直接用于在引用树中跳转。

---

## 🧩 根因聚合视图

逐条发现列表在大型变更中容易"见树不见林"：一次类型整体替换会连带产生多条子字段删除，共享组件被多处引用时同一根因会以多条发现出现。详情页的「**根因聚合**」切换把发现折叠为**有向影响组**，每组一个主因。

**分组规则**（服务端 `GET /api/reviews/{id}/groups` 实时计算，纯函数、无额外存储）：

- **端点 × 方向是硬边界**：同一组件（含循环引用场景）在请求侧与响应侧**各自成组**，绝不跨方向合并；
- **同源归并**：共享 / 循环组件被多处引用时，引擎可能对同一组件指针重复报告同一规则——`rule_id`、JSON Pointer 与 `$ref` 锚点完全一致的发现折叠为同一节点，多余条目记为 **同源**；
- **祖先吸收**：父路径被**整体替换**（`type-changed`、schema 变为布尔等）或**整体删除**（`property-removed`、`response-removed` 等）时，其 JSON Pointer 后代发现只是下游影响，被父发现吸收，记为 **下游影响**（有向边 父 → 子，挂接最近祖先；`parameter-removed` 等容器级指针不参与吸收，避免误并同操作下其他参数的独立发现）。

**每组返回**：

| 字段 | 说明 |
| --- | --- |
| `group_id` | 稳定组标识：端点 / 方向 / 规则 / 锚点指针的 SHA-256 短哈希，同一审查重复查询结果一致 |
| `primary` | 主因发现（完整字段，含 `relation: "primary"`） |
| `affected` | 受影响条目（完整发现字段 + `relation`（`absorbed` / `same_cause`）、`depth`、`absorbed_by`） |
| `locations` | 可展开定位：每个成员的语义路径 + 双侧 JSON Pointer + `$ref` 锚点 |
| `severity` / `member_count` / `breaking_count` | 组级统计（严重度取组内最高） |

聚合视图中主因与受影响条目均可展开定位、跳转引用树（🌲）并导出最小复现包（📦），与逐条视图能力一致；级别 / 方向 / 分类 / 关键字筛选同样生效。

---

## ⚖️ 破坏项对账

「历史记录」页顶部的对账面板用于回答一个问题：**相对上次基线，这次审查的破坏性变更多了哪些、还有哪些没修、哪些已经消失**。

**使用方式**：先在历史记录中「打开」一条审查作为当前审查（或刚执行完一次新审查），再在下拉框选一条已保存审查作基线（也可直接点历史行的「对账」按钮），点击「开始对账」。

**对账原理**（服务端 `GET /api/reviews/{id}/reconcile?baseline_id=N` 实时计算，纯函数、无额外存储）：

- **只对账破坏性发现**（`severity = breaking`）；某条发现若在新审查中降级为兼容/废弃，会体现为"已消失"；
- **稳定身份** = 端点 + 请求/响应方向 + 规则（`rule_id`）+ 语义定位（`location`）——刻意忽略发现项 ID 与列表顺序，同一文档重新审查、序号变化不影响配对；
- **同身份重复发现按出现次数一一配对**（多重集差）：基线 2 条、当前 3 条同身份发现 → 2 对"延续" + 1 条"新增"；
- 每条结果**保留两侧原发现 ID**（`current_finding_id` / `baseline_finding_id`），便于回查定位。

**结果展示**：新增 🆕 / 延续 🔁 / 已消失 ✅ 三列及各自数量。新增与延续项可点击「↗ 当前 #N」跳回当前审查的对应发现（自动切到审查页、清筛选、展开并高亮该卡片）；延续与已消失项可点击「↗ 基线 #N」打开基线审查并定位。

**明确提示**：基线与当前审查相同（HTTP 400）、基线不存在或已删除（HTTP 404）、尚未打开当前审查时，页面都会给出可读提示，不会静默失败。

---

## 🧭 组件可达性审计

审查结果页的「**组件可达性审计**」面板回答另一个问题：**`components` 里声明的组件，有哪些其实从未被 API 入口用到**——它们是可以安全清理的死契约。

**访问方式**：

- **页面**：执行审查（或在「历史记录」中打开一条已保存审查）后，结果页统计卡片下方即显示该面板，旧版 / 新版两栏并列；
- **接口**：`GET /api/reviews/{id}/reachability`，实时计算、纯函数、无额外存储，返回结构与页面一致。

**可达性判定**（对旧版、新版文档分别独立计算）：

- **入口**：`paths` 下每个 path item 与 `webhooks`（OpenAPI 3.1）下每个 webhook；
- **传递追踪**：沿文档内 `$ref` 逐层展开，visited 集合去重——A→B→A 形式的**循环引用只访问一次**，不会无限递归；外部文件引用（`./other.yaml#/...`）不属于本地可达性范围，跳过；
- **security 也算可达**：全局（根级）与操作级 `security` 按名称引用的 `components/securitySchemes` 计入可达（含被 `$ref` 引用的 pathItem 与内联 callback 中的操作级 security）。

**结果内容**：

- 各侧**未被入口触达的组件**：按类型（schemas / parameters / responses / securitySchemes / pathItems 等）分组列出名称与数量，附入口可达组件总数；
- **新版独有**徽标：新版未触达、且旧版文档中不存在同名同类型组件的条目——即新版新引入却从未使用的死组件；
- **无效本地引用**：`$ref` 目标不存在、或 `security` 引用了未声明的 securityScheme 名称时，逐条给出**所在位置、引用值与原因**（不计入可达）；
- 点击任意组件条目，右侧「引用展开树」自动切到对应版本并定位到该组件。

---

## 📊 调用清单影响定位

审查只能告诉你"契约上有哪些破坏"，回答不了"**线上到底有多少调用会被这次变更打到**"。审查详情页的「**调用清单影响定位**」面板接受一份调用清单 CSV，把每行调用匹配到**旧版 paths** 的操作，按操作汇总调用量并关联该操作的破坏性发现，输出一张**按调用量降序排列的影响表**。

### CSV 格式

UTF-8 文本，**首行必须是表头且包含 `method`、`path`、`count` 三列**（列顺序不限，允许额外列）：

```csv
method,path,count
GET,/pets/123,12000
GET,/pets/123?expand=owner,3000
post,/pets,800
GET,/orgs/7/members,150
GET,/pets/42,500
GET,/pets/42,250
```

规则：

- **method**：标准 HTTP 方法，大小写不敏感（`get` 与 `GET` 等价）；
- **path**：真实调用路径，须以 `/` 开头；**查询串不参与匹配**——`/pets/123?expand=owner` 按 `/pets/123` 匹配，页面会标注"查询串已忽略"；
- **count**：调用次数，**须为正整数**（`0`、负数、小数、空值均为非法行）；
- **重复行累加**：同一 (method, path)（忽略查询串）的多行调用量合并（上例中 `GET /pets/42` 合计 750）；
- 非法行（空字段 / 未知方法 / count 非正整数等）**逐行给出原因并跳过**，不影响其他行；文件级问题（缺列、无数据行）整体返回错误。

### 使用方式

1. 执行审查（或在「历史记录」中打开一条已保存审查）；
2. 在结果区「📊 调用清单影响定位」面板点击「**选择 CSV**」，或展开「直接粘贴 CSV 文本」粘贴内容；
3. 点击「**导入并定位**」，页面展示：
   - **汇总卡片**：清单总调用量、已分配调用量（操作数）、歧义未分配、未匹配、非法行；
   - **影响表**（按调用量降序）：旧版操作（方法 + 路径模板）、调用量、匹配到的具体路径明细、该操作的**操作级破坏性发现**与**路径级破坏**（如整条路径被删除），有破坏的行高亮并标 🔴，点击破坏徽标可直接跳到下方对应发现卡片；
   - **歧义行**与**未匹配行**两小节，逐行给出调用量、来源 CSV 行号与具体原因；
   - **非法行**小节：行号、原始字段与错误原因。
4. **导入结果只供本次查看**：实时计算、不入库、不出现在 Markdown / JSON 报告中，**不改变审查结论**；刷新页面或切换审查后需重新导入。

### 匹配原理

（服务端 `app/impact.py`，接口 `POST /api/reviews/{id}/impact`，请求体即 CSV 文本 `Content-Type: text/csv`；纯函数、无存储）

- 旧版每个路径模板按 `/` 切分为段，`{name}` 形式为**参数段**，其余为**静态段**；
- 具体路径与模板**段数相同**、且每段要么与静态段**逐字相等**、要么落在参数段上，才算结构匹配；
- **静态段优先于参数段**：候选按静态段数量排序，静态段越多越具体、越优先。例如旧版同时有 `/orgs/{id}` 与 `/{root}` 时，`GET /orgs/7` 唯一命中 `/orgs/{id}`（2 段中 1 个静态段）而不是 `/{root}`（0 个静态段）；
- **若最优候选仍不唯一**（静态段数量相同、互为歧义，如同时存在 `GET /a/{x}` 与 `GET /{p}/b` 时导入 `GET /a/b`），该行标为 **ambiguous（歧义）**，列出全部并列候选，**不分配调用量**；
- 结构上有模板匹配、但该模板**未定义对应 HTTP 方法**（如旧版只有 `GET /pets/{id}`，导入 `POST /pets/1`）归入**未匹配**并给出与"无任何模板匹配"不同的原因；
- 唯一匹配后，调用量按**操作**（方法 + 模板）汇总；同一操作可吸收多个具体路径的调用量。破坏性发现按 `GET /pets/{id}` 形式的端点关联到操作，按模板本身（如 `path-removed`）关联为路径级破坏。

---

## 🧪 请求样本回放

审查只能从**契约结构**上推断收紧点，请求样本回放回答更直接的问题：**线上真实的请求体，旧版契约接受的，新版契约会拒绝吗？** 在审查详情页的「🧪 请求样本回放」面板提交多条请求样本，后端用两侧文档对同一份 JSON 请求体分别做 schema 校验，逐条给出旧版 / 新版校验状态与失败字段路径，并汇总「旧版接受而新版拒绝」的风险样本。

### 样本格式

UTF-8 JSON 数组（可上传 `.json` 文件或直接粘贴），每条样本包含：

| 字段 | 说明 |
| --- | --- |
| `method` | **旧版**操作的 HTTP 方法，大小写不敏感（如 `post` / `POST`） |
| `path` | **旧版**操作的路径模板（如 `/pets/{id}`，按模板精确匹配，不是具体路径） |
| `content_type` | 请求的 Content-Type，如 `application/json`；自动忽略 `; charset=...` 等参数与大小写差异 |
| `body` | JSON 请求体：可直接写 JSON 值（对象 / 数组 / 标量），也可给 JSON 文本字符串 |

```json
[
  {
    "method": "POST",
    "path": "/pets",
    "content_type": "application/json",
    "body": { "name": "Rex", "kind": "cat", "tags": ["cute"] }
  },
  {
    "method": "POST",
    "path": "/pets",
    "content_type": "application/json; charset=utf-8",
    "body": "{\"name\": \"Kit\", \"kind\": \"dog\"}"
  }
]
```

### 校验与风险规则

（服务端 `app/samplecheck.py`，接口 `POST /api/reviews/{id}/sample-check`；纯函数、无存储）

- **操作定位**：新版按**相同方法与路径模板**查找操作——路径模板精确匹配（不做具体路径→模板匹配）；**新版操作消失（路径或方法不存在）视为新版拒绝**，requestBody 消失或 **Content-Type 对应媒体类型消失（415）同样视为新版拒绝**，失败原因分别标注 `path-removed` / `operation-removed` / `request-body` / `media-type`。
- **覆盖的校验关键字**：`type`、`required`、`enum`、`properties`、`items`、`allOf` 与**本地 `$ref`**（兼容 OpenAPI 3.0 `nullable: true` 与 3.1 `type: ["…","null"]`；3.1 中 `$ref` 的兄弟字段同样生效）。**循环引用沿引用链安全终止**：重复进入的 `$ref` 分支视为通过，不会无限递归；另有深度 / 步骤预算兜底。
- **单侧三种状态**：`accepted`（覆盖范围内约束全部满足）/ `rejected`（存在硬违反，带失败字段路径与规则，如 `$.address.zip` 的 `type`、`$.age` 的 `required`、`$.tags[0]` 的 `enum`）/ `undeterminable`。
- **无法判定（不计风险）**：遇到**无法解析的 `$ref`**，或实例可能被**覆盖范围外的约束**拒绝时（如 `minimum/maximum`、`minLength/maxLength/pattern/format`、`minItems/maxItems/uniqueItems`、`minProperties/maxProperties`、`additionalProperties: false`、`oneOf/anyOf/not`、`if/then/else` 等）——范围外约束**只有在当前实例上实际构成约束或可能被违反时**才标记，避免"假通过"漏报。
- **风险口径**：仅 **旧版 `accepted` 且新版 `rejected`** 计入风险汇总。**旧版自身拒绝的样本不计风险**（新老都拒绝说明请求本就不合法）；任一侧 `undeterminable` 也不计风险。
- **逐条报错（不影响其他样本）**：旧版操作不存在、旧版路径模板不存在、方法非法、路径不以 `/` 开头、缺少 Content-Type、请求体为空 / JSON 格式错误、样本不是对象等，逐条在结果中标注错误原因并跳过双侧校验；文件级问题（`samples` 不是数组、为空、超过 200 条）返回 HTTP 400。

### 结果展示

- **汇总卡片**：🔴 风险条数（旧版接受而新版拒绝）、已完成双侧校验数、双侧均接受、无法判定（不计风险）、旧版拒绝（不计风险）、样本错误数；
- **🔴 风险汇总表**：每条风险的样本序号、旧版操作（方法 + 路径模板）、Content-Type 与新版拒绝原因（含失败字段路径）；
- **逐条明细**：每行可展开查看旧版 / 新版各自的校验状态徽标、失败字段路径列表（路径 + 规则 + 人类可读原因）与无法判定项；风险行高亮。
- **只供本次查看**：实时计算、不入库、不出现在 Markdown / JSON 报告中，**不改变原审查结论**；刷新页面或切换审查后需重新提交。

### HTTP API 调用示例

`POST /api/reviews/{id}/sample-check`，请求体为 JSON：

```bash
curl -X POST http://localhost:8000/api/reviews/1/sample-check \
  -H "Content-Type: application/json" \
  -d '{
    "samples": [
      {
        "method": "POST",
        "path": "/pets",
        "content_type": "application/json",
        "body": { "name": "Rex", "kind": "cat" }
      }
    ]
  }'
```

响应（节选）：

```json
{
  "review_id": 1,
  "summary": {
    "total": 1, "errors": 0, "evaluated": 1,
    "risks": 1, "old_rejected": 0, "undeterminable": 0, "both_accepted": 0
  },
  "risks": [
    {
      "index": 1,
      "endpoint": "POST /pets",
      "method": "POST", "path": "/pets",
      "content_type": "application/json",
      "new_failures": [
        { "path": "$.kind", "rule": "enum",
          "message": "值 \"cat\" 不在枚举允许的取值中。" }
      ]
    }
  ],
  "samples": [
    {
      "index": 1, "endpoint": "POST /pets", "error": null, "risk": true,
      "old": { "status": "accepted", "failures": [], "unknowns": [] },
      "new": { "status": "rejected",
               "failures": [ { "path": "$.kind", "rule": "enum", "message": "…" } ],
               "unknowns": [] }
    }
  ],
  "note": "校验结果只供本次查看，不改变原审查结论。"
}
```

样本级错误出现在对应条目的 `error` 字段（如 `"旧版不存在操作 DELETE /pets。"`、`"JSON 请求体格式错误：…"`），该条不进入双侧校验；文件级错误返回 HTTP 400（如 `samples 为空：至少提交一条请求样本。`）。

---

## 🔗 Link 后续调用参数预览

OpenAPI 的 **Link 对象**描述"收到这个响应之后，客户端接下来可以怎么调"——但 Link 的参数值是**运行时表达式**（如 `$response.body#/id`），契约里看不到真正会传什么。「🔗 Link 后续调用参数预览」面板用一次**响应实例**（状态码 + JSON 响应体 + 响应头）把这些表达式当场求值，逐条展示每个 Link 的目标操作与构造出的后续调用参数。**整个过程只做静态预览：不发送任何请求、不入库、不出现在报告中、不改变审查结论。**

### 提交内容

在审查详情的面板中选择 / 填写：

| 字段 | 说明 |
| --- | --- |
| 版本 | **旧版**或**新版**文档（Link 在响应里，因此预览始终在同一侧文档内完成源 → 目标解析） |
| 方法 / 路径模板 | **源操作**：HTTP 方法（大小写不敏感）+ 路径模板（如 `POST /pets`，精确匹配） |
| 状态码 | 响应实例的状态码（100–599 整数；YAML 里写成整数的响应键也按字符串匹配） |
| JSON 响应体 | 任意 JSON 值（也接受 JSON 文本字符串），供 `$response.body#…` 取值 |
| 响应头 | JSON 对象（头名称 → 值），供 `$response.header.…` 取值 |

### 响应匹配顺序

对源操作的 `responses` 依次尝试：**精确码（`201`）→ 范围码（`1XX`…`5XX`，如 `4XX`）→ `default`**，三者命中第一个即停止（`4XX` 不会去匹配 5xx 状态码）；都没有则返回 HTTP 400 并列出已声明的响应键。结果中返回命中的键、匹配方式（`exact` / `range` / `default`）与响应对象的 JSON Pointer（含解引用后的指针与声明位置指针）。

### Link 与目标解析

- 逐条遍历匹配响应的 `links`（保持文档顺序）。Link 可以内联，也可以是指向 `components/links` 的**本地 `$ref`**；
- 响应、Link、参数上的**本地 `$ref` 链都做循环检测**：A→B→A 安全终止并对该条报 `circular-ref`，不会无限递归；**外部引用**（`http://…`、`./other.yaml#…`、裸 `foo.json` 等）一律报 `external-ref` 不做解析；指针无法解析报 `unresolved-ref`；
- 目标二选一（同时给出报 `operation-both-id-and-ref`，都没有报 `link-no-target`）：
  - **`operationId`**：在 `paths` 与 `webhooks` 的全部操作中索引；**必须唯一**——找不到报 `operation-id-not-found`，命中多个报 `operation-id-ambiguous`（列出全部候选）；
  - **`operationRef`**：只接受本文档内 `#/…` JSON Pointer（外部引用报错），且落点必须是操作对象（末段为 HTTP 方法且含 `responses`），否则报 `operation-ref-not-operation` / `operation-ref-unresolved`；支持指向 `paths` 或 `webhooks` 下的操作。

### 参数值与合并规则

- 目标参数 = **路径级 `parameters` 与操作级 `parameters` 按 `(in, name)` 合并，操作级覆盖路径级**（输出中以 `level: path | operation` 标明来源）；`path` 参数恒为必填；参数本身的本地 `$ref` 同样循环安全、外部引用报错；
- Link `parameters` 的**键**：
  - `位置.名称` 形式（如 `path.petId`、`header.X-Trace`）直接定位；位置非 `path/query/header/cookie` 报 `unknown-parameter`；
  - **裸名**（如 `petId`）按名称在目标参数中匹配：**裸名必须唯一**，同名列于多个位置时报 `ambiguous-parameter`（提示改用 `位置.名称`），目标没有该参数报 `unknown-parameter`；
- **值**支持三类：
  1. **JSON 常量**（数字 / 字符串 / 布尔 / 对象 / 数组，以及 **`null`——null 是合法常量、算"已提供"**，不会被当成缺失）；
  2. **`$response.body#/JSONPointer`**：在提交的响应体上按 JSON Pointer 取值（支持数组下标；token 按 RFC 6901 解码 **`~1`→`/`、`~0`→`~`**）；指针取不到（键不存在 / 下标越界等）报 `expression-not-resolved`；
  3. **`$response.header.名称`**：在提交的响应头中查找，**头名称忽略大小写**（`$response.header.x-request-id` 可命中 `X-Request-Id`）；响应实例没有该头报 `header-not-found`，按字符串原值给出；
  - 其余运行时表达式（`$request.*`、`$method`、`$statusCode`、`$response.body`（不带 `#`）、`$foo` 等）一律报 **`unsupported-expression`**；
  - 字符串以 `$` 开头才按表达式解析，其余都按 JSON 常量；
- Link 的 **`requestBody`** 不是参数，但同样按上述三类值解析并单独预览（含其表达式错误）；
- 输出会列出目标操作的**全部参数**：未赋值的标 `provided: false`；其中**必填且未提供**的汇总进该 Link 的 `missing_required` 并在页面高亮（表达式取值失败也算未提供，同时保留表达式错误）。

### 逐条输出

每条 Link 给出：Link 名称与 JSON Pointer（`$ref` 组件时给出引用值与解引用后的实际指针）、目标（方法、路径模板、operationId/operationRef、目标指针）、参数表（位置 / 名称 / 必填 / 路径级或操作级 / 解析值与来源表达式 / 状态）、请求体、未被目标接受的键（`unknown_keys`）、缺少的必填参数，以及该条的全部 `errors`（`{code, message}`）。顶部汇总 Link 总数、含错误 / 未知键的条数、缺少必填参数的条数。源路径 / 源操作不存在、无匹配响应、侧别或状态码非法等**请求级问题返回 HTTP 400 / 404**；**Link 级问题不阻断其他 Link，逐条在结果中报错。**

### HTTP API 调用示例

`POST /api/reviews/{id}/link-preview`，请求体为 JSON：

```bash
curl -X POST http://localhost:8000/api/reviews/1/link-preview \
  -H "Content-Type: application/json" \
  -d '{
    "side": "old",
    "method": "POST",
    "path": "/pets",
    "status_code": 201,
    "response_body": { "data": { "id": 42, "/slug": "rex" } },
    "headers": { "X-Request-Id": "abc-123" }
  }'
```

响应（节选）：

```json
{
  "review_id": 1,
  "side": "old",
  "source": { "method": "POST", "path": "/pets" },
  "status_code": 201,
  "matched_response": {
    "key": "201", "match": "exact",
    "pointer": "/paths/~1pets/post/responses/201",
    "declared_pointer": "/paths/~1pets/post/responses/201"
  },
  "links_count": 1,
  "links_with_errors": 1,
  "links_missing_required": 1,
  "links": [
    {
      "name": "getPet",
      "link_pointer": "/paths/~1pets/post/responses/201/links/getPet",
      "ref": null,
      "target": {
        "kind": "operationId", "operation_id": "getPet", "operation_ref": null,
        "method": "GET", "path": "/pets/{petId}",
        "pointer": "/paths/~1pets~1{petId}/get"
      },
      "parameters": [
        { "name": "petId", "in": "path", "required": true, "level": "path",
          "provided": true, "value": 42,
          "expression": "$response.body#/data/id", "errors": [] }
      ],
      "unknown_keys": [
        { "key": "bad", "value": null, "expression": "$request.body#/x",
          "errors": [ { "code": "unsupported-expression", "message": "…" } ] }
      ],
      "missing_required": [ { "name": "X-Tenant", "in": "header" } ],
      "request_body": {
        "provided": true,
        "value": { "createdFrom": 42 },
        "expression": null,
        "errors": []
      },
      "errors": [ { "code": "unsupported-expression", "message": "…" } ]
    }
  ],
  "note": "仅基于提交的响应实例做静态预览：不发送请求、不入库、不改变审查结论。"
}
```

错误码（`errors[].code` / `unknown_keys[].errors[].code`）一览：

| code | 触发条件 |
| --- | --- |
| `external-ref` / `link-external-ref` / `parameter-external-ref` / `operation-ref-external` | 响应 / Link / 参数 / operationRef 用了外部文档引用 |
| `circular-ref` / `link-circular-ref` / `parameter-circular-ref` | 本地 `$ref` 链存在循环 |
| `unresolved-ref` / `link-unresolved-ref` / `parameter-invalid` | `$ref` 指针无法解析、参数对象缺 `name` / `in` |
| `link-no-target` / `operation-both-id-and-ref` | Link 无目标、同时给出 operationId 与 operationRef |
| `operation-id-not-found` / `operation-id-ambiguous` | operationId 缺失或不唯一 |
| `operation-ref-not-operation` / `operation-ref-unresolved` / `operation-ref-invalid` | operationRef 落点不是操作 / 无法解析 |
| `unknown-parameter` / `ambiguous-parameter` | 键指向目标不存在的参数；裸名在目标中不唯一 |
| `unsupported-expression` / `expression-not-resolved` / `header-not-found` | 不支持的运行时表达式；body 指针取不到；响应头缺失 |
| `link-invalid` / `link-parameters-invalid` | Link 不是对象；`parameters` 不是映射 |

请求级错误（HTTP 400，`detail` 为中文说明）：侧别非法、方法非法、路径不以 `/` 开头、状态码越界、源路径 / 源操作不存在、无任何可匹配该状态码的响应、响应或其 `$ref` 链为外部 / 循环 / 失效引用、响应体 JSON 文本解析失败等；审查不存在返回 404。

---

## 🛡️ 限时豁免

某些破坏性变更短期内无法修复，但又不能一直阻塞发布——**限时豁免**允许为具体破坏项登记一条带 **UTC 到期时间**的豁免，到期或手动撤销后自动失效，避免"永久豁免"被遗忘。

**使用方式**：

1. 审查时（或审查后）为审查填写 **API 标识**：创建审查时在「开始审查」按钮旁的输入框填写，或在结果页顶部「API 标识」处填写并保存；**未设置 API 标识的审查不应用任何豁免**。
2. 展开任意 🔴 破坏项，填写**豁免理由**与 **UTC 到期时间**（须为未来时间），点击「＋ 创建限时豁免」。
3. 结果页顶部实时显示：**已豁免 / 未豁免破坏项数量**与放行结论——**未豁免为零才可放行**；豁免记录列表可展开查看并可**撤销**（撤销保留记录，立即失效）。

**匹配规则**（服务端 `app/exemptions.py` 计算，每次读取审查时按当前时间重新计算）：

- 豁免与发现按 **同一 API 标识** 下的 **规则（`rule_id`）+ 方向 + 端点 + 语义位置（`location`）+ 旧值 + 新值** 完全匹配；
- 值比较为**规范 JSON**：**对象键顺序忽略**，**数组顺序与值类型保留**（`1` ≠ `"1"`，数组 `[a,b]` ≠ `[b,a]`）；
- **不使用发现序号匹配**：同一文档重新审查、发现顺序变化不影响豁免命中；
- **同位置的变更值一旦变化**（旧值或新值不同），旧豁免自然失配，该破坏项回到"未豁免"状态，须重新评估并创建新豁免；
- **当前时间达到到期时间（UTC）或已撤销即失效**：失效豁免仍保留在记录中（状态显示"已过期 / 已撤销"），但不再计入"已豁免"数量。

**展示**：破坏项卡片头部显示豁免徽标（🛡️ 已豁免 / ⏰ 豁免过期 / 🚫 豁免撤销），展开后可见匹配状态、理由与到期时间；Markdown / JSON 报告同样包含豁免列、豁免记录表与放行结论。

---

## 📦 报告格式

- **Markdown**：结论、放行结论（未豁免为零才可放行）、统计概览（含已豁免 / 未豁免破坏项）、按严重度排列的明细表（级别/分类/方向/端点/定位/原因，破坏项含豁免列），可折叠的 JSON Pointer 与旧值/新值附录，以及限时豁免记录表。
- **JSON**：结构化结果，包含 `summary`、`exemption_summary`（已豁免/未豁免数量与放行结论）、`exemptions`（该 API 标识下全部豁免记录及命中状态）与全部 `findings`（含 `rule_id`、`severity`、`direction`、`location`、`old_json_path`、`new_json_path`、`old_value`、`new_value`、`exemption` 等），适合接入 CI/CD 质量门禁。

---

## 📦 最小复现包（Minimal Repro）

针对**单条发现**导出一个可独立复现该差异的最小文档包，便于提交 issue、回归测试或在其他工具中复现。

**使用方式**：展开任意发现项 → 点击「📦 导出最小复现包」→ 下载 ZIP。新审查与「历史记录」中重新打开的旧审查均可导出。

**裁剪规则**：

- 旧、新两份文档各自只保留：文档头（`openapi` / `info`，服务器类发现含 `servers`）、该发现所属的 **path / operation**（操作级发现同时保留 path 级 `parameters`，因为引擎会将其合并进操作比较）；
- 从保留内容出发做**本地 `$ref` 闭包**：传递引用到的 `components`（schemas / parameters / requestBodies / pathItems 等）按原指针位置复制进最小文档，闭包用 visited 集合**去重**、对 A→B→A 这类**循环引用安全终止**；
- 未被引用的路径与组件一律剔除。

**导出前复核**：裁剪完成后，后端会用同一差异引擎对最小文档对重新比对，确认目标发现的 `rule_id`、方向、端点与语义定位仍然存在；**复核失败时返回可读错误（HTTP 409），不提供失真的包**。

**ZIP 内容**：

| 文件 | 说明 |
| --- | --- |
| `old.yaml` | 裁剪后的旧版最小文档（可直接重新上传审查） |
| `new.yaml` | 裁剪后的新版最小文档 |
| `manifest.json` | 原审查 ID / 标题 / 时间、发现指纹（语义身份字段的 SHA-256 短哈希）、发现详情（rule_id、方向、端点、定位、JSON Pointer 等）与复核结论 |

`manifest.json` 示例：

```json
{
  "format": "openapi-repro/1",
  "generated_at": "2026-09-18T08:00:00+00:00",
  "review": { "id": 7, "title": "Petstore 1.4.0 → 2.0.0", "..." : "..." },
  "finding": {
    "id": 12,
    "fingerprint": "3f8c1a2b9d0e4f56",
    "rule_id": "property-became-required",
    "direction": "request",
    "endpoint": "POST /pets",
    "location": "requestBody > application/json → 请求体.status"
  },
  "verification": { "status": "passed", "engine": "run_diff" },
  "files": ["old.yaml", "new.yaml", "manifest.json"]
}
```

---

## 🗂️ 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI：审查 / 历史 / 引用树 / 根因聚合 / 破坏项对账 / 组件可达性审计 / 调用清单影响定位 / 请求样本回放 / Link 参数预览 / 限时豁免 / 报告下载 / 健康检查接口
│   ├── parser.py      # JSON·YAML 载入与 OpenAPI 3.x 校验
│   ├── refs.py        # JSON Pointer、$ref 解析、循环安全的引用展开树
│   ├── diff.py        # 语义兼容性规则引擎（请求/响应双方向 + 操作级/根级 security 认证要求）
│   ├── groups.py      # 根因聚合：端点×方向分区、同源归并、祖先吸收的有向影响组
│   ├── reconcile.py   # 破坏项对账：稳定身份配对，输出新增 / 延续 / 已消失
│   ├── reachability.py # 组件可达性审计：paths/webhooks 入口 → 本地 $ref 传递追踪，未触达组件与无效引用
│   ├── impact.py      # 调用清单影响定位：CSV(method,path,count) → 旧版操作匹配（静态段优先/歧义不分配）+ 破坏关联
│   ├── samplecheck.py # 请求样本回放：双侧请求体 schema 校验（type/required/enum/properties/items/allOf/本地$ref，循环安全）+ 旧接受/新拒绝风险汇总
│   ├── linkpreview.py # Link 后续调用参数预览：响应实例求值 $response.body#/#header（精确/范围/default 响应匹配、operationId/本地 operationRef 目标、路径级+操作级参数合并，$ref 循环安全、外部引用报错）
│   ├── exemptions.py  # 限时豁免：API 标识 + 语义指纹匹配，到期 / 撤销判定与放行结论
│   ├── repro.py       # 最小复现包：文档裁剪、$ref 闭包、复核与 ZIP 打包
│   ├── reports.py     # Markdown / JSON 报告生成
│   └── database.py    # SQLite 持久化（评审、原文、结果、豁免；启动时自动升级表结构）
├── static/
│   ├── index.html     # 单页界面
│   ├── styles.css
│   ├── app.js         # 原生 JS：筛选 / 聚合视图 / 展开树 / 历史 / 对账 / 调用清单导入 / 请求样本回放 / Link 参数预览 / 下载
│   └── samples.js     # 五组内置示例（JSON + YAML，含认证要求收紧）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 🔌 HTTP API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/reviews` | 提交 `{old_spec, new_spec, api_id?, ...}` 执行审查并持久化（响应含豁免视图） |
| `GET` | `/api/reviews` | 历史审查列表 |
| `GET` | `/api/reviews/{id}` | 审查详情（含全部 findings 与实时豁免视图） |
| `PATCH` | `/api/reviews/{id}` | 设置 / 清除审查的 API 标识 `{api_id}` |
| `DELETE` | `/api/reviews/{id}` | 删除审查 |
| `POST` | `/api/reviews/{id}/exemptions` | 为破坏项创建限时豁免 `{finding_id, reason, expires_at}`（UTC，须为未来时间） |
| `POST` | `/api/reviews/{id}/exemptions/{eid}/revoke` | 撤销豁免（保留记录，立即失效） |
| `GET` | `/api/reviews/{id}/groups` | 根因聚合：有向影响组（稳定组标识 / 主因 / 受影响条目 / 可展开定位） |
| `GET` | `/api/reviews/{id}/reconcile?baseline_id=N` | 破坏项对账：基线 vs 当前审查，输出新增 / 延续 / 已消失三类破坏项及数量（基线不存在 404、与当前相同 400） |
| `GET` | `/api/reviews/{id}/reachability` | 组件可达性审计：各侧未触达组件（类型/名称/数量）、新版独有标记、无效本地引用明细 |
| `POST` | `/api/reviews/{id}/impact` | 调用清单影响定位：请求体为 `method,path,count` 的 CSV 文本（`Content-Type: text/csv`），返回按调用量排序的影响表及未匹配 / 歧义 / 非法行（实时计算、不入库、不改变审查结论；缺列 / 无数据行返回 400） |
| `POST` | `/api/reviews/{id}/sample-check` | 请求样本回放：请求体 `{samples: [{method, path, content_type, body}, ...]}`，逐条返回旧版 / 新版请求体校验状态（accepted/rejected/undeterminable）、失败字段路径与「旧版接受而新版拒绝」风险汇总（实时计算、不入库、不改变审查结论；samples 缺失 / 为空 / 非数组 / 超 200 条返回 400，单条格式错误在条目中逐条报错） |
| `POST` | `/api/reviews/{id}/link-preview` | Link 后续调用参数预览：请求体 `{side: old\|new, method, path, status_code, response_body, headers}`，响应按精确码 → 范围码 → default 匹配，逐条返回 Link 指针、唯一 operationId / 本地 operationRef 目标方法路径与每个参数的解析值（JSON 常量 / `$response.body#JSONPointer` / `$response.header.名称`，~0/~1 解码、头名忽略大小写、null 算已提供；路径级+操作级参数合并）及未知参数 / 歧义裸名 / 缺必填 / 不支持表达式 / 外部或失效 $ref 逐条错误（实时计算、不发请求、不入库、不改变审查结论；源操作或响应缺失等请求级问题 400，审查不存在 404） |
| `GET` | `/api/reviews/{id}/pointers` | 引用树根节点分组（paths / components） |
| `GET` | `/api/reviews/{id}/tree?side=old|new&pointer=/...` | 引用展开树（含循环标记） |
| `GET` | `/api/reviews/{id}/report?format=markdown|json` | 下载报告（含豁免匹配状态、理由与到期时间） |
| `GET` | `/api/reviews/{id}/findings/{fid}/repro` | 导出单条发现的最小复现包（ZIP；复核失败返回 409 及可读原因） |
| `GET` | `/api/health` | 健康检查（Docker HEALTHCHECK 与编排探活使用） |

交互式文档：http://localhost:8000/docs （FastAPI 自动生成）。

## ⚠️ 范围说明

- 仅支持 OpenAPI **3.0 / 3.1**；Swagger 2.0 会被明确拒绝并提示。
- 仅解析**文档内** `$ref`；外部文件引用（`./other.yaml#/...`）会在树中标记为无法解析。
- 规则基于 OpenAPI 社区通行的破坏性变更分类（参考 OASdiff 的方向化判定），属于静态契约分析，不验证具体业务逻辑。

# 磅单码配码 / 解码服务（FastAPI + SQLite + Docker Compose）

几家工厂共用一个磅单入口，设备只能用**一位数字**做格式标记。

- 磅单码固定 **9 位数字**：第 1 位是标记 `marker`，后 8 位是正文 `body`。
- 每套模板规定：正文前缀 `prefix`、秤号在正文中的截取位置、净重克数的截取位置。
- **秤号按字符串处理，保留前导零**；净重按整数克处理。
- 两套模板若**存在同一正文**能同时命中、却会解出不同 `(秤号, 净重)`，就**必须**使用不同标记。
- 支持先**导入现用模板及标记**；批量登记新模板时保留所有原标记，返回每套新模板的标记，并使**新增使用的数字种数最少**。
- 新旧模板合计最多 **12 套**。
- 分配基于**提交时的当前生效配置**计算；无解或保存失败时整体回滚，**沿用原配置**。
- 解码始终读取**当前生效配置**，返回秤号（字符串）与净重克数（整数）。
- 支持给下游数仓生成**只读换版计划**：按旧、新配置版号与缓存容量（1～12 条），
  给出边导入边逐条换规则的执行顺序，保证每一步装得下、共同可解码码始终可解、
  且任意可解码只有一组秤号与净重。

---

## 1. 快速开始

### Docker Compose（推荐）

```bash
docker compose up -d --build
# 等待健康检查通过
curl http://127.0.0.1:8000/health
```

- 服务监听 `8000` 端口（宿主机端口可用 `HOST_PORT=8080 docker compose up -d` 改）。
- SQLite 数据库持久化在宿主机 `./data/bangdan.db`（容器内 `/data/bangdan.db`）。
- 交互式文档： <http://127.0.0.1:8000/docs> 。

### 本地直接运行

```bash
python -m pip install -r requirements.txt
python -m app.main                 # 默认 0.0.0.0:8000，DB 为 ./data/bangdan.db
# 或： uvicorn app.main:app --reload
```

运行测试：

```bash
python -m pip install -r requirements-dev.txt
python -m pytest -q
```

---

## 2. 模板格式（规范字段）

位置默认**相对 8 位正文、0 基、左闭右开（用 `start + length`）**。

```json
{
  "name": "甲厂",
  "prefix": "10",
  "scale":  { "start": 2, "length": 2 },
  "weight": { "start": 4, "length": 4 }
}
```

上例正文布局：

```
位置:  0 1 2 3 4 5 6 7
正文:  1 0 0 7 0 1 2 3
       └前缀┘ └秤号┘ └─净重(克)─┘
秤号 = body[2:4] = "07"   （保留前导零）
净重 = int(body[4:8]) = 123
```

对应磅单码 `marker=1` + 正文 `10070123` ⇒ `110070123`。

> 1 基位置可在请求顶层加 `"index_base": 1`（此时 `start` 为 1 基；`end` 按中文习惯视为含端点）。
> 也支持 `scale_start/scale_length`、`scale_range:[起,止)`、`"2:4"`、camelCase
> （`scaleRange`/`bodyPrefix`）以及常见别名字段，详见第 6 节。

---

## 3. API

所有写/解接口均接收/返回 JSON，统一带有 `success` 字段；失败时返回 4xx/5xx 与
`{"success": false, "code": "错误码", "message": "...", "detail": "..."}`。

### 3.1 健康检查 / 当前配置

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/config
curl http://127.0.0.1:8000/api/templates
curl http://127.0.0.1:8000/api/conflicts     # 冲突图与同标记违规
```

### 3.2 导入现用模板及标记

`POST /api/import`（等价别名：`/api/templates/import`、`/import`；查询参数 `mode=replace|append`、`force=true`）

```bash
curl -X POST http://127.0.0.1:8000/api/import \
  -H 'Content-Type: application/json' \
  -d '{
    "templates": [
      {"name":"甲厂","marker":"1","prefix":"10",
       "scale":{"start":2,"length":2},"weight":{"start":4,"length":4}},
      {"name":"乙厂","marker":"2","prefix":"20",
       "scale":{"start":2,"length":3},"weight":{"start":5,"length":3}}
    ]
  }'
```

- 导入是“现状”，每套模板必须带一位数字 `marker`（接受 `"1"` 或 `1`），原样保留。
- 默认 `mode=replace`（用导入集合替换生效配置，适合初始化/测试复位）；`mode=append` 追加。
- 导入后会检查“同标记且冲突”的违规并在 `warnings` 中给出（正常现用配置应为空）。
- 也可用 CLI：`python -m app.import_seed seed/current_templates.example.json`。

### 3.3 批量登记 + 配码（核心）

`POST /api/assign`

等价 REST 路径：`POST /api/templates`、`POST /api/templates/batch`、
`POST /api/assign-markers`、`POST /api/peima`、`POST /peima` 等。

请求（**不需要**给新模板 marker，由服务器分配）：

```bash
curl -X POST http://127.0.0.1:8000/api/assign \
  -H 'Content-Type: application/json' \
  -d '{
    "templates": [
      {"name":"丙厂","prefix":"30",
       "scale":{"start":2,"length":2},"weight":{"start":4,"length":4}}
    ]
  }'
```

响应（节选）：

```json
{
  "success": true,
  "version": 2,
  "old_count": 2,
  "new_count": 1,
  "total": 3,
  "max_templates": 12,
  "markers": ["1"],
  "marker_digits": [1],
  "new_digits": [],
  "new_digit_count": 0,
  "assignments": [
    {"index": 0, "marker": "1", "marker_digit": 1, "mark": "1",
     "is_new_digit": false, "template": { "...": "..." }}
  ],
  "config": { "...": "新的生效配置快照..." }
}
```

字段说明：

- `markers[i]` 与提交的 `templates[i]` 一一对齐，即“每套新模板的标记”（字符串，另有整数版 `marker_digits`）。
- `new_digit_count` / `new_digits`：相对现用标记集合，**新启用的数字种数/具体数字**（优化目标，取全局最小）。
- `total > 12` ⇒ `409 TEMPLATE_LIMIT_EXCEEDED`，不产生新版本。
- 染色无解（例如一套新模板与 0–9 全部十种标记的现用模板冲突）⇒ `409 NO_SOLUTION`。
- 落库失败 ⇒ `500 SAVE_FAILED`。
- 以上失败均回滚，`GET /api/config` 的 `version` 与内容保持不变。
- `GET /api/plan` / `POST /api/plan` 为**只读**接口：只读取指定版本，不产生新版本，也不改动当前生效配置。

换版计划的完整契约见 **3.5 节**。

分配规则：

1. 构建模板两两冲突图（见第 4 节）。
2. 旧模板 marker 作为预染色固定不变。
3. 为新模板求合法染色；首要目标：新启用数字种数最少；其次尽量集中到更少的不同标记；
   最后按提交顺序给出字典序最小的确定结果。

### 3.4 解码（核心）

`POST /api/decode`（别名 `/decode`、`/api/jiema`、`/jiema`、`/api/bill/decode` 等；
也支持 `GET /api/decode/{code}` 与 `GET /api/decode?code=...`）

```bash
curl -X POST http://127.0.0.1:8000/api/decode \
  -H 'Content-Type: application/json' \
  -d '{"code":"110070123"}'
```

```json
{
  "success": true,
  "code": "110070123",
  "marker": "1",
  "body": "10070123",
  "scale_no": "07",
  "scale_number": "07",
  "scale": "07",
  "net_weight_g": 123,
  "net_weight": 123,
  "weight_g": 123,
  "秤号": "07",
  "净重克数": 123,
  "matched_count": 1,
  "matched_templates": ["..."]
}
```

- `scale_no` 永远是字符串，前导零不丢。
- 找不到命中模板 ⇒ `404 NO_MATCHING_TEMPLATE`。
- 码不是 9 位数字 ⇒ `422 INVALID_CODE`；8 位字符串会提示补首位 0 并以 9 位字符串重传。
- 若生效配置本身违规（同标记模板对同一正文解出不同结果）⇒ `409 AMBIGUOUS_DECODE`。

### 3.5 换版计划（只读）

`POST /api/plan`（别名：`/api/switch-plan`、`/api/version-plan`、`/api/plans`、
`/api/huanban`、`/plan`、`/api/v1/plan`；也可 `GET /api/plan?old=..&new=..&capacity=..`）

下游数仓边导入磅单、边逐条更换缓存里的解码规则。调用方给出**旧、新配置版号**和
**缓存容量**（1～12 条），拿到按执行顺序排列的换装清单。**该接口只读取配置，不会
产生新版本、也不会改动当前生效版本。**

```bash
curl -X POST http://127.0.0.1:8000/api/plan \
  -H 'Content-Type: application/json' \
  -d '{"old_version": 2, "new_version": 5, "capacity": 3}'
```

请求字段接受常见别名：`from_version/to_version`、`old/new`、`version`、
`cache_size/cache_capacity/limit`，以及中文 `旧版/新版/缓存容量`；也支持
`{"versions": {"old": .., "new": ..}, "capacity": ..}` 的嵌套写法。

**规则与两版差异**

- 一条“规则”由 **标记、前缀、秤号截取区间、净重截取区间** 完全相同来确定；
  多套模板命中同一条规则时只算一条缓存项。
- 两版都有的规则是**共用规则**，整个换装过程始终留在缓存里，不占步骤。
- 两版各自独有的规则合计**最多 8 条**；超过即 `409 RULE_LIMIT_EXCEEDED`。

**每一步允许的动作**（`action` 字段）：

| `action` | 含义 | 涉及字段 |
| --- | --- | --- |
| `install` | 装入一条新版独有规则 | `install`（`new_rule`/`add` 同义） |
| `remove` | 卸下一条旧版独有规则 | `remove`（`old_rule`/`uninstall` 同义） |
| `replace` | 一步内同时卸旧装新（原子替换） | 两者都有 |

**对每一中间状态的保证**（都在整个 9 位码空间上解析判定，不逐码试解）：

1. 缓存条数不超过容量（`step.fits` / `step.size_after`）。
2. **两版原本都能解的码仍能解**：两版可解码集合的交集始终被已装规则覆盖
   （前缀柱面是层族，交集/并集可归约为少量前缀）。
3. **任何能解的码只有一组秤号和净重**：已装规则中同标记的两两冲突用
   `app/conflicts.py` 的精确判定检查，无需枚举正文。

规划采用 BFS 求**最少步骤**的可行顺序（同长时优先原子替换、再按规则序稳定取舍）。
响应中的 `steps`（同义 `plan`/`schedule`/`operations`/`checklist`/`换装清单`）
即按执行顺序排列的清单，每步含 `action`、`install`、`remove`、`size_before`、
`size_after`、`capacity`、`cache_after_keys` 及说明。

响应（节选）：

```json
{
  "success": true,
  "read_only": true,
  "old_version": 2,
  "new_version": 5,
  "capacity": 3,
  "step_count": 2,
  "shared_rule_count": 1,
  "old_only_count": 1,
  "new_only_count": 2,
  "unique_rule_total": 3,
  "max_unique_rules": 8,
  "steps": [
    {"step": 1, "action": "replace",
     "remove": {"rule_id": "2:20|2+3|5+3", "name": "乙厂"},
     "install": {"rule_id": "2:21|2+3|5+3", "name": "乙厂新"},
     "size_before": 2, "size_after": 2, "capacity": 3, "fits": true},
    {"step": 2, "action": "install",
     "install": {"rule_id": "2:210|2+3|5+3", "name": "乙厂扩展"},
     "size_before": 2, "size_after": 3, "capacity": 3, "fits": true}
  ],
  "shared_decodable": [{"marker": "1", "prefix": "10"}],
  "guarantees": {
    "capacity_respected_every_step": true,
    "shared_decodable_codes_always_decodable": true,
    "unique_decode_every_step": true,
    "checked_over_entire_code_space": true,
    "brute_force_used": false
  }
}
```

失败原因（`code`，均在 409/422/404）：

| 情况 | HTTP | `code` | `details.reason` |
| --- | --- | --- | --- |
| 缺版号 | 422 | `MISSING_VERSION` | `missing_version` |
| 版号不存在 | 404 | `VERSION_NOT_FOUND` | `missing_version` |
| 容量缺失/非整数/越界 | 422 | `MISSING_CAPACITY` / `INVALID_CAPACITY` | `missing_capacity` / `invalid_capacity` |
| 两版独有规则合计超 8 | 409 | `RULE_LIMIT_EXCEEDED` | `unique_rule_limit_exceeded` |
| 任一版内部有解码冲突 | 409 | `DECODE_CONFLICT` | `decode_conflict` |
| 无可行换装顺序 | 409 | `NO_FEASIBLE_ORDER` | `capacity_too_small` / `no_feasible_order` |

`DECODE_CONFLICT` 的 `details.versions.old/new` 会给出具体冲突模板对；
`NO_FEASIBLE_ORDER` 的 `details` 会带上容量与两版规则结构，便于判断是容量不足
（初始/目标缓存已超容量）还是确实不存在同时满足三条保证的顺序。

### 3.6 辅助：按模板生成码

`POST /api/encode`：给 `marker`、内联模板（或 `template_id/index/name`）、`scale_no`、
`net_weight_g`，可选 `filler`（默认 `0`），返回拼好的 9 位码。

### 3.7 复位（测试/演示用）

```bash
curl -X POST http://127.0.0.1:8000/api/admin/reset
```

---

## 4. 冲突判定（精确规则）

对两套模板 A/B：

1. **是否存在共同正文？** 两个字面前缀在重叠区间必须逐位一致
   （等价于一个前缀是另一个的前缀）。前缀冲突（如 `"12"` vs `"13"`）则永不命中同一正文，
   不构成约束，可共用标记。
2. **秤号是否可能不同？** 秤号按字符串比较：
   - 长度不同 ⇒ 必不同，构成冲突；
   - 等长时逐位比较两个区间：若对应到同一位置、或两位都被前缀固定成相同数字，则该位恒等；
     只要存在一个对应位落在可自由选择的数字上（或被前缀固定成不同数字），就能构造出不同秤号。
3. **净重是否可能不同？** 净重是整数克，其值是关于正文各位数字的仿射式
   `C + Σ c_j·d_j`（按十进制位权，对两套模板求差）。若所有非零系数位都被前缀固定、
   且常数项 `C == 0`，则对所有共同正文净重恒等；否则存在正文使净重不同。
   （这也正确处理了前导零与错位区间，例如 `"0d"` 与 `"d"` 在首位被前缀固定为 `0` 时数值恒等。）

只要秤号或净重任一“可能不同”，这对模板之间连边，必须使用不同标记。
该判定无需枚举 10^8 个正文。实现见 `app/conflicts.py`，并有完整单测。

---

## 5. 配置存储与原子性

SQLite 表 `config_versions` + `templates`：每次成功的导入/配码/复位都生成一个新的
不可变版本，并通过部分唯一索引保证恰有一个 `active=1` 的当前版本。

- 配码在同一把进程锁 + `BEGIN IMMEDIATE` 事务内“读当前生效版本 → 计算 → 写新版本并切换指针”。
- `NO_SOLUTION`、超过 12 套或任何保存异常都会回滚；活动版本指针不变，即“沿用原配置”。
- 单 uvicorn worker / 单容器部署；SQLite 文件可直接挂载备份。

---

## 6. 兼容性速查

| 项目 | 支持的写法 |
| --- | --- |
| 模板集合 | `{"templates":[...]}`、裸数组 `[...]`、`items/records/data/list/factories/模板` |
| 模板名 | `name/template_name/factory/factory_name/title/名称/厂名` |
| 前缀 | `prefix/body_prefix/bodyPrefix/前缀/正文前缀` |
| 标记 | `marker/mark/tag/digit/标记`（输入接受 `1` 或 `"1"`；响应同时给字符串与 `*_digit` 整数） |
| 秤号区间 | `scale:{start,length}`、`scale_start/scale_length`、`scaleRange:[a,b]`、`scale_pos/scale_span/...`、中文 `秤号` |
| 净重区间 | `weight` / `net_weight` / `netWeight` / `net_weight_g` / `weight_start...` / `净重克数` |
| 区间对象键 | `start/begin/from/first/起点`、`length/len/size/长度`、`end/stop/to/last/终点` |
| 下标基准 | 顶层或模板内 `index_base: 1`；二元数组可用 `"array_format":"length"` 表示 `[起,长]` |
| 解码码字段 | `code/bill_code/billNo/bangdan_code/no/number/磅单码/单号`，或 `{marker, body}` |

若你的客户端字段/路径不在表中，服务通常也能通过归一化键名（忽略大小写、下划线、连字符）
与模糊匹配识别；仍不可用时请参考 `/openapi.json` 的规范契约。

---

## 7. 目录结构

```
app/
  domain.py        # TemplateSpec/Range 与 9 位码常量、12 套上限
  conflicts.py     # 精确的共同正文 / 解码差异（冲突图）分析
  coloring.py      # 预染色图着色 + 新增数字种数最优解
  plan.py          # 只读换版计划：规则差异 + 最少步骤可行换装顺序（全码空间解析判定）
  payload.py       # 宽松字段/位置解析
  schemas.py       # OpenAPI/Pydantic 契约（before 校验复用 payload）
  storage.py       # SQLite 版本化配置与事务
  service.py       # 导入/配码/解码用例与响应序列化
  main.py          # FastAPI 路由与大量兼容别名
  import_seed.py   # 现用模板导入 CLI
tests/             # 271 个单元/接口测试（含 200 组穷举一致性 + 随机暴力最优性校验）
seed/              # 现用模板示例
Dockerfile
docker-compose.yml
requirements.txt
```

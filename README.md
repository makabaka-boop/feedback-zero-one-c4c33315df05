# 加工企业批次放行质量系统（纯后端）

面向加工企业质量人员的自定义工程场景（不代表行业法规）：随生产谱系与风险处置
变化，判断批次是否可放行。仅依赖 Python 标准库与 SQLite，从零实现批次登记、
加工谱系订正、风险追溯、处置核验四项职责。

- `qms.py` — 后端源码（库 + JSON Lines 命令行）
- `verify_qms.py` — 独立验证脚本（仅通过公开接口核对）
- 运行要求：Python 3.8+，无第三方依赖

## 运行

```bash
# 独立验证（86 项核对，全部通过时退出码 0）
python3 verify_qms.py

# 命令行演示：每行一个 JSON 请求，输出 {"result": ...} 或 {"error": ...}
python3 qms.py /tmp/qms.db
{"op":"register_batch","batch_id":"A"}
{"op":"register_batch","batch_id":"B"}
{"op":"add_record","record_id":"M1","inputs":["A"],"outputs":["B"]}
{"op":"register_risk","risk_id":"R1","batch_ids":["A"]}
{"op":"query_batch","batch_id":"B"}
{"op":"confirm_disposition","batch_id":"B","risk_id":"R1","evidence":"ev-<上一步查询返回的 token>"}
```

作为库使用：`from qms import Store, QmsError`，`Store(db_path)` 线程安全，
`db_path` 可为文件路径或 `":memory:"`。

## 接口

所有标识（批次、加工记录、风险）均为非空字符串。写方法返回
`{"ok": true, "op": <全局操作序号>, ...}`；拒绝抛 `QmsError` 且不改状态。

| 方法 | 说明 |
| --- | --- |
| `register_batch(batch_id)` | 登记批次。登记后不可删除、身份不可复用。 |
| `add_record(record_id, inputs, outputs)` | 新增加工记录。`inputs`/`outputs` 为非空无重复批次列表，引用须已登记；每个投入与全部产出建立有向关系。返回不复用的 `revision`。 |
| `replace_record(record_id, inputs, outputs)` | 整体替换记录内容；同内容替换也算修订，生成新 `revision`。 |
| `delete_record(record_id)` | 删除记录；其修订身份退役，删除后重建不得沿用。 |
| `register_risk(risk_id, batch_ids)` | 登记风险：非空直接受影响批次集合，登记后不可修改；身份不得复用（含撤销后）。 |
| `revoke_risk(risk_id)` | 撤销风险；其有效处置裁决为 `RISK_REVOKED` 失效。 |
| `query_batch(batch_id)` | 返回当前风险、有效处置、未处置/处置失效原因、放行判定，以及每项当前风险的依据凭证（见下）。 |
| `confirm_disposition(batch_id, risk_id, evidence)` | 人工确认处置。仅当风险仍影响该批次且凭证当前有效才成功；有效凭证重复确认幂等成功（`already: true`）。 |

### 查询返回（JSON 可序列化字典）

```json
{
  "batch": "B",
  "releasable": false,
  "current_risks": [{
    "risk_id": "R1",
    "directly_affected": ["A"],
    "affected_via": ["A"],
    "disposed": false,
    "disposition": null,
    "undisposed_reason": {"code": "NEVER_DISPOSED"},
    "evidence": {
      "token": "ev-<sha256>",
      "batch": "B",
      "risk": {"risk_id": "R1", "directly_affected": ["A"], "revoked": false},
      "basis": [{"record_id": "M1", "revision": "rev-000000000001",
                 "inputs": ["A"], "outputs": ["B"]}]
    }
  }],
  "valid_dispositions": [],
  "disposition_history": []
}
```

- `releasable`：无当前风险，或全部当前风险均有有效处置时为 `true`。
- `undisposed_reason.code`：`NEVER_DISPOSED`（从未处置）或
  `DISPOSITION_INVALIDATED`（处置失效，附 `invalid_reason`：
  `BASIS_CHANGED` / `RISK_REVOKED`）。
- `evidence`：依据凭证，绑定该批次**全部上游加工记录的内容与修订身份**
  以及该风险登记；确认时提交其中的 `token`。

## 编码约定

- 传输/存储编码：JSON（UTF-8）；标识为任意非空字符串，集合入参为列表。
- 修订身份：`rev-` + 12 位全局递增序号，取自持久化计数器，任何成功写入
  （新增/替换，含同内容替换）消耗一个，删除后重建亦不沿用，全局绝不复用。
- 凭证：`ev-` + 对 (批次, 风险登记, 全部上游记录内容与修订身份) 规范化
  JSON 的 SHA-256。上游依据集合或其中修订身份任何变化、风险撤销均使凭证
  失效；因修订号不复用，失效永久，恢复相同关系也不复活。
- 操作序号 `op`：全局递增逻辑时钟，用于排序变更与失效裁决。

## 错误格式

拒绝抛 `QmsError`，序列化为
`{"error": {"code": <机器码>, "message": <说明>, "details": {...}}}`；
CLI 原样输出。拒绝时当前事务整体回滚，不改状态；多重错误时任报其一。

| code | 含义 |
| --- | --- |
| `INVALID_ID` | 标识为空或非字符串 |
| `EMPTY_SET` | 投入/产出/风险批次集合为空 |
| `DUPLICATE_ID` | 集合内标识重复 |
| `BATCH_EXISTS` | 批次已登记（身份不可复用） |
| `BATCH_NOT_FOUND` | 批次未登记（含记录/风险引用了未登记批次） |
| `RECORD_EXISTS` | 加工记录已存在（应改用整体替换） |
| `RECORD_NOT_FOUND` | 加工记录不存在 |
| `CYCLE` | 加工关系成环（含自环；返工须登记新批次） |
| `RISK_EXISTS` | 风险身份已存在（撤销后亦不得复用） |
| `RISK_NOT_FOUND` | 风险不存在 |
| `RISK_ALREADY_REVOKED` | 风险已撤销（重复撤销） |
| `RISK_REVOKED` | 确认时风险已撤销 |
| `RISK_NOT_APPLICABLE` | 风险当前不影响该批次 |
| `EVIDENCE_STALE` | 凭证已失效或与当前状态不符，须重新查询 |
| `UNKNOWN_OP` / `BAD_REQUEST` | CLI 未知操作 / 请求格式错误 |

## 语义与一致性设计

- **传播与局部解除**：有效风险影响直接受影响批次及其全部下游（按批次级
  有向图求闭包，多路径只计一次，直接批次自身计入）。处置按 (批次, 风险)
  记录，只解除指定批次的指定风险，不阻断向下游传播。
- **失效裁决**：每次变更（记录增/替/删、风险撤销）在同一事务内即时复核
  全部有效处置并持久化失效原因：风险撤销 → `RISK_REVOKED`；该批次上游
  依据（记录集合 + 修订身份）与确认时快照不一致 → `BASIS_CHANGED`。
  依据之外的变化不改变快照，故无关变化不影响处置。
- **不复活**：修订身份全局单调不复用，任何“恢复相同关系”的重建必然得到
  新修订号，旧凭证/旧处置永不复活；风险撤销为终态。
- **串行等价与原子持久**：单写者可重入锁 + `BEGIN IMMEDIATE` 事务，所有
  变更、失效裁决与查询串行执行；变更与其引发的失效裁决同事务提交；
  `synchronous=FULL`，重启仅见完整已提交状态，序号自持久化计数器延续。

## 独立验证覆盖（verify_qms.py）

- **S1 传播与局部解除**：菱形谱系多路径只计一次、直接批次自身纳入、
  处置 B 不影响 C/D、逐批次确认后放行、无关批次拒绝、伪造凭证拒绝。
- **S2 依据恢复不复活**：同内容替换算修订、删除重建不沿用旧身份、
  依据变化使处置/凭证失效、重建相同关系不复活、无关变化不影响、
  空依据批次新增上游记录后失效。
- **S3 交错确认一致性**：双风险凭证交错确认、无关变化后旧凭证仍有效、
  相关变化后全部凭证失效、跨风险凭证张冠李戴拒绝、风险撤销后处置裁决
  为 `RISK_REVOKED` 且身份不得复用。
- **S4 持久化与重启**：重启后已提交处置/凭证有效、重复确认幂等、修订
  序号延续、失效裁决持久可见。
- **S5 校验**：全部入参与状态拒绝路径，且拒绝后谱系状态不变。
- **S6 多线程交错**：1 写线程 + 4 确认线程交错，结果只允许成功或
  `EVIDENCE_STALE`，结束后状态一致（恰一个有效处置）。

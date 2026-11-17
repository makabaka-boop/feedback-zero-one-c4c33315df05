# -*- coding: utf-8 -*-
"""
qms.py — 加工企业批次放行质量系统（纯后端，仅依赖 Python 标准库与 SQLite）

职责
====
1. 批次登记：非空字符串唯一标识；登记后不可删除、身份不可复用。
2. 加工谱系订正：加工记录含非空、无重复的投入/产出批次集合，引用批次须已登记；
   每个投入与全部产出建立有向关系；支持新增、整体替换、删除；最终关系不得成环
   （返工须登记新批次）。每次成功写入生成全局不复用的修订身份；同内容替换也算
   修订；删除后重建不得沿用旧修订身份。
3. 风险追溯：风险含不可修改的非空直接受影响批次集合；可撤销，身份不得复用；
   有效风险影响直接批次及其全部下游（多条路径到达只计一次；直接批次自身计入）。
4. 处置核验：处置仅解除指定批次的指定风险，不阻断向下游传播；查询返回当前风险、
   有效处置、未处置/处置失效原因与放行判定；每项当前风险附依据凭证（绑定该批次
   全部上游加工记录的内容与修订身份，以及该风险登记）；人工确认须风险仍影响该
   批次且凭证当前有效。上游依据集合或其中修订身份改变、风险撤销，均使相关凭证
   与处置永久失效；恢复相同关系也不复活；无关变化不影响处置。

一致性与持久化
==============
- 单写者可重入锁 + SQLite `BEGIN IMMEDIATE` 事务：所有变更、失效裁决与查询
  串行等价执行；Store 实例线程安全。
- 每次变更与随附的失效裁决在同一事务内原子提交；拒绝抛 QmsError 并回滚，
  不改状态。重启后仅见完整已提交状态。
- 修订身份取自持久化的全局递增序号，任何写入（含同内容替换、删除后重建）
  都消耗新序号，故旧凭证/旧处置永远无法被“恢复相同关系”复活。

接口、编码与错误格式见 README.md。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager

# 处置状态与失效原因（持久化取值，亦出现在查询结果中）
VALID = "VALID"
INVALID = "INVALID"
REASON_BASIS_CHANGED = "BASIS_CHANGED"   # 上游加工依据集合或其中修订身份改变
REASON_RISK_REVOKED = "RISK_REVOKED"     # 风险被撤销


# --------------------------------------------------------------------------- #
# 错误格式
# --------------------------------------------------------------------------- #
class QmsError(Exception):
    """业务拒绝。携带稳定机器码 code、人类可读 message、结构化 details。

    序列化格式：{"error": {"code": ..., "message": ..., "details": {...}}}
    拒绝发生时当前事务整体回滚，不改任何状态。多重错误时任报其一。
    """

    def __init__(self, code, message, details=None):
        super().__init__("%s: %s" % (code, message))
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self):
        return {"error": {"code": self.code, "message": self.message,
                          "details": self.details}}


# --------------------------------------------------------------------------- #
# 入参校验
# --------------------------------------------------------------------------- #
def _check_id(value, field):
    """各类对象标识：非空字符串。"""
    if not isinstance(value, str) or value == "":
        raise QmsError("INVALID_ID", "标识必须为非空字符串",
                       {"field": field, "value": value})
    return value


def _check_id_set(value, field):
    """非空、无重复的标识集合；返回排序后的列表（规范化，便于确定性编码）。"""
    if not isinstance(value, (list, tuple)) or len(value) == 0:
        raise QmsError("EMPTY_SET", "集合必须非空（传入列表）",
                       {"field": field})
    seen, out, dups = set(), [], []
    for v in value:
        _check_id(v, field)
        if v in seen:
            dups.append(v)
        else:
            seen.add(v)
            out.append(v)
    if dups:
        raise QmsError("DUPLICATE_ID", "集合内存在重复标识",
                       {"field": field, "duplicates": sorted(dups)})
    return sorted(out)


# --------------------------------------------------------------------------- #
# 图算法（纯函数，输入为当前存活加工记录字典）
# records: {record_id: {"revision": str, "inputs": [..], "outputs": [..]}}
# --------------------------------------------------------------------------- #
def _batch_edges(records):
    """批次级有向邻接：每个投入指向全部产出。所有出现的批次都是节点。"""
    adj = {}
    for rec in records.values():
        for i in rec["inputs"]:
            adj.setdefault(i, set()).update(rec["outputs"])
        for o in rec["outputs"]:
            adj.setdefault(o, set())
    return adj


def _assert_acyclic(records):
    """最终关系不得成环（含自环）。Kahn 拓扑检测，成环则拒绝。"""
    adj = _batch_edges(records)
    indeg = {b: 0 for b in adj}
    for outs in adj.values():
        for o in outs:
            indeg[o] += 1
    queue = [b for b, d in indeg.items() if d == 0]
    seen = 0
    while queue:
        b = queue.pop()
        seen += 1
        for o in adj[b]:
            indeg[o] -= 1
            if indeg[o] == 0:
                queue.append(o)
    if seen != len(indeg):
        remaining = sorted(b for b, d in indeg.items() if d > 0)
        raise QmsError("CYCLE", "加工关系成环；返工须登记新批次",
                       {"batches_in_cycle": remaining})


def _upstream_cone(records, batch):
    """该批次全部上游加工记录：凡其产出可沿有向关系到达该批次的记录。"""
    producers = {}  # 产出批次 -> [record_id]
    for rid, rec in records.items():
        for o in rec["outputs"]:
            producers.setdefault(o, []).append(rid)
    cone, visited, stack = {}, set(), [batch]
    while stack:
        b = stack.pop()
        if b in visited:
            continue
        visited.add(b)
        for rid in producers.get(b, ()):
            if rid not in cone:
                cone[rid] = records[rid]
                stack.extend(records[rid]["inputs"])
    return cone


def _downstream(records, starts):
    """直接受影响批次集合的下游闭包（含起点自身）。"""
    adj = _batch_edges(records)
    seen = set(starts)
    stack = list(starts)
    while stack:
        b = stack.pop()
        for o in adj.get(b, ()):
            if o not in seen:
                seen.add(o)
                stack.append(o)
    return seen


def _basis_detail(cone):
    """依据明细：全部上游加工记录的内容与修订身份（按记录标识排序）。"""
    return [{"record_id": rid,
             "revision": cone[rid]["revision"],
             "inputs": list(cone[rid]["inputs"]),
             "outputs": list(cone[rid]["outputs"])}
            for rid in sorted(cone)]


def _evidence_token(batch_id, risk_id, risk_direct_sorted, cone):
    """凭证：对 (批次, 风险登记, 全部上游记录内容与修订身份) 的规范化 SHA-256。

    任何上游记录增删、内容或修订身份变化、风险登记差异都会改变令牌；
    修订身份全局不复用，故令牌一旦失效即永久失效。
    """
    payload = {
        "batch": batch_id,
        "risk_id": risk_id,
        "risk_direct": list(risk_direct_sorted),
        "basis": [[rid, cone[rid]["revision"],
                   list(cone[rid]["inputs"]), list(cone[rid]["outputs"])]
                  for rid in sorted(cone)],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return "ev-" + hashlib.sha256(blob).hexdigest()


# --------------------------------------------------------------------------- #
# 存储与业务
# --------------------------------------------------------------------------- #
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS batches (          -- 批次：只增不删，身份不可复用
    batch_id   TEXT PRIMARY KEY,
    created_op INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS records (          -- 当前存活加工记录（删除即移出）
    record_id   TEXT PRIMARY KEY,
    revision    TEXT NOT NULL,                -- 全局不复用修订身份
    inputs_json  TEXT NOT NULL,               -- JSON 数组（排序规范化）
    outputs_json TEXT NOT NULL,
    updated_op  INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS risks (            -- 风险：直接受影响集合不可修改；
    risk_id      TEXT PRIMARY KEY,            -- 撤销保留行，身份不得复用
    batches_json TEXT NOT NULL,
    revoked      INTEGER NOT NULL DEFAULT 0,
    created_op   INTEGER NOT NULL,
    revoked_op   INTEGER
);
CREATE TABLE IF NOT EXISTS dispositions (     -- 处置：历史只增；失效就地裁决标记
    disp_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id          TEXT NOT NULL,
    risk_id           TEXT NOT NULL,
    evidence          TEXT NOT NULL,          -- 确认时凭证
    basis_json        TEXT NOT NULL,          -- 确认时依据 {record_id: revision}
    risk_batches_json TEXT NOT NULL,          -- 风险登记快照
    status            TEXT NOT NULL,          -- VALID / INVALID
    invalid_reason    TEXT,                   -- BASIS_CHANGED / RISK_REVOKED
    created_op        INTEGER NOT NULL,
    invalidated_op    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_disp_batch_risk ON dispositions(batch_id, risk_id);
"""


class Store:
    """质量系统后端。线程安全；所有公开方法要么完整提交，要么整体回滚。"""

    def __init__(self, db_path):
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, isolation_level=None,
                                     check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=FULL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        with self._lock:
            self._conn.executescript(_SCHEMA)
            for k in ("rev_seq", "op_seq"):
                self._conn.execute(
                    "INSERT OR IGNORE INTO meta(key, value) VALUES (?, 0)", (k,))

    def close(self):
        with self._lock:
            self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    # ------------------------------------------------------------------ #
    # 事务与内部工具
    # ------------------------------------------------------------------ #
    @contextmanager
    def _txn(self):
        """串行写事务：锁内 BEGIN IMMEDIATE，成功提交、异常回滚。"""
        with self._lock:
            cur = self._conn.cursor()
            cur.execute("BEGIN IMMEDIATE")
            try:
                yield cur
            except Exception:
                cur.execute("ROLLBACK")
                raise
            else:
                cur.execute("COMMIT")

    @staticmethod
    def _next_seq(cur, key):
        cur.execute("UPDATE meta SET value = value + 1 WHERE key = ?", (key,))
        cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
        return cur.fetchone()[0]

    def _next_revision(self, cur):
        return "rev-%012d" % self._next_seq(cur, "rev_seq")

    @staticmethod
    def _load_records(cur):
        cur.execute("SELECT record_id, revision, inputs_json, outputs_json "
                    "FROM records")
        return {rid: {"revision": rev,
                      "inputs": json.loads(ins),
                      "outputs": json.loads(outs)}
                for rid, rev, ins, outs in cur.fetchall()}

    @staticmethod
    def _load_risks(cur):
        cur.execute("SELECT risk_id, batches_json, revoked, created_op, "
                    "revoked_op FROM risks ORDER BY risk_id")
        return [{"risk_id": rid, "batches": json.loads(b),
                 "revoked": bool(rv), "created_op": c, "revoked_op": ro}
                for rid, b, rv, c, ro in cur.fetchall()]

    @staticmethod
    def _require_batch(cur, batch_id):
        cur.execute("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,))
        if cur.fetchone() is None:
            raise QmsError("BATCH_NOT_FOUND", "批次未登记",
                           {"batch_id": batch_id})

    @staticmethod
    def _require_risk(cur, risk_id):
        cur.execute("SELECT batches_json, revoked FROM risks WHERE risk_id = ?",
                    (risk_id,))
        row = cur.fetchone()
        if row is None:
            raise QmsError("RISK_NOT_FOUND", "风险不存在", {"risk_id": risk_id})
        return {"batches": json.loads(row[0]), "revoked": bool(row[1])}

    def _require_batches(self, cur, ids):
        missing = [b for b in ids if not self._batch_exists(cur, b)]
        if missing:
            raise QmsError("BATCH_NOT_FOUND", "引用的批次未登记",
                           {"missing": sorted(missing)})

    @staticmethod
    def _batch_exists(cur, batch_id):
        cur.execute("SELECT 1 FROM batches WHERE batch_id = ?", (batch_id,))
        return cur.fetchone() is not None

    def _sweep_invalidations(self, cur, op):
        """失效裁决（在变更事务内调用，与变更原子提交）。

        对每个有效处置复核：风险已撤销 -> RISK_REVOKED；该批次当前上游依据
        （记录集合 + 修订身份）与确认时快照不一致 -> BASIS_CHANGED。
        无关变化不改变依据快照，故不影响处置。
        """
        records = self._load_records(cur)
        cur.execute("SELECT disp_id, batch_id, risk_id, basis_json "
                    "FROM dispositions WHERE status = ?", (VALID,))
        rows = cur.fetchall()
        for disp_id, batch_id, risk_id, basis_json in rows:
            cur.execute("SELECT revoked FROM risks WHERE risk_id = ?",
                        (risk_id,))
            r = cur.fetchone()
            if r is None or r[0]:
                reason = REASON_RISK_REVOKED
            else:
                cone = _upstream_cone(records, batch_id)
                current = {rid: rec["revision"] for rid, rec in cone.items()}
                reason = (None if current == json.loads(basis_json)
                          else REASON_BASIS_CHANGED)
            if reason:
                cur.execute("UPDATE dispositions SET status = ?, "
                            "invalid_reason = ?, invalidated_op = ? "
                            "WHERE disp_id = ?",
                            (INVALID, reason, op, disp_id))

    # ------------------------------------------------------------------ #
    # 批次登记
    # ------------------------------------------------------------------ #
    def register_batch(self, batch_id):
        """登记批次。登记后不可删除、身份不可复用。"""
        _check_id(batch_id, "batch_id")
        with self._txn() as cur:
            if self._batch_exists(cur, batch_id):
                raise QmsError("BATCH_EXISTS", "批次已登记，身份不可复用",
                               {"batch_id": batch_id})
            op = self._next_seq(cur, "op_seq")
            cur.execute("INSERT INTO batches(batch_id, created_op) "
                        "VALUES (?, ?)", (batch_id, op))
            return {"ok": True, "op": op, "batch_id": batch_id}

    # ------------------------------------------------------------------ #
    # 加工谱系订正
    # ------------------------------------------------------------------ #
    def _write_record(self, cur, record_id, inputs, outputs, must_exist):
        cur.execute("SELECT 1 FROM records WHERE record_id = ?", (record_id,))
        exists = cur.fetchone() is not None
        if must_exist and not exists:
            raise QmsError("RECORD_NOT_FOUND", "加工记录不存在",
                           {"record_id": record_id})
        if not must_exist and exists:
            raise QmsError("RECORD_EXISTS",
                           "加工记录已存在；整体替换请用 replace_record",
                           {"record_id": record_id})
        self._require_batches(cur, list(inputs) + list(outputs))
        op = self._next_seq(cur, "op_seq")
        rev = self._next_revision(cur)  # 每次成功写入均生成不复用修订身份
        cur.execute(
            "INSERT INTO records(record_id, revision, inputs_json, "
            "outputs_json, updated_op) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(record_id) DO UPDATE SET revision = excluded.revision,"
            " inputs_json = excluded.inputs_json,"
            " outputs_json = excluded.outputs_json,"
            " updated_op = excluded.updated_op",
            (record_id, rev, json.dumps(inputs, ensure_ascii=False),
             json.dumps(outputs, ensure_ascii=False), op))
        _assert_acyclic(self._load_records(cur))  # 最终关系不得成环
        self._sweep_invalidations(cur, op)
        return op, rev

    def add_record(self, record_id, inputs, outputs):
        """新增加工记录。每个投入与全部产出建立有向关系。"""
        _check_id(record_id, "record_id")
        ins = _check_id_set(inputs, "inputs")
        outs = _check_id_set(outputs, "outputs")
        with self._txn() as cur:
            op, rev = self._write_record(cur, record_id, ins, outs, False)
            return {"ok": True, "op": op, "record_id": record_id,
                    "revision": rev}

    def replace_record(self, record_id, inputs, outputs):
        """整体替换加工记录内容；同内容替换也算修订，生成新修订身份。"""
        _check_id(record_id, "record_id")
        ins = _check_id_set(inputs, "inputs")
        outs = _check_id_set(outputs, "outputs")
        with self._txn() as cur:
            op, rev = self._write_record(cur, record_id, ins, outs, True)
            return {"ok": True, "op": op, "record_id": record_id,
                    "revision": rev}

    def delete_record(self, record_id):
        """删除加工记录。其修订身份随之退役，重建不得沿用。"""
        _check_id(record_id, "record_id")
        with self._txn() as cur:
            cur.execute("SELECT 1 FROM records WHERE record_id = ?",
                        (record_id,))
            if cur.fetchone() is None:
                raise QmsError("RECORD_NOT_FOUND", "加工记录不存在",
                               {"record_id": record_id})
            op = self._next_seq(cur, "op_seq")
            cur.execute("DELETE FROM records WHERE record_id = ?",
                        (record_id,))
            self._sweep_invalidations(cur, op)
            return {"ok": True, "op": op, "record_id": record_id}

    # ------------------------------------------------------------------ #
    # 风险登记与撤销
    # ------------------------------------------------------------------ #
    def register_risk(self, risk_id, batch_ids):
        """登记风险：非空直接受影响批次集合，登记后不可修改；身份不得复用。"""
        _check_id(risk_id, "risk_id")
        ids = _check_id_set(batch_ids, "batch_ids")
        with self._txn() as cur:
            cur.execute("SELECT 1 FROM risks WHERE risk_id = ?", (risk_id,))
            if cur.fetchone() is not None:
                raise QmsError("RISK_EXISTS",
                               "风险身份已存在；撤销后亦不得复用",
                               {"risk_id": risk_id})
            self._require_batches(cur, ids)
            op = self._next_seq(cur, "op_seq")
            cur.execute("INSERT INTO risks(risk_id, batches_json, revoked, "
                        "created_op) VALUES (?, ?, 0, ?)",
                        (risk_id, json.dumps(ids, ensure_ascii=False), op))
            return {"ok": True, "op": op, "risk_id": risk_id,
                    "directly_affected": ids}

    def revoke_risk(self, risk_id):
        """撤销风险。撤销后身份仍不得复用；其有效处置裁决为 RISK_REVOKED。"""
        _check_id(risk_id, "risk_id")
        with self._txn() as cur:
            risk = self._require_risk(cur, risk_id)
            if risk["revoked"]:
                raise QmsError("RISK_ALREADY_REVOKED", "风险已撤销",
                               {"risk_id": risk_id})
            op = self._next_seq(cur, "op_seq")
            cur.execute("UPDATE risks SET revoked = 1, revoked_op = ? "
                        "WHERE risk_id = ?", (op, risk_id))
            self._sweep_invalidations(cur, op)
            return {"ok": True, "op": op, "risk_id": risk_id}

    # ------------------------------------------------------------------ #
    # 查询：当前风险、有效处置、未处置/失效原因、放行判定、依据凭证
    # ------------------------------------------------------------------ #
    def query_batch(self, batch_id):
        _check_id(batch_id, "batch_id")
        with self._lock:
            cur = self._conn.cursor()
            self._require_batch(cur, batch_id)
            records = self._load_records(cur)
            cone = _upstream_cone(records, batch_id)
            cone_inputs = set()
            for rec in cone.values():
                cone_inputs.update(rec["inputs"])
            basis_detail = _basis_detail(cone)

            current_risks = []
            releasable = True
            for risk in self._load_risks(cur):
                if risk["revoked"]:
                    continue
                if batch_id not in _downstream(records, risk["batches"]):
                    continue
                token = _evidence_token(batch_id, risk["risk_id"],
                                        risk["batches"], cone)
                via = sorted(d for d in risk["batches"]
                             if d == batch_id or d in cone_inputs)
                disps = self._dispositions_of(cur, batch_id, risk["risk_id"])
                valid = [d for d in disps if d["status"] == VALID]
                disposed = bool(valid)
                reason = None
                if not disposed:
                    releasable = False
                    if not disps:
                        reason = {"code": "NEVER_DISPOSED",
                                  "message": "该风险尚无处置记录"}
                    else:
                        last = disps[-1]
                        reason = {"code": "DISPOSITION_INVALIDATED",
                                  "invalid_reason": last["invalid_reason"],
                                  "invalidated_op": last["invalidated_op"],
                                  "message": "先前处置已失效：%s"
                                             % last["invalid_reason"]}
                current_risks.append({
                    "risk_id": risk["risk_id"],
                    "directly_affected": list(risk["batches"]),
                    "affected_via": via,
                    "disposed": disposed,
                    "disposition": valid[0] if valid else None,
                    "undisposed_reason": reason,
                    "evidence": {
                        "token": token,
                        "batch": batch_id,
                        "risk": {"risk_id": risk["risk_id"],
                                 "directly_affected": list(risk["batches"]),
                                 "revoked": False,
                                 "created_op": risk["created_op"]},
                        "basis": basis_detail,
                    },
                })

            cur.execute("SELECT * FROM dispositions WHERE batch_id = ? "
                        "ORDER BY disp_id", (batch_id,))
            history = [self._disp_summary(r) for r in cur.fetchall()]
            return {
                "batch": batch_id,
                "releasable": releasable,
                "current_risks": current_risks,
                "valid_dispositions": [d for d in history
                                       if d["status"] == VALID],
                "disposition_history": history,
            }

    @staticmethod
    def _dispositions_of(cur, batch_id, risk_id):
        cur.execute("SELECT * FROM dispositions WHERE batch_id = ? AND "
                    "risk_id = ? ORDER BY disp_id", (batch_id, risk_id))
        return [Store._disp_summary(r) for r in cur.fetchall()]

    @staticmethod
    def _disp_summary(row):
        (disp_id, batch_id, risk_id, evidence, _basis, _rb, status,
         reason, created_op, invalidated_op) = row
        return {"disposition_id": disp_id, "batch_id": batch_id,
                "risk_id": risk_id, "evidence": evidence, "status": status,
                "invalid_reason": reason, "created_op": created_op,
                "invalidated_op": invalidated_op}

    # ------------------------------------------------------------------ #
    # 人工确认处置
    # ------------------------------------------------------------------ #
    def confirm_disposition(self, batch_id, risk_id, evidence):
        """人工确认处置：提交批次、风险及凭证。

        仅当风险仍影响该批次且凭证当前有效才成功；有效凭证重复确认成功
        （幂等）。凭证失效须重新查询获取新凭证后再确认。
        """
        _check_id(batch_id, "batch_id")
        _check_id(risk_id, "risk_id")
        _check_id(evidence, "evidence")
        with self._txn() as cur:
            self._require_batch(cur, batch_id)
            risk = self._require_risk(cur, risk_id)
            if risk["revoked"]:
                raise QmsError("RISK_REVOKED", "风险已撤销，不可处置",
                               {"risk_id": risk_id})
            records = self._load_records(cur)
            if batch_id not in _downstream(records, risk["batches"]):
                raise QmsError("RISK_NOT_APPLICABLE",
                               "该风险当前不影响此批次",
                               {"batch_id": batch_id, "risk_id": risk_id})
            cone = _upstream_cone(records, batch_id)
            token = _evidence_token(batch_id, risk_id, risk["batches"], cone)
            if token != evidence:
                raise QmsError("EVIDENCE_STALE",
                               "凭证已失效或与当前状态不符，请重新查询",
                               {"batch_id": batch_id, "risk_id": risk_id})
            cur.execute("SELECT disp_id FROM dispositions WHERE batch_id = ? "
                        "AND risk_id = ? AND status = ?",
                        (batch_id, risk_id, VALID))
            row = cur.fetchone()
            if row is not None:  # 有效凭证重复确认成功（幂等）
                return {"ok": True, "already": True,
                        "disposition_id": row[0], "evidence": token}
            op = self._next_seq(cur, "op_seq")
            basis = {rid: rec["revision"] for rid, rec in cone.items()}
            cur.execute(
                "INSERT INTO dispositions(batch_id, risk_id, evidence, "
                "basis_json, risk_batches_json, status, created_op) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (batch_id, risk_id, token,
                 json.dumps(basis, ensure_ascii=False, sort_keys=True),
                 json.dumps(risk["batches"], ensure_ascii=False),
                 VALID, op))
            return {"ok": True, "already": False,
                    "disposition_id": cur.lastrowid, "op": op,
                    "evidence": token}


# --------------------------------------------------------------------------- #
# 命令行（JSON Lines）：python3 qms.py <db路径>
# 每行一个请求 {"op": <方法名>, ...参数}；输出 {"result": ...} 或 {"error": ...}
# --------------------------------------------------------------------------- #
_OPS = {"register_batch", "add_record", "replace_record", "delete_record",
        "register_risk", "revoke_risk", "query_batch", "confirm_disposition"}

if __name__ == "__main__":
    import sys

    store = Store(sys.argv[1] if len(sys.argv) > 1 else "qms.db")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
            op = req.pop("op", None)
            if op not in _OPS:
                raise QmsError("UNKNOWN_OP", "未知操作", {"op": op})
            out = getattr(store, op)(**req)
            print(json.dumps({"result": out}, ensure_ascii=False), flush=True)
        except QmsError as e:
            print(json.dumps(e.to_dict(), ensure_ascii=False), flush=True)
        except Exception as e:  # 请求格式错误等
            print(json.dumps({"error": {"code": "BAD_REQUEST",
                                        "message": str(e),
                                        "details": {}}},
                             ensure_ascii=False), flush=True)

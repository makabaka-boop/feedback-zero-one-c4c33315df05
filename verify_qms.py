# -*- coding: utf-8 -*-
"""
verify_qms.py — qms.py 的独立验证脚本（仅通过公开接口核对，不触碰内部）。

覆盖：
  S1 风险传播与局部解除（含多路径只计一次、直接受影响批次自身纳入）
  S2 修订身份规则与“依据恢复不复活”（同内容替换、删除重建、无关变化）
  S3 交错确认的一致性（双风险交错、无关/相关变化、风险撤销）
  S4 持久化与重启（重启仅见完整已提交状态，序号延续）
  S5 入参与状态校验（拒绝须报真实原因且不改状态）
  S6 多线程交错确认（串行等价：只允许成功或 EVIDENCE_STALE）

运行：python3 verify_qms.py    全部通过时退出码 0。
"""

import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qms import Store, QmsError  # noqa: E402

CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    if not cond:
        raise AssertionError("核对失败: " + label)
    print("  ok - %s" % label)


def expect_err(code, fn, *args, **kw):
    """期望以指定错误码拒绝；返回异常对象。"""
    try:
        fn(*args, **kw)
    except QmsError as e:
        check(e.code == code, "拒绝错误码 %s（期望 %s）" % (e.code, code))
        return e
    raise AssertionError("期望拒绝 %s，实际成功" % code)


def evidence_of(store, batch, risk_id):
    q = store.query_batch(batch)
    for r in q["current_risks"]:
        if r["risk_id"] == risk_id:
            return r["evidence"]["token"]
    raise AssertionError("批次 %s 当前无风险 %s" % (batch, risk_id))


def risk_entry(store, batch, risk_id):
    q = store.query_batch(batch)
    for r in q["current_risks"]:
        if r["risk_id"] == risk_id:
            return r
    return None


# --------------------------------------------------------------------------- #
def scenario_propagation_and_local_disposition():
    print("S1 风险传播与局部解除")
    s = Store(":memory:")
    for b in ["A", "B", "C", "D", "F", "Z"]:
        s.register_batch(b)
    # 菱形谱系：A->B, A->F, {B,F}->C, C->D
    s.add_record("M1", ["A"], ["B"])
    s.add_record("M2", ["A"], ["F"])
    s.add_record("M3", ["B", "F"], ["C"])
    s.add_record("M4", ["C"], ["D"])
    s.register_risk("R1", ["A"])

    qa = s.query_batch("A")
    check([r["risk_id"] for r in qa["current_risks"]] == ["R1"],
          "直接受影响批次 A 自身纳入风险判断")
    check(qa["current_risks"][0]["evidence"]["basis"] == [],
          "A 无上游加工记录，依据为空集")
    check(qa["releasable"] is False, "有风险未处置不可放行")
    check(qa["current_risks"][0]["undisposed_reason"]["code"] == "NEVER_DISPOSED",
          "未处置原因为 NEVER_DISPOSED")

    qc = s.query_batch("C")
    check(len(qc["current_risks"]) == 1 and
          qc["current_risks"][0]["risk_id"] == "R1",
          "多条路径到达只计同一风险（A 经 B、F 两路传到 C）")
    check(qc["current_risks"][0]["affected_via"] == ["A"],
          "追溯来源为直接受影响批次 A")
    basis_ids = [b["record_id"] for b in qc["current_risks"][0]["evidence"]["basis"]]
    check(basis_ids == ["M1", "M2", "M3"],
          "凭证绑定 C 的全部上游加工记录 M1,M2,M3（不含下游 M4）")
    check(all(b["revision"].startswith("rev-") for b in
              qc["current_risks"][0]["evidence"]["basis"]),
          "依据含各记录修订身份")

    qz = s.query_batch("Z")
    check(qz["current_risks"] == [] and qz["releasable"] is True,
          "无风险批次直接可放行")

    # 局部解除：处置 B 不影响 C/D
    r1 = s.confirm_disposition("B", "R1", evidence_of(s, "B", "R1"))
    check(r1["ok"] and r1["already"] is False, "B 首次确认处置成功")
    r2 = s.confirm_disposition("B", "R1", evidence_of(s, "B", "R1"))
    check(r2["ok"] and r2["already"] is True and
          r2["disposition_id"] == r1["disposition_id"],
          "有效凭证重复确认成功且幂等")
    check(s.query_batch("B")["releasable"] is True, "B 处置后可放行")
    check(s.query_batch("C")["releasable"] is False,
          "处置只解除指定批次：C 仍受 R1 影响")
    check(s.query_batch("D")["releasable"] is False,
          "处置不阻断向下游传播：D 仍受 R1 影响")
    check(len(s.query_batch("B")["valid_dispositions"]) == 1,
          "查询返回 B 的有效处置")

    for b in ("C", "D"):
        s.confirm_disposition(b, "R1", evidence_of(s, b, "R1"))
    check(s.query_batch("C")["releasable"] is True and
          s.query_batch("D")["releasable"] is True,
          "C、D 各自确认后放行")

    expect_err("RISK_NOT_APPLICABLE", s.confirm_disposition, "Z", "R1", "ev-x")
    check(True, "风险不影响的批次确认被拒绝 RISK_NOT_APPLICABLE")
    expect_err("EVIDENCE_STALE", s.confirm_disposition, "C", "R1", "ev-bogus")
    check(True, "伪造凭证确认被拒绝 EVIDENCE_STALE")
    s.close()


# --------------------------------------------------------------------------- #
def scenario_revision_and_no_revival():
    print("S2 修订身份规则与依据恢复不复活")
    s = Store(":memory:")
    for b in ["A", "B", "Z", "Y"]:
        s.register_batch(b)
    r1 = s.add_record("M1", ["A"], ["B"])["revision"]
    r2 = s.replace_record("M1", ["A"], ["B"])["revision"]
    check(r1 != r2, "同内容替换也算修订，生成新修订身份")
    s.register_risk("R", ["A"])

    e1 = evidence_of(s, "B", "R")
    s.confirm_disposition("B", "R", e1)
    check(s.query_batch("B")["releasable"] is True, "处置后 B 可放行")

    r3 = s.replace_record("M1", ["A"], ["B"])["revision"]
    check(len({r1, r2, r3}) == 3, "修订身份持续不复用")
    e = risk_entry(s, "B", "R")
    check(e["disposed"] is False and
          e["undisposed_reason"]["code"] == "DISPOSITION_INVALIDATED" and
          e["undisposed_reason"]["invalid_reason"] == "BASIS_CHANGED",
          "依据修订身份改变使处置失效（BASIS_CHANGED）")
    check(s.query_batch("B")["releasable"] is False, "失效后不可放行")
    expect_err("EVIDENCE_STALE", s.confirm_disposition, "B", "R", e1)
    check(True, "旧凭证确认被拒绝 EVIDENCE_STALE")

    # 删除后重建：不得沿用旧身份；恢复相同关系也不复活
    s.delete_record("M1")
    r4 = s.add_record("M1", ["A"], ["B"])["revision"]
    check(r4 not in {r1, r2, r3}, "删除后重建不得沿用旧修订身份")
    e = risk_entry(s, "B", "R")
    check(e["disposed"] is False, "恢复相同关系不复活旧处置")
    expect_err("EVIDENCE_STALE", s.confirm_disposition, "B", "R", e1)
    check(True, "恢复相同关系不复活旧凭证")
    e2 = evidence_of(s, "B", "R")
    check(e2 != e1, "重新查询得到新凭证")
    s.confirm_disposition("B", "R", e2)
    check(s.query_batch("B")["releasable"] is True,
          "失效后重新查询并确认，恢复放行")

    # 无关变化不影响处置
    s.add_record("M9", ["Z"], ["Y"])
    check(s.query_batch("B")["releasable"] is True,
          "无关变化（B 依据之外）不影响处置")
    again = s.confirm_disposition("B", "R", e2)
    check(again["already"] is True, "无关变化后旧凭证仍有效，重复确认成功")

    # 直接受影响批次自身：空依据 -> 新增上游记录使其依据增长而失效
    s.confirm_disposition("A", "R", evidence_of(s, "A", "R"))
    check(s.query_batch("A")["releasable"] is True, "A 自身处置后放行")
    s.add_record("M10", ["Z"], ["A"])
    check(risk_entry(s, "A", "R")["disposed"] is False,
          "A 的上游依据集合由空变非空，处置失效")
    check(risk_entry(s, "B", "R")["disposed"] is False,
          "B 的依据集合随之改变（M10 进入上游），处置亦失效")
    s.close()


# --------------------------------------------------------------------------- #
def scenario_interleaved_confirmation():
    print("S3 交错确认的一致性")
    s = Store(":memory:")
    for b in ["A", "B", "C", "X", "Y"]:
        s.register_batch(b)
    s.add_record("M1", ["A"], ["B"])
    s.add_record("M2", ["B"], ["C"])
    s.register_risk("R1", ["A"])
    s.register_risk("R2", ["B"])

    e1, e2 = evidence_of(s, "C", "R1"), evidence_of(s, "C", "R2")
    check(e1 != e2, "不同风险的凭证不同")
    s.confirm_disposition("C", "R1", e1)

    s.add_record("M8", ["X"], ["Y"])          # 无关变化
    r = s.confirm_disposition("C", "R2", e2)  # 交错：用变化前的凭证
    check(r["ok"], "无关变化后另一风险的既有凭证仍可有效确认")
    check(s.query_batch("C")["releasable"] is True, "两风险均处置，放行")

    s.replace_record("M2", ["B"], ["C"])      # 相关变化（同内容）
    expect_err("EVIDENCE_STALE", s.confirm_disposition, "C", "R2", e2)
    check(True, "相关变化使既有凭证失效")
    q = s.query_batch("C")
    check(all(not x["disposed"] for x in q["current_risks"]),
          "凭证绑定全部上游依据：R1、R2 的处置同时失效")
    check(q["releasable"] is False, "失效后不可放行")

    n1, n2 = evidence_of(s, "C", "R1"), evidence_of(s, "C", "R2")
    check(n1 != e1 and n2 != e2, "重新查询获得新凭证")
    expect_err("EVIDENCE_STALE", s.confirm_disposition, "C", "R2", n1)
    check(True, "张冠李戴的凭证被拒绝")
    s.confirm_disposition("C", "R1", n1)
    s.confirm_disposition("C", "R2", n2)
    check(s.query_batch("C")["releasable"] is True, "重新确认后放行")

    # 风险撤销：处置裁决为 RISK_REVOKED；身份不得复用
    s.revoke_risk("R1")
    q = s.query_batch("C")
    check([x["risk_id"] for x in q["current_risks"]] == ["R2"],
          "撤销后 R1 不再是当前风险")
    check(q["releasable"] is True, "R2 处置仍有效，放行")
    gone = [h for h in q["disposition_history"]
            if h["risk_id"] == "R1" and h["status"] == "INVALID"
            and h["invalid_reason"] == "RISK_REVOKED"]
    check(len(gone) >= 1, "R1 的处置被裁决为 RISK_REVOKED 失效")
    expect_err("RISK_REVOKED", s.confirm_disposition, "C", "R1", n1)
    check(True, "已撤销风险确认被拒绝 RISK_REVOKED")
    expect_err("RISK_EXISTS", s.register_risk, "R1", ["A"])
    check(True, "风险身份撤销后不得复用")
    s.close()


# --------------------------------------------------------------------------- #
def scenario_persistence_restart():
    print("S4 持久化与重启")
    path = os.path.join(tempfile.mkdtemp(prefix="qms-"), "qms.db")
    s1 = Store(path)
    s1.register_batch("A")
    s1.register_batch("B")
    rev = s1.add_record("M1", ["A"], ["B"])["revision"]
    s1.register_risk("R", ["A"])
    ev = evidence_of(s1, "B", "R")
    s1.confirm_disposition("B", "R", ev)
    s1.close()

    s2 = Store(path)  # 重启：仅见完整已提交状态
    q = s2.query_batch("B")
    check(q["releasable"] is True and
          q["valid_dispositions"][0]["evidence"] == ev,
          "重启后已提交的处置与凭证仍然有效")
    again = s2.confirm_disposition("B", "R", ev)
    check(again["already"] is True, "重启后重复确认幂等成功")
    rev2 = s2.replace_record("M1", ["A"], ["B"])["revision"]
    check(int(rev2[4:]) > int(rev[4:]), "重启后修订序号延续、不复用")
    check(s2.query_batch("B")["releasable"] is False,
          "重启后变更仍触发失效裁决")
    s2.close()

    s3 = Store(path)
    check(s3.query_batch("B")["current_risks"][0]["undisposed_reason"]
          ["invalid_reason"] == "BASIS_CHANGED",
          "失效裁决已原子持久化，重启后仍可见")
    s3.close()


# --------------------------------------------------------------------------- #
def scenario_validation_rejection():
    print("S5 入参与状态校验（拒绝不改状态）")
    s = Store(":memory:")
    s.register_batch("A")
    s.register_batch("B")
    expect_err("BATCH_EXISTS", s.register_batch, "A")
    expect_err("INVALID_ID", s.register_batch, "")
    expect_err("INVALID_ID", s.register_batch, 123)
    expect_err("EMPTY_SET", s.add_record, "M1", [], ["B"])
    expect_err("DUPLICATE_ID", s.add_record, "M1", ["A", "A"], ["B"])
    expect_err("BATCH_NOT_FOUND", s.add_record, "M1", ["A"], ["ZZ"])
    s.add_record("M1", ["A"], ["B"])
    expect_err("RECORD_EXISTS", s.add_record, "M1", ["A"], ["B"])
    expect_err("RECORD_NOT_FOUND", s.replace_record, "MX", ["A"], ["B"])
    expect_err("RECORD_NOT_FOUND", s.delete_record, "MX")
    expect_err("CYCLE", s.add_record, "M2", ["B"], ["A"])
    expect_err("CYCLE", s.add_record, "M3", ["B"], ["B"])
    check(True, "成环（含自环）被拒绝；返工不得复用旧批次")
    s.register_batch("B2")
    s.add_record("M4", ["B"], ["B2"])
    check(True, "返工登记新批次 B2 后允许")

    expect_err("EMPTY_SET", s.register_risk, "R1", [])
    expect_err("BATCH_NOT_FOUND", s.register_risk, "R1", ["ZZ"])
    s.register_risk("R1", ["A"])
    expect_err("RISK_EXISTS", s.register_risk, "R1", ["A"])
    expect_err("RISK_NOT_FOUND", s.revoke_risk, "RX")
    expect_err("RISK_NOT_FOUND", s.confirm_disposition, "B", "RX", "ev-x")
    expect_err("BATCH_NOT_FOUND", s.confirm_disposition, "BX", "R1", "ev-x")
    expect_err("BATCH_NOT_FOUND", s.query_batch, "BX")

    # 拒绝不改状态：谱系与风险判断保持拒绝前样子
    q = s.query_batch("B2")
    basis = [b["record_id"] for b in
             q["current_risks"][0]["evidence"]["basis"]]
    check(basis == ["M1", "M4"], "一连串拒绝后谱系状态未被破坏")
    s.revoke_risk("R1")
    expect_err("RISK_ALREADY_REVOKED", s.revoke_risk, "R1")
    check(s.query_batch("B2")["releasable"] is True, "撤销后无当前风险可放行")
    s.close()


# --------------------------------------------------------------------------- #
def scenario_threaded_interleave():
    print("S6 多线程交错确认（串行等价）")
    s = Store(":memory:")
    s.register_batch("P")
    s.register_batch("Q")
    s.add_record("M", ["P"], ["Q"])
    s.register_risk("RP", ["P"])

    errors, stops = [], threading.Event()

    def mutator():
        try:
            for _ in range(40):
                s.replace_record("M", ["P"], ["Q"])
        except Exception as e:  # noqa: BLE001
            errors.append(("mutator", e))
        finally:
            stops.set()

    def confirmer():
        try:
            while not stops.is_set():
                try:
                    ev = evidence_of(s, "Q", "RP")
                    s.confirm_disposition("Q", "RP", ev)
                except QmsError as e:
                    if e.code != "EVIDENCE_STALE":
                        errors.append(("confirmer", e))
        except Exception as e:  # noqa: BLE001
            errors.append(("confirmer", e))

    threads = [threading.Thread(target=mutator)] + \
              [threading.Thread(target=confirmer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    check(not errors, "交错执行无越界异常（仅允许成功或 EVIDENCE_STALE）：%r"
          % (errors[:1],))
    ev = evidence_of(s, "Q", "RP")
    s.confirm_disposition("Q", "RP", ev)
    q = s.query_batch("Q")
    check(q["releasable"] is True and len(q["valid_dispositions"]) == 1,
          "交错结束后状态一致：恰好一个有效处置且可放行")
    s.close()


# --------------------------------------------------------------------------- #
def main():
    scenarios = [
        scenario_propagation_and_local_disposition,
        scenario_revision_and_no_revival,
        scenario_interleaved_confirmation,
        scenario_persistence_restart,
        scenario_validation_rejection,
        scenario_threaded_interleave,
    ]
    for fn in scenarios:
        fn()
    print("\n全部 %d 项核对通过。" % CHECKS)


if __name__ == "__main__":
    main()

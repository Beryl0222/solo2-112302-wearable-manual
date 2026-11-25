"""共享决定账本的行为测试。

覆盖：
* 维度分开：排除提醒不等于停用趋势。
* 受众消歧：两个“阿敏”与未登记的“妈”。
* 转述、反问、请求再问都不构成授权。
* 迟到确认与已改变的范围/指代。
* 每份材料的真实来源与受众裁定。
* 组合推断时只删改上下文，不连坐趋势。
* 回执保留、入口收紧；二次转发默认不允许。
"""
import json
import unittest
from datetime import date, datetime
from pathlib import Path

from family_share import Action, load_ledger, material_label
from family_share.cast import parse_persons, resolve_audience
from family_share.events import day_bounds, night_bounds, open_bounds, week_bounds
from family_share.model import Clarification, DecisionStatus
from family_share.service import Ledger

ROOT = Path(__file__).resolve().parents[1]


def corpus():
    return json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))


def ledger(**kwargs):
    return load_ledger(corpus(), **kwargs)


def decisions_for(led, person_id, material, action=None):
    return [
        decision
        for decision in led.decisions
        if decision.person_id == person_id
        and decision.material == material
        and (action is None or decision.action is action)
    ]


class DimensionSeparationTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger()

    def test_excluding_the_night_does_not_stop_the_week_trend(self):
        self.assertTrue(self.led.view("p2").allows("sleep_trend"))
        trend = day_bounds(date(2026, 11, 23))
        self.assertTrue(self.led.view("p2").allows("sleep_trend", trend))
        exclusions = self.led.view("p2").exclusions_for("reminder_notice")
        self.assertEqual(len(exclusions), 1)
        self.assertIn("夜间", exclusions[0].coverage.describe())

    def test_exclusion_is_recorded_as_its_own_decision(self):
        excluded = decisions_for(self.led, "p2", "reminder_notice", Action.EXCLUDE)
        self.assertEqual(len(excluded), 1)
        self.assertEqual(excluded[0].source_record, "q1")
        self.assertFalse(self.led.view("p2").scopes.get("reminder_notice"))
        # 趋势的授权仍然在
        grants = decisions_for(self.led, "p2", "sleep_trend", Action.GRANT)
        self.assertTrue(grants)

    def test_reading_window_is_separate_from_coverage(self):
        led = ledger()
        question = led.ask(
            person_id="p2",
            material="daily_average",
            coverage=open_bounds("长期"),
            read_window=day_bounds(date(2026, 11, 25)),
            prompt="日均时长能看到什么时候？",
            source_record="manual-window",
        )
        outcome = led.answer_question(question.id, affirmative=True, source_record="manual-window-ok")
        self.assertEqual(outcome.state, "bound")
        scope = led.view("p2").scopes["daily_average"]
        self.assertIsNotNone(scope.read_window)
        self.assertTrue(scope.allows(day_bounds(date(2026, 12, 1))))
        self.assertIn("读取窗口", led.explain("p2", "daily_average"))

    def test_second_forwarding_is_denied_by_default(self):
        check = self.led.forwarding_check("p2", "sleep_trend")
        self.assertFalse(check.allowed)
        self.assertIn("明确不给", check.reason)
        self.assertFalse(self.led.view("p2").scopes["sleep_trend"].forwardable)

    def test_explicit_forwarding_grant_is_needed(self):
        led = ledger()
        led.revise(
            person_id="p2",
            material="sleep_trend",
            action=Action.GRANT,
            coverage=week_bounds(date(2026, 11, 23)),
            source_record="manual-forward",
            note="允许二次转发",
        )
        # revise 本身不带 forwardable 标记：默认仍不允许。
        self.assertFalse(led.forwarding_check("p2", "sleep_trend").allowed)


class AudienceDisambiguationTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger()
        self.persons = parse_persons(corpus())

    def test_two_amíns_are_not_merged(self):
        family = resolve_audience("阿敏", self.persons)
        self.assertEqual(family.state, "ambiguous")
        self.assertEqual(set(family.person_ids), {"p2", "p3"})
        sister = resolve_audience("我姐", self.persons)
        self.assertEqual(sister.person_ids, ("p2",))
        partner = resolve_audience("运动组的阿敏", self.persons)
        self.assertEqual(partner.person_ids, ("p3",))

    def test_unregistered_mother_is_not_mapped_to_sister(self):
        resolution = resolve_audience("妈", self.persons)
        self.assertEqual(resolution.state, "unresolved")
        self.assertEqual(resolution.person_ids, ())
        self.assertNotIn("p2", resolution.person_ids)

    def test_relay_cannot_grant_the_mother_anything(self):
        self.assertFalse(self.led.view("p2").scopes.get("reminder_notice"))
        # 没有任何授权被签到未登记的母亲头上
        for decision in self.led.decisions:
            self.assertIn(decision.person_id, {"p1", "p2", "p3"})
        issues = {issue.id: issue for issue in self.led.issues()}
        self.assertIn("i-q2-mother", issues)
        self.assertIn("i-q2-relay", issues)

    def test_owner_request_with_pronoun_uses_explicit_antecedent(self):
        grants = decisions_for(self.led, "p2", "sleep_trend", Action.GRANT)
        sources = {decision.source_record for decision in grants}
        self.assertIn("r1", sources)
        self.assertIn("q1", sources)
        exclusions = decisions_for(self.led, "p2", "reminder_notice", Action.EXCLUDE)
        self.assertEqual(exclusions[0].source_record, "q1")
        # “她”没有被解析成运动搭档
        self.assertFalse(decisions_for(self.led, "p3", "reminder_notice"))

    def test_ambiguous_reminder_request_stays_one_question(self):
        open_items = self.led.pending_decisive_items()
        self.assertTrue(open_items)
        audience_questions = [item for item in open_items if "阿敏" in item.audience_text]
        self.assertEqual(len(audience_questions), 1)
        self.assertIn("运动组", audience_questions[0].audience_text)
        issues = {issue.id for issue in self.led.issues()}
        self.assertIn("i-q1-ask-again", issues)
        # “再提醒我问问”没有生成任何授权，也没有替它在两位阿敏中选人
        for decision in self.led.decisions:
            self.assertNotEqual(decision.source_record, "q1a")


class ConfirmationBindingTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger()

    def test_confirmation_binds_to_the_question_it_answers(self):
        questions = [q for q in self.led.questions.values() if q.material == "sleep_trend"]
        self.assertEqual(len(questions), 1)
        question = questions[0]
        self.assertEqual(question.person_id, "p2")
        self.assertEqual(question.state, "answered")
        reaffirm = [
            decision
            for decision in self.led.decisions
            if decision.question_id == question.id
        ]
        self.assertEqual(len(reaffirm), 1)
        self.assertEqual(reaffirm[0].action, Action.REAFFIRM)
        self.assertEqual(reaffirm[0].person_id, "p2")

    def test_late_confirmation_does_not_widen_scope(self):
        question = self.led.ask(
            person_id="p2",
            material="daily_average",
            coverage=None,
            prompt="给苑敏日均时长吗？",
            source_record="manual-1",
        )
        self.assertFalse(decisions_for(self.led, "p2", "daily_average"))
        outcome = self.led.answer_question(question.id, affirmative=True, source_record="manual-2")
        # 没有既有范围、也没有明确范围：拒绝制造无边界授权
        self.assertEqual(outcome.state, "ambiguous")
        self.assertFalse(decisions_for(self.led, "p2", "daily_average"))

    def test_stale_confirmation_after_scope_change(self):
        question = self.led.ask(
            person_id="p2",
            material="daily_average",
            coverage=day_bounds(date(2026, 11, 24)),
            prompt="给苑敏一天日均时长吗？",
            source_record="manual-3",
        )
        self.led.revise(
            person_id="p2",
            material="daily_average",
            action=Action.GRANT,
            coverage=day_bounds(date(2026, 11, 25)),
            source_record="manual-4",
        )
        outcome = self.led.answer_question(question.id, affirmative=True, source_record="manual-5")
        self.assertEqual(outcome.state, "stale")
        self.assertFalse(outcome.applied)
        self.assertEqual(
            self.led.questions[question.id].state,
            "stale",
        )

    def test_unrelated_material_change_does_not_stale_the_question(self):
        question = self.led.ask(
            person_id="p2",
            material="daily_average",
            coverage=open_bounds("长期"),
            prompt="苑敏要不要日均时长？",
            source_record="manual-6",
        )
        fingerprint = self.led.view("p2").fingerprint("daily_average")
        self.led.revise(
            person_id="p2",
            material="reminder_notice",
            action=Action.EXCLUDE,
            coverage=night_bounds(date(2026, 11, 25)),
            source_record="manual-7",
        )
        self.assertEqual(self.led.view("p2").fingerprint("daily_average"), fingerprint)
        outcome = self.led.answer_question(question.id, affirmative=True, source_record="manual-8")
        self.assertEqual(outcome.state, "bound")

    def test_confirmation_naming_a_different_person_is_mismatched(self):
        question = self.led.ask(
            person_id="p2",
            material="daily_average",
            coverage=open_bounds("长期"),
            prompt="苑敏要不要日均时长？",
            source_record="manual-9",
        )
        outcome = self.led.answer_question(
            question.id,
            affirmative=True,
            source_record="manual-10",
            explicit_person_ids=("p3",),
        )
        self.assertEqual(outcome.state, "mismatched")
        self.assertFalse(outcome.applied)

    def test_clarification_invalidating_the_referent(self):
        led = ledger()
        question = led.ask(
            person_id="p3",
            material="daily_average",
            coverage=open_bounds("长期"),
            audience_text="阿敏",
            prompt="阿敏要不要日均时长？",
            source_record="manual-11",
        )
        # 模拟一次澄清：“刚才指我姐，不是运动伙伴。”
        clarification = Clarification(
            id="c-manual",
            record_id="manual-12",
            question_ids=(question.id,),
            refers_to=("p2",),
            excludes=("p3",),
        )
        led.clarifications.append(clarification)
        led._apply_clarification(clarification)
        self.assertTrue(led.questions[question.id].referent_invalidated)
        outcome = led.answer_question(question.id, affirmative=True, source_record="manual-13")
        self.assertEqual(outcome.state, "mismatched")
        self.assertFalse(outcome.applied)
        self.assertFalse(decisions_for(led, "p3", "daily_average", Action.GRANT))

    def test_partner_negation_does_not_grant_or_revoke(self):
        question = [q for q in self.led.questions.values() if q.material == "daily_average"]
        self.assertEqual(len(question), 1)
        self.assertEqual(question[0].state, "open")
        self.assertFalse(decisions_for(self.led, "p3", "daily_average", Action.GRANT))
        self.assertFalse(decisions_for(self.led, "p3", "daily_average", Action.DENY))


class MaterialOriginTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger()

    def test_origins_are_recorded_per_material(self):
        trend = self.led.origin_of("sleep_trend")
        self.assertEqual(trend.authorization_records[0], "r1")
        summary = self.led.origin_of("weekly_summary")
        self.assertEqual(summary.authorization_records, ("r2",))
        self.assertIn("r2", summary.content_records)
        reminder = self.led.origin_of("reminder_notice")
        self.assertIn("r2", reminder.content_records)
        self.assertIn("t2", reminder.content_records)

    def test_dangling_recipient_in_original_consent_is_flagged(self):
        blocked = self.led.issues(blocking_only=True)
        ids = {issue.id for issue in blocked}
        self.assertIn("i-r1-daily-recipient", ids)
        # p4 不在登记联系人里，不能凭空出现
        self.assertNotIn("p4", self.led.persons)

    def test_why_visible_names_the_supporting_records(self):
        basis = self.led.why_visible("sleep_trend", person_id="p2")
        sources = {entry.source_record for entry in basis.basis}
        self.assertIn("r1", sources)
        self.assertIn("q1", sources)
        text = basis.describe()
        self.assertIn("本周睡眠趋势", text)
        self.assertIn("r1", text)
        self.assertIn("q4", text)


class BundleRedactionTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger()

    def test_inference_is_detected(self):
        findings = self.led.inference_findings("p2")
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.material, "reminder_notice")
        self.assertEqual(finding.reminder_moment, datetime(2026, 11, 24, 23, 35))

    def test_bundle_keeps_trend_and_redacts_only_context(self):
        bundle = self.led.compose_bundle("p2")
        materials = [item.material for item in bundle.items]
        self.assertIn("sleep_trend", materials)
        self.assertIn("weekly_summary", materials)
        for item in bundle.items:
            self.assertNotIn("23:35", item.text)
            self.assertNotIn("23 分钟", item.text)
        summary = next(item for item in bundle.items if item.material == "weekly_summary")
        self.assertTrue(summary.redactions)
        self.assertTrue(any("反推" in note for note in bundle.notes))

    def test_unrelated_exclusion_leaves_summary_intact(self):
        led = ledger(include=["p1", "p2", "p3", "r1", "r2"])
        led.revise(
            person_id="p2",
            material="reminder_notice",
            action=Action.EXCLUDE,
            coverage=night_bounds(date(2026, 11, 30)),
            source_record="manual-19",
            note="排除的是另一晚",
        )
        bundle = led.compose_bundle("p2")
        self.assertFalse(bundle.notes)
        summary = next(item for item in bundle.items if item.material == "weekly_summary")
        self.assertFalse(summary.redactions)
        self.assertIn("统计摘要", summary.text)

    def test_summary_denied_but_receipt_metadata_stays(self):
        led = ledger()
        led.revise(
            person_id="p2",
            material="weekly_summary",
            action=Action.DENY,
            source_record="manual-20",
            note="只收回摘要",
        )
        access = led.receipt_access(led.receipts[0])
        self.assertFalse(access.allowed)
        self.assertIsNone(access.text)
        self.assertIn("投递记录仍保留", access.partial_text)
        self.assertTrue(led.view("p2").allows("sleep_trend"))

    def test_no_trend_is_withheld_for_an_unrelated_person(self):
        bundle = self.led.compose_bundle("p3")
        self.assertFalse(bundle.items)
        withheld = {item.material for item in bundle.withheld}
        self.assertIn("sleep_trend", withheld)
        self.assertIn("weekly_summary", withheld)

    def test_bundle_for_person_with_trend_but_no_summary(self):
        led = ledger()
        led.revise(
            person_id="p2",
            material="weekly_summary",
            action=Action.DENY,
            source_record="manual-14",
            note="不再给摘要，但趋势不变",
        )
        bundle = led.compose_bundle("p2")
        self.assertEqual([item.material for item in bundle.items], ["sleep_trend"])
        self.assertTrue(led.view("p2").allows("sleep_trend"))
        self.assertFalse(led.view("p2").allows("weekly_summary"))
        self.assertIn("收回", led.explain("p2", "weekly_summary"))


class ReceiptTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger()

    def test_receipt_metadata_is_retained(self):
        receipt = self.led.receipts[0]
        access = self.led.receipt_access(receipt)
        self.assertFalse(access.allowed)
        self.assertIsNotNone(access.receipt)
        self.assertIn("投递记录仍保留", access.partial_text)
        self.assertIsNone(access.text)

    def test_receipt_body_available_when_authorized(self):
        led = ledger(include=["p1", "p2", "p3", "r1", "r2"])
        access = led.receipt_access(led.receipts[0], person_id="p2")
        self.assertTrue(access.allowed)
        self.assertIn("23 分钟", access.text)

    def test_receipt_with_unrelated_exclusion_still_opens(self):
        led = ledger(include=["p1", "p2", "p3", "r1", "r2"])
        led.revise(
            person_id="p2",
            material="reminder_notice",
            action=Action.EXCLUDE,
            coverage=night_bounds(date(2026, 11, 30)),
            source_record="manual-15",
            note="排除别的晚间的提醒",
        )
        access = led.receipt_access(led.receipts[0])
        # 摘要落在周二，排除的是另一晚：正文仍可展示。
        self.assertTrue(access.allowed)
        self.assertIn("23 分钟", access.text)


class CorrectionAndHistoryTest(unittest.TestCase):
    def setUp(self):
        self.led = ledger()

    def test_second_exclusion_still_leaves_trend_and_summary(self):
        led = ledger()
        led.revise(
            person_id="p2",
            material="reminder_notice",
            action=Action.EXCLUDE,
            coverage=night_bounds(date(2026, 11, 25)),
            source_record="manual-18",
            note="今晚这条也不给",
        )
        self.assertTrue(led.view("p2").allows("sleep_trend"))
        self.assertTrue(led.view("p2").allows("weekly_summary"))
        bundle = led.compose_bundle("p2")
        materials = {item.material for item in bundle.items}
        self.assertEqual(materials, {"sleep_trend", "weekly_summary"})
        summary = next(item for item in bundle.items if item.material == "weekly_summary")
        self.assertTrue(summary.redactions)

    def test_narrowing_one_person_does_not_touch_another(self):
        led = ledger()
        led.revise(
            person_id="p2",
            material="sleep_trend",
            action=Action.DENY,
            source_record="manual-16",
            note="只收回姐姐的趋势",
        )
        self.assertFalse(led.view("p2").allows("sleep_trend"))
        # 无关的摘要仍在
        self.assertTrue(led.view("p2").allows("weekly_summary"))
        # 其余亲友没有任何变化
        self.assertFalse(led.view("p3").allows("sleep_trend"))

    def test_rollback_is_recorded_with_audit_trail(self):
        trend = decisions_for(self.led, "p2", "sleep_trend", Action.GRANT)[0]
        issue = self.led.rollback(trend.id, source_record="manual-17", reason="误写受众")
        self.assertEqual(issue.kind, "rollback")
        withdrawn = [d for d in self.led.decisions if d.id == trend.id][0]
        self.assertEqual(withdrawn.status, DecisionStatus.WITHDRAWN)
        self.assertIn(trend.id, {entry.decision_id for entry in self.led.why_visible("sleep_trend", person_id="p2").basis})

    def test_clarification_effects_name_the_person(self):
        effects = self.led.clarify_effects()
        self.assertEqual(len(effects), 1)
        effect = effects[0]
        self.assertEqual(effect.refers_to, ("p2",))
        self.assertEqual(effect.excludes, ("p3",))
        self.assertIn("p2", effect.affected_persons)

    def test_reingest_is_idempotent(self):
        before = (
            len(self.led.decisions),
            len(self.led.receipts),
            len(self.led.issues()),
            self.led.version,
        )
        self.led.ingest(corpus())
        after = (
            len(self.led.decisions),
            len(self.led.receipts),
            len(self.led.issues()),
            self.led.version,
        )
        self.assertEqual(before, after)

    def test_state_round_trip(self):
        state = self.led.export_state()
        restored = Ledger.from_state(state, parse_persons(corpus()))
        self.assertEqual(restored.version, self.led.version)
        self.assertEqual(restored.compose_bundle("p2").text(), self.led.compose_bundle("p2").text())
        self.assertEqual([r.id for r in restored.receipts], [r.id for r in self.led.receipts])
        self.assertEqual({i.id for i in restored.issues()}, {i.id for i in self.led.issues()})


class NonConsentSpeechTest(unittest.TestCase):
    def test_question_is_not_a_grant(self):
        variant = corpus()
        for record in variant["records"]:
            if record["id"] == "q1":
                record["text"] = "苑云新文字|能不能把趋势发给我姐？"
        led = load_ledger(variant)
        # 只有既往回执 r1 的授权；没有任何来自 q1 的新范围
        sources = {decision.source_record for decision in led.decisions}
        self.assertNotIn("q1", sources)
        self.assertTrue(any(issue.kind == "question_not_consent" for issue in led.issues()))

    def test_relay_is_not_a_grant(self):
        variant = corpus()
        for record in variant["records"]:
            if record["id"] == "q2":
                record["text"] = "转述|我姐说想看她自己那条，我也可以把她的给她。"
        led = load_ledger(variant)
        sources = {decision.source_record for decision in led.decisions}
        self.assertNotIn("q2", sources)
        self.assertTrue(any(issue.kind == "relay_not_consent" for issue in led.issues()))


class VariantCorpusTest(unittest.TestCase):
    """贴近需求原句的变体：只看“给/姓名”就会误停用或转给家属。"""

    def setUp(self):
        self.variant = json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))
        for record in self.variant["records"]:
            if record["id"] == "q1":
                record["text"] = "苑云新文字|我把睡眠趋势给我姐看，昨晚这条提醒别给她，妈还是照原来那样。"

    def test_text_with_mother_and_tonight_does_not_stop_trend_or_reach_mother(self):
        led = load_ledger(self.variant)
        self.assertTrue(led.view("p2").allows("sleep_trend"))
        self.assertTrue(led.view("p2").allows("weekly_summary"))
        excluded = decisions_for(led, "p2", "reminder_notice", Action.EXCLUDE)
        self.assertEqual(len(excluded), 1)
        persons_seen = {decision.person_id for decision in led.decisions}
        self.assertEqual(persons_seen - {"p1", "p2", "p3"}, set())
        self.assertTrue(any(issue.kind == "unregistered_person" for issue in led.issues()))

    def test_exclusion_keeps_remainder_of_week(self):
        led = load_ledger(self.variant)
        excluded = decisions_for(led, "p2", "reminder_notice", Action.EXCLUDE)[0]
        trend = led.view("p2").scopes["sleep_trend"]
        # 排除区间与本周重叠，但没有把趋势范围整个挖空
        self.assertTrue(trend.allowed_segments if hasattr(trend, "allowed_segments") else True)
        self.assertTrue(led.allowed_segments("p2", "sleep_trend"))
        self.assertEqual(excluded.coverage.precision, "night")

    def test_tonight_exclusion_does_not_hide_last_night(self):
        variant = json.loads((ROOT / "data" / "corpus.json").read_text(encoding="utf-8"))
        for record in variant["records"]:
            if record["id"] == "q1":
                record["text"] = "苑云新文字|我把睡眠趋势给我姐看，今晚这条提醒别给她。"
        led = load_ledger(variant)
        bundle = led.compose_bundle("p2")
        summary = next(item for item in bundle.items if item.material == "weekly_summary")
        # 排除的是尚未发生的今晚，昨夜摘要不应被追溯改写
        self.assertFalse(summary.redactions)
        self.assertFalse(bundle.notes)

    def test_inference_redacts_only_the_summary_context(self):
        led = load_ledger(self.variant)
        bundle = led.compose_bundle("p2")
        materials = {item.material for item in bundle.items}
        self.assertEqual(materials, {"sleep_trend", "weekly_summary"})
        summary = next(item for item in bundle.items if item.material == "weekly_summary")
        self.assertTrue(summary.redactions)
        trend = next(item for item in bundle.items if item.material == "sleep_trend")
        self.assertFalse(trend.redactions)
        for item in bundle.items:
            self.assertNotIn("23:35", item.text)


class LabelsTest(unittest.TestCase):
    def test_material_labels_are_readable(self):
        self.assertEqual(material_label("sleep_trend"), "本周睡眠趋势")
        self.assertEqual(material_label("daily_average"), "日均时长")
        self.assertEqual(material_label("reminder_notice"), "提醒通知")


if __name__ == "__main__":
    unittest.main()

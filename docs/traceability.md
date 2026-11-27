# التتبع — مرجع حي

## مستوى المتطلبات والقرارات

[Master SRS v0.7](reference/handover-v1.6/master_srs.md) ← [سجل القرارات الوظيفية §3](reference/handover-v1.6/decisions_ledger.md) ← [مصفوفة RTM الأصلية في مواصفة القبول](reference/handover-v1.6/acceptance_tests.md). تغطي المصفوفة 241 معرف SRS متميزاً؛ يحتفظ كل صف بمرجع التصميم ومعيار الاختبار وحالة الترشيح. لا يُكرر الجدول هنا. القرارات اللاحقة (COST-001 وARCH وDEC وACC وINV وCTR وDM وBPM وغيرها) مرجعها السجل الوظيفي والقرارات التشغيلية الأحدث. لا يعني التغطية المعرفية تحقق القبول.

## ربط التنفيذ التأسيسي بمعايير القبول

| البطاقة | التصميم / معيار القبول | الكود والاختبار | دليل التنفيذ |
| --- | --- | --- | --- |
| S1-01 | [البطاقة](reference/source/ERP-02_S1-01_Task_Card_AR_v1.md) | بيئتا Compose والصحة | [الإغلاق والاعتماد](reference/source/ERP-02_G3_S1-01_Approval_AR.md) |
| S1-02 | [سجل التوافق](reference/source/ERP-02_S1-02_Compatibility_Record_AR.md) | توافق البيئة | السجل نفسه؛ لا ادعاء وحدة أعمال |
| S1-03 | [البطاقة](reference/source/ERP-02_S1-03_Task_Card_AR.md)، G3 جزئي UAT-PL-02 | [المشاريع](../tests/test_s1_03_projects.py)، [العقود](../tests/test_s1_03_contracts.py) | [الإغلاق](reference/source/ERP-02_G3_S1-03_Closure_AR.md) |
| S1-04 | [البطاقة](reference/source/ERP-02_S1-04_Task_Card_AR.md)، G3 العزل SEC-UAT-07/08/11 وUAT-DB-11 جزئياً | [HTTP](../tests/test_s1_04_access_http.py)، [السياسة](../tests/test_s1_04_access_policy.py)، [النماذج](../tests/test_s1_04_access_models.py) | [الإغلاق](reference/source/ERP-02_G3_S1-04_Closure_AR.md) |
| S1-05/06 | [المراجعة 3](reference/source/ERP-02_S1-05_S1-06_Development_Batch_AR.md)، UAT-DB-01 | [المعاملات](../tests/test_s1_05_transactions.py)، [التخزين](../tests/test_s1_05_storage.py)، [الإعادة](../tests/test_s1_06_idempotency.py)، [التزامن](../tests/test_s1_06_idempotency_concurrency.py) | PR #9 |
| S1-07 | [A01–A07](reference/source/S1-07_Inventory_Valuation_and_Locations_AR.md)، UAT-DB-10 | [القيود](../tests/test_s1_07_inventory_constraints.py)، [التزامن](../tests/test_s1_07_inventory_concurrency.py) | PR #11 |
| S1-08 | [A01–A05](reference/source/S1-08_S1-09_Inventory_Batch_AR.md)، على نمط UAT-DB-03 | [التراجع](../tests/test_s1_08_stock_rollback.py)، [التنظيف](../tests/test_s1_08_attachment_cleanup.py)، [المنح](../tests/test_s1_08_runtime_grants.py) | PR #13 |
| S1-09 | [A01–A05](reference/source/S1-08_S1-09_Inventory_Batch_AR.md)، UAT-DB-02 | [الصرف](../tests/test_s1_09_stock_issue.py)، [التزامن](../tests/test_s1_09_stock_issue_concurrency.py) | PR #13 |
| S1-10 | [معايير 1–3](reference/source/S1-10_S1-11_Printing_and_Internal_API_AR.md)، الشعار مؤجل | [الوصول](../tests/test_s1_10_print_access.py)، [دورة العامل](../tests/test_s1_10_printer_lifecycle.py) | PR #15 وcgroups |
| S1-11 | [معايير 1–3](reference/source/S1-10_S1-11_Printing_and_Internal_API_AR.md) | [API](../tests/test_s1_11_internal_api.py)، [التزامن](../tests/test_s1_11_api_concurrency.py) | PR #15 |

الربط أعلاه على مستوى البطاقة وملفات الاختبار؛ لا يدعي مطابقة كل حالة اختبار منفردة بكل معيار. عند إدراج متطلب في دفعة لاحقة يُثبت ربطه الصريح بقرار وتصميم ومعيار وحالة اختبار ودليل رأس التنفيذ. لا تُغلق SRS-187/200/201 بمجرد إثبات UAT-DB-02/01/03 في العينة. FIN وSEC والقبول الميداني تبقى مستقلة.

## أدلة الدمج وCI التاريخية
| الدفعة | قرار وإثبات التنفيذ | فحص main بعد الدمج |
| --- | --- | --- |
| S1-05 وS1-06 | [#9](https://github.com/nahda-construction/AlNahda-ERP/pull/9) | [PostgreSQL 18](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45698042533/job/139500514695) |
| S1-07 | [#11](https://github.com/nahda-construction/AlNahda-ERP/pull/11) | [PostgreSQL 18](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45734653263/job/139624836295) |
| S1-08 وS1-09 | [#13](https://github.com/nahda-construction/AlNahda-ERP/pull/13) | [PostgreSQL 18](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45739290887/job/139640061381) |
| S1-10 وS1-11 | [#15](https://github.com/nahda-construction/AlNahda-ERP/pull/15) | [PostgreSQL 18](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45943900808/job/140324108623) · [cgroups](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45943900808/job/140324108879) |

[الإصدارات](reference/versions.md). لا تُستنتج نتيجة فحص الرأس الحالي من أدلة رؤوس سابقة.

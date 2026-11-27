# نتائج القبول ومراجع الاختبار

| البطاقة | الدليل والنتيجة |
| --- | --- |
| S1-01 | يبقى دليل القبول التشغيلي وملف `accept-s1-01.sh` ضمن التسليم الخارجي المشار إليه في [README](../README.md#التشغيل-والقبول). |
| S1-04 | اختبارات العزل والصلاحيات المدمجة تشمل [طلبات HTTP](../tests/test_s1_04_access_http.py) و[سياسات الوصول](../tests/test_s1_04_access_policy.py) و[قيود النماذج](../tests/test_s1_04_access_models.py). يظل نطاق قبول البطاقة كما اعتُمد. |
| CI-01 | نجح فحص PostgreSQL 18 على [طلب السحب #1](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45550371753/job/138989861955). |
| CI-01 | نجح [تشغيل `main` بعد الدمج](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45551265159) عند `485410cd17d0a690f279a93844141da408c9e46a`. تؤكد بيانات الخطوات نجاح فحص الأسرار، والتحقق من إصدار PostgreSQL، وفحوص Django والترحيلات وتطبيقها، وتشغيل اختبارات Django. |
| DOC-01 | معايير القبول تشمل حصر الفرق في الوثائق المصرح بها وفحص المسافات والأسرار والتحقق من طلب السحب ونجاح GitHub Actions. تسجل نتائج التنفيذ في طلب السحب نفسه. |

يتضمن سير العمل `python manage.py check` و`python manage.py makemigrations --check --dry-run` في بيئة الاختبار على PostgreSQL 18. تغييرات DOC-01 وثائقية، وأثرها على الترحيلات معدوم. الرجوع عند الحاجة بعد الدمج يكون بعكس التزام الوثائق.

## أدلة الأتمتة والدفعات اللاحقة

| المرجع | الدليل والحدود |
| --- | --- |
| PR #6 | head `daaad71948b94967b648bc64726cc736441d4901`؛ CI [45625570893](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45625570893)، مراجعة Codex مكتملة بلا ملاحظات، التصحيح تحقق من الدالة الاصطناعية؛ مغلق دون دمج. تقرير Cloud: 30 اختباراً على PostgreSQL 18.7. |
| PR #7 | head `96677d47ec3ff40bce5b1f6e55d23ae9f227ba22`؛ CI [45627127910](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45627127910)، مراجعة مكتملة بلا ملاحظات، دمج `bf7c3f31`؛ [دليل القبول](https://github.com/nahda-construction/AlNahda-ERP/pull/7#issuecomment-7164383093). اختبار دمج إيجابي لا اختبار رفض. |
| CLEAN-01 | تعديل وثائق فقط؛ لا Migration. دليل قبول هذه الدفعة في PR الخاص بها بعد CI والمراجعة والدمج. |
| S1-05 وS1-06 | دُمجتا عبر [#9](https://github.com/nahda-construction/AlNahda-ERP/pull/9)، واجتاز main فحص PostgreSQL 18 بعد الدمج. [النتيجة](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45698042533/job/139500514695) |

هذا الملف أدلة التنفيذ لا بديل عن RTM واختبارات UAT في Handover.

# نتائج القبول ومراجع الاختبار

| البطاقة | الدليل والنتيجة |
| --- | --- |
| S1-01 | يبقى دليل القبول التشغيلي وملف `accept-s1-01.sh` ضمن التسليم الخارجي المشار إليه في [README](../README.md#التشغيل-والقبول). |
| S1-04 | اختبارات العزل والصلاحيات المدمجة تشمل [طلبات HTTP](../tests/test_s1_04_access_http.py) و[سياسات الوصول](../tests/test_s1_04_access_policy.py) و[قيود النماذج](../tests/test_s1_04_access_models.py). يظل نطاق قبول البطاقة كما اعتُمد. |
| CI-01 | نجح فحص PostgreSQL 18 على [طلب السحب #1](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45550371753/job/138989861955). |
| CI-01 | نجح [تشغيل `main` بعد الدمج](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45551265159) عند `485410cd17d0a690f279a93844141da408c9e46a`. تؤكد بيانات الخطوات نجاح فحص الأسرار، والتحقق من إصدار PostgreSQL، وفحوص Django والترحيلات وتطبيقها، وتشغيل اختبارات Django. |
| DOC-01 | معايير القبول تشمل حصر الفرق في الوثائق المصرح بها وفحص المسافات والأسرار والتحقق من طلب السحب ونجاح GitHub Actions. تسجل نتائج التنفيذ في طلب السحب نفسه. |

يتضمن سير العمل `python manage.py check` و`python manage.py makemigrations --check --dry-run` في بيئة الاختبار على PostgreSQL 18. تغييرات DOC-01 وثائقية، وأثرها على الترحيلات معدوم. الرجوع عند الحاجة بعد الدمج يكون بعكس التزام الوثائق.

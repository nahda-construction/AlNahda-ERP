# ERP شركة النهضة للبناء والتشييد

أُسست بيئتا التطوير والاختبار في S1-01 على Ubuntu/WSL2. تشغّل كل بيئة Django عبر Gunicorn وقاعدة PostgreSQL 18 خاصة بها. يعمل التطبيق محليًا في بيئة التطوير على `127.0.0.1:8001` وفي الاختبار على `127.0.0.1:8002`. تكشف `/healthz/` حالة مبسطة بعد استعلام PostgreSQL. راجع [المراجع المعتمدة](docs/reference/README.md).

## حالة المشروع

حتى 25 نوفمبر 2026، دُمجت S1-04 لصلاحيات الوصول إلى المشاريع وCI-01 لفحوص GitHub Actions على PostgreSQL 18. اكتملت تجربة الأتمتة Native-First والدمج المحمي في OPS-AUTO-04. البطاقة التالية هي S1-05 للمعاملة التجريبية والمرفق الخاص وسجل التدقيق.

- [حالة التنفيذ](docs/project_state.md)
- [سجل القرارات](docs/decisions_ledger.md)
- [الفجوات والمتابعات](docs/open_blockers.md)
- [نتائج القبول](docs/acceptance_tests.md)

وثائق GitHub أدلة التنفيذ والقرارات التشغيلية، ووثائق Handover العشرة في مشروع ChatGPT مرجع المتطلبات والتصميم. اقرأ AGENTS.md قبل العمل. لا تعني نقطة الاستئناف إذن تنفيذ البطاقة تلقائياً.

## التشغيل والقبول

يتم التشغيل من نافذة Ubuntu العادية باستخدام `docker --context rootless`. أسماء المشاريع `erp-dev` و`erp-test`، وتستخدم كل بيئة ملف إعداد منفصلًا خارج المستودع. يستخدم دليل تسليم S1-01 وملف `accept-s1-01.sh` الموجود معه المسارين `$HOME/.config/alnahda-erp/dev.env` و`$HOME/.config/alnahda-erp/test.env`. يمكن استخدام أمثلة `ops/env/` لإعداد بدائل محلية بالقيم نفسها وأسرار مستقلة. يحتفظ المشغل بالملفين خارج المزامنة ويستخدم أذونات قراءة مالك الملف فقط.

يوثّق ملف التسليم الخارجي `accept-s1-01.sh` قبول S1-01 عند نسخته. يضيف إجراء التشغيل التالي تهيئة دور قاعدة البيانات قبل بدء خدمة `web`.

### التهيئة أو الترقية

حدّث ملف البيئة الخارجي من المثال المناسب في `ops/env/`. تضبط متغيرات `ERP_DB_*` اتصال مالك قاعدة البيانات، وتضبط `ERP_WEB_DB_USER` و`ERP_WEB_DB_PASSWORD` اتصال تشغيل مستقل. اختر كلمة مرور عشوائية مختلفة لكل دور وبيئة واحفظها في ملف الإعداد الخارجي. تتصل خدمة `web` حصريًا باعتماد التشغيل.

تستخدم الأوامر التالية بيئة التطوير واسم تشغيل اصطناعيًا. لتجهيز بيئة الاختبار غيّر ملف البيئة إلى `test.env` واسم المشروع إلى `erp-test`، واستخدم `erp_test_web` في أوامر SQL وملف الإعداد والمنفذ `8002` لفحص الصحة. عند الترقية أوقف `web` أولًا واحتفظ بوحدة PostgreSQL الحالية واسم القاعدة واعتماد مالكها. استخدم ملف المنح من نسخة الكود المطابقة للترحيلات.

```bash
erp_compose() {
  docker --context rootless compose -p erp-dev --env-file "$HOME/.config/alnahda-erp/dev.env" "$@"
}
erp_compose stop web
erp_compose up -d --wait db
erp_compose exec db sh -ec '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1
'
```

في جلسة `psql` أنشئ دور التشغيل لأول مرة واضبط كلمة مروره بالقيمة الموجودة في ملف البيئة الخارجي. إذا كان الدور موجودًا، أعد استخدامه بعد التحقق من أنه دور دخول غير مالك وبلا عضوية في أدوار أخرى.

```sql
CREATE ROLE erp_dev_web LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
\password erp_dev_web
\quit
```

طبّق الترحيلات بخدمة الصيانة، ثم طبّق المنح بحساب مالك القاعدة. يُعاد تطبيق المنح بعد إعادة إنشاء الجداول أو ترقية المخطط.

```bash
erp_compose run --rm --build migrate
erp_compose exec -T db sh -ec '
  export PGPASSWORD="$POSTGRES_PASSWORD"
  exec psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v runtime_role="$1"
' _ erp_dev_web < ops/postgres/s1_05_runtime_grants.sql
erp_compose up -d --build --wait web
curl --fail http://127.0.0.1:8001/healthz/
```

افحص `SELECT current_user` من اتصال Django في خدمة `web` ومن اتصال الصيانة. يسجل الفحص اسمَي الدورين فقط. على بيئة القبول الاصطناعية، تحقق من قراءة وإدراج أحداث التدقيق ومن رفض تغييرها أو حذفها بهوية التشغيل.

```bash
erp_user_query='
from django.db import connection
with connection.cursor() as cursor:
    cursor.execute("SELECT current_user")
    print(cursor.fetchone()[0])
'
erp_compose exec -T web python manage.py shell -c "$erp_user_query"
erp_compose run --rm -T migrate python manage.py shell -c "$erp_user_query"
```

تستخدم كل بيئة وحدة مرفقات خاصة عند `/var/lib/erp/private` يملكها مستخدم تشغيل التطبيق. يمر تنزيل المرفق عبر مسار الموظفين المحمي. عند تشغيل Django خارج Compose، يحدد `ERP_PRIVATE_ROOT` مسارًا مطلقًا لمجلد خاص يملكه مستخدم التطبيق.

## إيقاف البيئة

استخدم مشروع Compose وملف الإعداد المحدّد للبيئة المراد إيقافها. مثال:

```bash
docker --context rootless compose --env-file "$HOME/.config/alnahda-erp/dev.env" -p erp-dev stop
```

يحافظ الإيقاف على بيانات PostgreSQL في وحدتها الخاصة.

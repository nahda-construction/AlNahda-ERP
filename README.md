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
erp_compose_project=erp-dev
erp_compose_env="$HOME/.config/alnahda-erp/dev.env"
erp_compose() {
  docker --context rootless compose -p "$erp_compose_project" --env-file "$erp_compose_env" "$@"
}
erp_compose stop web printer
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

ابنِ الصورة المشتركة، ثم طبّق الترحيلات بخدمة الصيانة والمنح بحساب مالك القاعدة. تطبع النسخة الحالية اسم الشركة في رأس المستند. عند إتاحة الشعار المعتمد، اضبط `erp_logo` على مسار ملف PNG خارج المستودع قبل البناء. تدخل بصمته في إعداد البناء لتحديث الصورة عند تغيّر الأصل. يُعاد تطبيق المنح بعد إعادة إنشاء الجداول أو ترقية المخطط.

```bash
erp_logo=
erp_health_host=localhost
erp_build_image() {
  if [ -z "$erp_logo" ]; then
    docker --context rootless build --tag "${erp_compose_project}-erp-app" .
    return
  fi
  local erp_logo_digest
  erp_logo_digest=$(sha256sum < "$erp_logo") &&
  docker --context rootless build \
    --secret "id=erp_print_logo,src=$erp_logo" \
    --build-arg "ERP_PRINT_LOGO_SHA256=${erp_logo_digest%% *}" \
    --tag "${erp_compose_project}-erp-app" .
}
erp_grants() {
  erp_compose exec -T db sh -ec '
    export PGPASSWORD="$POSTGRES_PASSWORD"
    exec psql -h 127.0.0.1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
      -v ON_ERROR_STOP=1 -v runtime_role="$1" -v revoke_inventory_runtime="$2"
  ' _ "$1" "${2:-off}"
}
erp_build_image &&
erp_compose run --rm --pull never migrate &&
erp_grants erp_dev_web < ops/postgres/s1_05_runtime_grants.sql &&
erp_grants erp_dev_web < ops/postgres/s1_08_inventory_runtime_grants.sql &&
erp_compose up -d --no-build --pull never --wait web printer &&
curl --fail -H "Host: $erp_health_host" http://127.0.0.1:8001/healthz/
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

### الطباعة الداخلية

تعمل خدمة `printer` من صورة الإصدار نفسها على شبكة Compose داخلية خاصة. يرسل `web` إليها القالب بعد التحقق من المشروع وصلاحية الطباعة وحالة المعاملة. يتضمن الإصدار Chromium والخط العربي وBootstrap RTL، ويقرأ العامل الشعار من `apps/printing/assets/logo.png` عند تضمينه في الصورة. يحجز المشرف قفلًا واحدًا لكل بيئة على ملف `render.lock` داخل وحدة `printlock`. احتفظ بالملف والوحدة أثناء الترقية، وأوقف مثيل الطباعة كاملاً قبل تشغيل بديله.

يضبط `ERP_WEB_WORKERS` عدد عمال الويب، ويضبط `ERP_PRINT_MEMORY_LIMIT` سقف ذاكرة حاوية الطباعة. القيمة الأولية للسقف `1g`. قِس ذروة الاستهلاك بعينة الـ80 سطرًا قبل تثبيت إعداد بيئة القبول. مهلة المشرف 30 ثانية ومهلة استدعاء الويب 35 ثانية. يعيد الطلب المتزامن `429` مع `Retry-After: 5`، ويستمر الويب وقاعدة البيانات في الخدمة أثناء تعافي عامل الطباعة.

### واجهة API الداخلية

اضبط `ERP_INTERNAL_HOSTS` و`ERP_OWNER_HOSTS` بأسماء مضيفين منفصلة في إعداد البيئة. يتيح مضيف الموظفين مسارات `/api/v1/` بجلسة Django، ويتطلب إنشاء المسودة CSRF ومفتاح `Idempotency-Key`. ترجع إعادة الإنشاء المطابقة استجابة `201` الأصلية. تُرفع مرفقات PDF عبر شاشة إنشاء المعاملة للموظف.

تتطلب واجهة `/api/v1/docs/` ومخطط `/api/v1/openapi.json` إذن `view_internal_api_docs`. يتيح الإعداد الافتراضي `owner.localhost` لاختبار حجب واجهة الموظفين على مضيف المالك المحاكى. تُراجع تهيئة الوسيط وLAN ضمن S1-12.

### تنظيف المرفقات اليتيمة

يُجري مشغّل الصيانة التنظيف من نافذة صيانة بعد تصريف عمليات الكتابة. أوقف `web` وأي عمليات كتابة أخرى على قاعدة البيئة، ثم تحقق من اسم وحدة ملفات مشروع Compose نفسه قبل تركيبها في جلسة الصيانة. يفترض المثال بيئة `erp-dev`. في بيئة الاختبار استخدم مشروع `erp-test` ووحدة `erp-test_privatefiles` وإعداداتهما.

```bash
erp_private_volume="${erp_compose_project}_privatefiles"
erp_volume_labels='{{index .Labels "com.docker.compose.project"}}/{{index .Labels "com.docker.compose.volume"}}'
erp_compose stop web &&
erp_actual_labels=$(docker --context rootless volume inspect --format "$erp_volume_labels" "$erp_private_volume") &&
test "$erp_actual_labels" = "$erp_compose_project/privatefiles" &&
erp_compose run --rm -T --no-deps \
  -v "$erp_private_volume:/var/lib/erp/private" \
  -e ERP_PRIVATE_ROOT=/var/lib/erp/private \
  migrate python manage.py shell -c '
from apps.test_transactions.services import cleanup_orphaned_attachments
result = cleanup_orphaned_attachments()
print(result)
if result.failures:
    raise SystemExit(1)
'
```

تتحقق الخدمة من هوية مالك جدول المرفقات، وتؤجل التشغيل عند تعذر أخذ قفل الصيانة. راجع مفاتيح الملفات التي فشل حذفها ورموز أخطائها، ثم أعد التنظيف بعد معالجة السبب. أعد تشغيل `web` بعد إكمال الصيانة.

### الرجوع عن ترحيلات المخزون وإلغاء منحها

نفّذ الرجوع المخبري في قاعدة اصطناعية. يتطلب الرجوع مع بيانات مخزون أو تدقيق مرتبطة خطة حفظ واستعادة معتمدة. أوقف الكتابة، ثم ألغِ منح S1-08 قبل إسقاط جداولها. تبدأ الترحيلات العكسية بمرجع التدقيق ثم المخزون.

```bash
erp_grants erp_dev_web on < ops/postgres/s1_08_inventory_runtime_grants.sql
erp_compose run --rm -T migrate python manage.py migrate test_transactions 0003 --noinput
erp_compose run --rm -T migrate python manage.py migrate inventory 0002 --noinput
```

تبقى منح ERP-02 سارية. لإعادة التطبيق نفّذ الترحيلات والمنح بالترتيب الوارد في إجراء التهيئة أو الترقية.

## إيقاف البيئة

استخدم مشروع Compose وملف الإعداد المحدّد للبيئة المراد إيقافها. مثال:

```bash
docker --context rootless compose --env-file "$HOME/.config/alnahda-erp/dev.env" -p erp-dev stop
```

يحافظ الإيقاف على بيانات PostgreSQL في وحدتها الخاصة.

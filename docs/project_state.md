# حالة مشروع ERP في 27 نوفمبر 2026

دُمجت دفعة S1-10 وS1-11 على `main` عند `a274aadbacd1a41a715d684ba6c3a3fca86d4603`. تخضع المتطلبات الوظيفية لمراجع Handover v1.6 وترتيب الحجية في `AGENTS.md`. [طلب السحب #15](https://github.com/nahda-construction/AlNahda-ERP/pull/15)

| البطاقة | الحالة والدليل |
| --- | --- |
| S1-01 | دُمج تأسيس بيئتي التطوير والاختبار في `ea1110a`. تبقى مراجع التسليم والاعتماد متاحة في [فهرس المراجع](reference/README.md). |
| S1-02 | مغلقة؛ سجل توافق البيئة، لا وحدة أعمال. [الدليل](reference/source/ERP-02_S1-02_Compatibility_Record_AR.md). |
| S1-03 | دُمجت المشاريع وأشجار الأعمال ومراجعات عقود المالك في `2235b5a`. |
| S1-04 | اكتمل التنفيذ `0e8d11b` ودُمج في `main` عبر `6beb495`. قواعد الصلاحيات واختبارات العزل المعتمدة مرجعها البطاقة وتنفيذها. |
| CI-01 | دُمج طلب السحب [#1](https://github.com/nahda-construction/AlNahda-ERP/pull/1). تشمل GitHub Actions فحص الأسرار وفحوص Django والترحيلات والاختبارات على PostgreSQL 18. راجع [نتائج القبول](acceptance_tests.md). |
| DEV-ENV-01 | تحقق DOC-01 من Ubuntu على WSL2 ومن إصداري Codex CLI `0.172.0` وعميل Docker `29.10.0`. تحقق أيضًا من حساب GitHub CLI `nahda-construction` ومن وصول GitHub API وGit remote بعد إتاحة الشبكة في `workspace-write`. |
| DOC-01 | أغلقت البطاقة بعد إثبات رفع الفرع وفتح طلب السحب وتشغيل فحوصه. الدليل النهائي هو طلب السحب وسجل GitHub Actions المرفق به. |
| OPS-AUTO-04 | اكتمل اختبار التنفيذ السحابي والمراجعة والدمج المحمي؛ PR #6 مغلق دون دمج، PR #7 مدمج في `bf7c3f31`. |
| CLEAN-01 | توحيد المرجعية وتحديث المتابعات؛ دليل الدمج في طلب السحب الخاص بها. |
| S1-05 | اكتملت المعاملة التجريبية والمرفق الخاص وسجل التدقيق وفصل حساب تشغيل PostgreSQL عن حساب الترحيلات في `e9e3125`. دُمجت عبر [PR #9](https://github.com/nahda-construction/AlNahda-ERP/pull/9). |
| S1-06 | اكتملت إعادة إرسال الطلب بالإيصال نفسه وحماية الإيصال المكتمل في `e5d3398`. دُمجت عبر [PR #9](https://github.com/nahda-construction/AlNahda-ERP/pull/9). |
| S1-07 | اكتملت مجموعات تقييم المخزون والمواقع وأرصدة المواقع في `b2baa99`. دُمجت عبر [PR #11](https://github.com/nahda-construction/AlNahda-ERP/pull/11). |
| S1-08 | دُمجت الحركة الافتتاحية الاصطناعية، وقيود السند والحركة والتدقيق، واختبارات التراجع والصيانة ومنح التشغيل في الالتزام `811f52f` عبر [#13](https://github.com/nahda-construction/AlNahda-ERP/pull/13). |
| S1-09 | دُمج اعتماد الصرف المتزامن بترتيب أقفال ثابت وإعادة التحقق من الصلاحية والمتاح في الالتزام `7b36098` عبر [#13](https://github.com/nahda-construction/AlNahda-ERP/pull/13). |
| S1-10 | دُمجت الطباعة العربية الداخلية وعامل Chromium المعزول، مع قفل مشترك وتعافٍ بعد الفشل، في الالتزام `e8e4119`. يظهر اسم الشركة في المستند وفق قرار تأجيل الشعار المعتمد. [#15](https://github.com/nahda-construction/AlNahda-ERP/pull/15) |
| S1-11 | دُمجت واجهة Django Ninja الداخلية ووثائق OpenAPI المحمية وحفظ إيصال الإنشاء وعزل مضيف المالك المحاكى في الالتزام `254c14f`. [#15](https://github.com/nahda-construction/AlNahda-ERP/pull/15) |

اجتازت ERP-02 الاختبارات الـ66 على PostgreSQL 18.7، مع إثبات التهيئة والترقية والرجوع وفصل الأدوار في بيئتي Compose معزولتين. اجتاز `main` فحص PostgreSQL 18 بعد الدمج. [أدلة قبول الدفعة](https://github.com/nahda-construction/AlNahda-ERP/pull/9#issuecomment-7173434970) · [GitHub Actions بعد الدمج](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45698042533/job/139500514695)

اجتازت S1-07 الاختبارات الـ82 على PostgreSQL 18.7. نجحت التهيئة والترقية والرجوع وإعادة التطبيق على بيانات اصطناعية، ثم نجح فحص `main` بعد الدمج. [أدلة قبول S1-07](https://github.com/nahda-construction/AlNahda-ERP/pull/11) · [GitHub Actions بعد الدمج](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45734653263/job/139624836295)

اجتازت دفعة S1-08 وS1-09 على `main` بعد الدمج 115 اختبارًا على PostgreSQL 18.7 وفحوص Django والترحيلات والأسرار. في اختبار UAT-DB-02 اعتُمد طلب صرف واحد من طلبين متزامنين لصرف 7 من رصيد 10، وبقي الرصيد 3 في كلا الترتيبين. [أدلة القبول](https://github.com/nahda-construction/AlNahda-ERP/pull/13) · [GitHub Actions بعد الدمج](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45739290887/job/139640061381)

اجتازت دفعة S1-10 وS1-11 على `main` بعد الدمج 135 اختبارًا على PostgreSQL 18.7 وفحوص Django والترحيلات والأسرار. في اختبار الطباعة سجّل Docker إنهاءً فعليًا بسبب نفاد الذاكرة عند حد 96 MiB. أعادت الطباعة `503` مع استمرار الويب وPostgreSQL، ثم نجحت عند استعادة حد 1 GiB. [فحص PostgreSQL](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45943900808/job/140324108623) · [فحص الطباعة وذاكرة الحاوية](https://github.com/nahda-construction/AlNahda-ERP/actions/runs/45943900808/job/140324108879)

تُقدّم دفعة REF-01 وDOC-02 وRTM-01 وVER-01 لضبط المرجعية والتتبع والإصدارات. يظل إعداد S1-12 لتجربة Staging الداخلية عبر LAN تابعًا لها ويُعرض نطاقه للاعتماد قبل التنفيذ.

يسجل DEV-ENV-01 نجاح Docker من Ubuntu ونجاح تجربة إنشاء فرع محلي وحذفه باستخدام `--approve-for-me` وفق تقرير البيئة. أثبت تقرير البيئة السحابية تشغيل Compose وPostgreSQL 18.7 و30 اختباراً؛ تشغيل Docker داخل Codex المحلي ليس شرطاً للمسار السحابي الحالي. راجع [المتابعات](open_blockers.md).

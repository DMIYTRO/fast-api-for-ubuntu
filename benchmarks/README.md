# Локальные бенчмарки старта и обработки

Замеры нужны для сравнения одной и той же машины и одной конфигурации между изменениями. Порогов в секундах нет: они зависят от CPU, диска, версии Python, ImageMagick/Ghostscript и БД.

## Старт сервиса

```bash
.venv/bin/python benchmarks/benchmark_startup.py --rounds 5 --scenario import --database warm
.venv/bin/python benchmarks/benchmark_startup.py --rounds 3 --scenario import --database cold
.venv/bin/python benchmarks/benchmark_startup.py --rounds 5 --scenario lifespan --database warm
.venv/bin/python benchmarks/benchmark_startup.py --rounds 3 --scenario create_app --database warm
```

`warm` один раз подготавливает временную БД и затем замеряет новые процессы на уже мигрированной схеме — это ближе к обычному рестарту. `cold` использует отдельную пустую БД на каждом повторе и показывает старт с первичными миграциями. Измеряются импорт `control_panel` вместе с созданием module-level приложения, длительность вызова Alembic, полный wall time нового Python-процесса; сценарии `lifespan` и `create_app` дополнительно измеряют эти этапы отдельно. БД и логи создаются во временной папке, Sborka и PitStop отключены.

## Обработка файлов

```bash
.venv/bin/python benchmarks/benchmark_processing.py --orders 25 --rounds 3
.venv/bin/python benchmarks/benchmark_processing.py --orders 5 --rounds 3 --full
.venv/bin/python benchmarks/benchmark_processing.py --orders 5 --rounds 3 --full --previews
```

Первый режим меряет обнаружение файлов, время до первой проверенной группы и полную инспекцию ImageMagick. `--full` добавляет PDF и его проверку Ghostscript, а `--previews` — создание превью. Также выводятся время от отправки run до первого результата координатора, до завершения run, число прошедших заказов, PDF/превью, ошибки, пропускная способность, медиана и p95. Генерация входных CMYK TIFF фикстур вынесена отдельно и не включена в обработку. Все файлы создаются во временном каталоге; сетевые интеграции не вызываются.

Оба скрипта печатают JSON в stdout; сохранить отчёт можно перенаправлением, например `... > /tmp/startup.json`. Скрипт обработки требует `magick`; полный режим дополнительно требует `gs`. Smoke/regression тесты инструментов запускаются так:

```bash
.venv/bin/python -m pytest tests/test_benchmarks.py -q
```

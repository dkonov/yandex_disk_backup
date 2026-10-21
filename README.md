# Yandex Disk Inventory — v0.1

Инвентаризация нескольких аккаунтов Яндекс Диска на Linux в Docker Compose. **Первая версия не скачивает содержимое файлов и не является бэкапом**. Только чтение метаданных, SQLite и отчёты по возможным дублям.

## Первый запуск

На сервере с Docker и Docker Compose:

```bash
git clone https://github.com/dkonov/yandex_disk_backup.git
cd yandex_disk_backup
mkdir -p config data
cp config/accounts.example.json config/accounts.json
docker compose build
docker compose --profile setup run --rm rclone-config config
```

В интерактивном меню rclone добавьте отдельный remote типа `yandex` для каждого аккаунта: `yandex_personal`, `yandex_family` и т. д. При сервере без браузера используйте предложенную rclone процедуру авторизации через другой компьютер с браузером.

Отредактируйте `config/accounts.json`:

```json
{"accounts":[
  {"id":"personal","remote":"yandex_personal:","enabled":true},
  {"id":"family","remote":"yandex_family:","enabled":true}
]}
```

Проверьте подключение:

```bash
docker compose --profile setup run --rm rclone-config lsd yandex_personal:
```

## Сбор каталога

```bash
docker compose run --rm inventory scan
docker compose run --rm inventory scan --account personal
docker compose run --rm inventory report
```

Результаты:

- `data/inventory.sqlite3` — SQLite, последняя **успешная** инвентаризация по каждому аккаунту.
- `data/reports/inventory.csv` — путь, размер, дата изменения, MIME-тип, MD5 (если доступен).
- `data/reports/duplicates.csv` — кандидаты в дубли по совпадению MD5 и размера, включая межаккаунтные.
- `data/reports/summary.json` — объёмы, число файлов, группы дублей, оценка потенциальной экономии.

Сканирование каждого аккаунта независимо; неполное сканирование не публикуется вместо предыдущего успешного. Для миллионов файлов используйте SQLite, а не открывайте огромный CSV в редакторе.

## Безопасность

- Используется только команда `rclone lsjson --recursive --hash`; чтение метаданных, **без изменения файлов в облаке**.
- Хеш может отсутствовать; такие файлы не участвуют в поиске точных дублей.
- MD5 не криптографически устойчив. **Нельзя автоматически удалять файлы по этому отчёту.**
- Оценка экономии — теоретическая, не обещание снижения квоты Yandex Disk.
- Каталог отражает перечисленные объекты, но не корзину или историю версий облака. Инвентаризация не является атомарным снимком.
- `config/rclone.conf` хранит OAuth-токены: `chmod 600 config/rclone.conf`, не публикуйте его.
- Одновременный запуск двух сканирований с одной SQLite базой не предусмотрен.

Следующая версия: история изменений между обходами, улучшенные отчёты. Реальное резервное копирование файлов будет отдельным режимом.

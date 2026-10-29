# Hegd

Терминал для файлов и Python-скриптов на Android (Kivy).

## Что где лежит

    main.py, kiwi_core.py, buildozer.spec, icon.png   само приложение (APK)
    tul.py                                            менеджер инструментов (кладётся на телефон один раз)
    tools.json                                        список инструментов: имя -> файлы, версия, SHA-256
    make_manifest.py                                  делает tools.json (запускать перед коммитом)
    hpiptool/hpip.py                                  инструмент "hpip"    (pip для Hegd)
    network/hegd_sv.py, network/sv.py                 инструмент "network" (общий доступ к папке в браузере)

`icon.png` (512x512) положите в корень сами: в архиве его нет.

## Первая настройка

1. Создайте репозиторий на GitHub (лучше публичный: `tul` ходит за файлами без пароля),
   загрузите в него всё из этой папки:

        git init && git add -A && git commit -m "Hegd"
        git branch -M main
        git remote add origin https://github.com/ВАШ_ЛОГИН/ВАШ_РЕПОЗИТОРИЙ.git
        git push -u origin main

2. На телефон в папку `hegd` положите `tul.py` и в Hegd выполните:

        +tr tul.py
        tul repo https://raw.githubusercontent.com/ВАШ_ЛОГИН/ВАШ_РЕПОЗИТОРИЙ/main
        tul list
        tul install hpip --force      (--force нужен, если hpip.py там уже лежит)
        tul install network

   Команда `tul` появится после сборки APK с этой версией ядра. До этого: `py tul.py install network`.

## Команды

    tul list                 что есть в репозитории и что установлено
    tul install NAME         установить (hpip, network)
    tul updates              есть ли новые версии (сервер опрашивается не чаще раза в 10 минут)
    tul update NAME          обновить один инструмент
    tul update --all         обновить все установленные
    tul rollback NAME        вернуть версию, которая была до последней установки/обновления

Hegd при запуске сам пишет строку «Tool updates are available...», если последняя проверка нашла обновления.

## Как выпустить обновление инструмента

1. Поправьте файл (например, `network/sv.py`).
2. Для `network` поднимите версию в `make_manifest.py`; для `hpip` она берётся из `VERSION` в `hpip.py`.
3. `python3 make_manifest.py`
4. `git add -A && git commit -m "network 1.1" && git push`
5. На телефоне: `tul updates`, затем `tul update network`. Не понравилось: `tul rollback network`.

## Безопасность

* Всё скачивается и проверяется (SHA-256, код должен компилироваться) ДО замены файлов.
  Заменяемые файлы копируются в `tools_backup/`; при любой ошибке всё возвращается обратно.
* `tul` не принимает файлы с именами `main.py`, `kiwi_core.py`, `tul.py` и служебных json,
  пути с `..`, не-https адреса.
* SHA-256 в `tools.json` лежит в том же репозитории, что и файлы: он защищает от повреждённой
  загрузки, а не от взлома самого репозитория. Включите двухфакторную защиту на GitHub.
* Обновлённый скрипт - новое содержимое: если вы его доверили (`+tr`), Hegd спросит снова.

## Сборка APK

Из корня репозитория (папки `hpiptool/`, `network/` и файлы `tul.py`, `make_manifest.py` в APK
не попадают, это прописано в `buildozer.spec`):

    buildozer android debug

Не удаляйте `~/.buildozer` и строку `p4a.branch` в `buildozer.spec`.

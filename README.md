# Hegd

Терминал для файлов и Python-скриптов на Android (Kivy).

## Что где лежит

    main.py, kiwi_core.py, buildozer.spec, icon.png   само приложение (APK)
    tul.py                                            менеджер инструментов (кладётся на телефон один раз)
    tools.json                                        список инструментов: имя -> файлы, версия, SHA-256
    make_manifest.py                                  делает tools.json (запускать перед коммитом)
    hpiptool/hpip.py                                  инструмент "hpip"    (pip для Hegd)
    network/hegd_sv.py, sv.py, net.py                 инструмент "network" (папка в браузере; ip, dns, port, ping, trace)
    gittool/gitkit.py                                 инструмент "gitkit"  (GitHub с телефона: clone, status, pull, push)

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
    tul install NAME         установить (hpip, network, gitkit)
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

## gitkit: GitHub с телефона

Программы `git` на Android нет, поэтому `gitkit` работает через API GitHub. Клон это снимок без истории.

    tul install gitkit
    +tr gitkit.py
    py gitkit.py token                          проверить токен
    py gitkit.py clone resolik/Hegd             скачать в папку repos/Hegd
    py gitkit.py status                         что вы изменили (без сети)
    py gitkit.py pull                           забрать чужие изменения
    py gitkit.py push -m "что сделано"          отправить изменения ОДНИМ коммитом (--dry: только показать)
    py gitkit.py log resolik/Hegd -n 5          последние коммиты
    py gitkit.py ls resolik/Hegd src            файлы репозитория

Токен: GitHub -> Settings -> Developer settings -> Fine-grained tokens -> Generate. Выберите ОДИН репозиторий,
Permissions -> Contents: Read and write. Положите токен одной строкой в `github_token.txt` рядом с `gitkit.py`.
Этот файл в репозиторий не попадёт: он в `.gitignore`, а `push` сам отказывается отправлять файлы, похожие на секреты.

## net.py: сетевые инструменты (инструмент "network")

    py net.py ip [public]                 адреса телефона (public: внешний адрес через api.ipify.org)
    py net.py dns NAME [TYPE]             без TYPE отвечает резолвер телефона; с TYPE (MX, TXT, NS...) через DNS-over-HTTPS
    py net.py port HOST 80,443,8000-8010  какие TCP-порты отвечают (чужой публичный адрес: до 20 портов, больше только с --yes)
    py net.py ping HOST [-c 4] [--tcp]    системный ping; если он не работает у приложения, время TCP-соединения (и об этом сказано)
    py net.py trace HOST [-m 20] [--raw]  маршрут по прыжкам через ping с ограниченным TTL; --raw показывает сырой вывод для отладки

Без root сырые пакеты из Python недоступны, поэтому `ping` и `trace` зависят от системной программы `ping` на вашей прошивке.

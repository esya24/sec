# Telegram-секретарь

Python 3.11 или новее. Установите зависимости командой `python -m pip install -r requirements.txt`. Запуск — `python main.py`.

При обновлении распакуйте сборку в чистую папку приложения. Сохраните переменные окружения и прежний каталог данных `DATA_DIR`, чтобы бот подхватил словарь и ручные паузы.

## Настройки бота

- `TELEGRAM_BOT_TOKEN` или `BOT_TOKEN` — токен бота.
- `BUSINESS_AUTO_REPLY=1` — автоответы в подключённых личных чатах. В Telegram у подключения должно быть право отвечать на сообщения.
- `OPENAI_API_KEY` или `MODEL_API_KEY` — ключ модели. Поддерживаются ранее использовавшиеся `OPENAI_MODEL`, `MODEL_NAME`, `MODEL_BASE_URL` и `LOCAL_MODEL_URL`.
- `TELEGRAM_OWNER_ID` — числовой Telegram ID владельца. Если переменная отсутствует, используется ID из `main.py`.
- `DATA_DIR` — каталог для `manual-chats.json` и `vocabulary.sqlite3`. На Bothost по умолчанию используется `/app/data`.

Ручное исходящее сообщение владельца ставит секретаря на паузу в этом чате на 30 минут. Срок отсчитывается от последнего такого сообщения и сохраняется при перезапуске. Когда срок истекает, секретарь снова отвечает в чате. Для пауз старого формата без времени отсчёт начинается с первого запуска обновлённого бота. В панели можно снять активные ручные паузы раньше. Кнопка «Стоп» самого Telegram управляет отдельной настройкой подключения.

Каждый запрос к модели начинается с текущего сообщения. В сборке больше нет архива и профиля стиля. Новый код игнорирует прежние `STYLE_PATH` и `CONTEXT_ARCHIVE_KEY`. При обновлении поверх старых файлов удалите оставшиеся на хосте `chat-history.enc` и `style.json`, если они там есть. Сообщения в Telegram хранятся на стороне Telegram.

## Панель разработчика

В личном чате владельца с ботом отправьте `/panel`, `/start` или «меню». На главном экране видно состояние автоответов, модели и число чатов на паузе.

- **Чаты и паузы.** Список известных чатов и снятие всех ручных пауз с подтверждением.
- **Словарь.** Добавление и удаление слов или коротких выражений. Словарь сохраняется между перезапусками и начинает пустым. В 20% запросов на черновик автоответа модель получает одно случайное слово из словаря и может употребить его, если оно подходит по смыслу.
- **Статус системы.** Имя модели, интерфейс API и состояние подключения MCP.

Автоответы в подключённых чатах отправляются отдельными сообщениями без цитирования. Секретарь пишет коротко, по-дружески и говорит о себе в мужском роде. Шутки, разговорные слова и мат зависят от тона сообщения. Исходящие сообщения, уведомления и кнопки оформляются без эмодзи.

Секретарь начинает обработку сразу после получения входящей реплики и отправляет готовый ответ без паузы. Короткие приветствия и благодарности вроде «Привет!» и «Спасибо!» получают ответ без обращения к модели. Сообщения с дополнительным текстом обрабатываются как обычно. Данные о подключении хранятся в памяти и обновляются при изменении прав в Telegram. Для обычного автоответа достаточно одного запроса к модели. Сомнительные реплики, в том числе возможные обещания от лица владельца, проходят дополнительную проверку. При ошибке модели бот может отправить уведомление сразу, без ожидания повторного запроса. В журнале выводится время запроса к модели и время обработки отправленного автоответа.

В Shupp значение `MODEL_NAME=shupp` служит общим идентификатором. Конкретную модель для ключа бота задаёт раздел «Модели» кабинета Shupp. Сравнивайте время по строкам «Модель ответила» для одинаковых сообщений, требующих генерации.

Запрос модели из личного чата владельца может выполняться одновременно с автоответами в подключённых чатах. В личном чате владельца команды, ответы и действия панели выполняются по очереди.

## Подключение Codex по MCP

1. Создайте отдельный секрет командой `python -c "from secrets import token_urlsafe; print(token_urlsafe(32))"` и задайте его в окружении бота как `SECRETARY_MCP_TOKEN`. Без переменной MCP отключён.
2. Для локального запуска адрес по умолчанию — `http://127.0.0.1:8765/mcp`. На Bothost включите HTTPS-домен и задайте `MCP_PUBLIC_URL=https://ваш-домен` без пути. Бот слушает порт из `PORT`, который выставляет Bothost. [Документация Bothost](https://bothost.ru/docs/domains-and-ports)
3. Задайте тот же `SECRETARY_MCP_TOKEN` в окружении, из которого запущен Codex. Зарегистрируйте сервер, подставив фактический адрес, и перезапустите Codex. [Документация Codex](https://developers.openai.com/codex/mcp)

   ```powershell
   codex mcp add secretary --url https://ваш-домен/mcp --bearer-token-env-var SECRETARY_MCP_TOKEN
   ```

`secretary_status` показывает статус модели и автоответов, а также число активных ручных пауз. `secretary_resume_all` снимает их. Доступ к методам защищён отдельным токеном. Инструменты не читают переписку.

## Лицензия данных Unicode

```text
UNICODE LICENSE V3

COPYRIGHT AND PERMISSION NOTICE

Copyright © 1991-2026 Unicode, Inc.

NOTICE TO USER: Carefully read the following legal agreement. BY
DOWNLOADING, INSTALLING, COPYING OR OTHERWISE USING DATA FILES, AND/OR
SOFTWARE, YOU UNEQUIVOCALLY ACCEPT, AND AGREE TO BE BOUND BY, ALL OF THE
TERMS AND CONDITIONS OF THIS AGREEMENT. IF YOU DO NOT AGREE, DO NOT
DOWNLOAD, INSTALL, COPY, DISTRIBUTE OR USE THE DATA FILES OR SOFTWARE.

Permission is hereby granted, free of charge, to any person obtaining a
copy of data files and any associated documentation (the "Data Files") or
software and any associated documentation (the "Software") to deal in the
Data Files or Software without restriction, including without limitation
the rights to use, copy, modify, merge, publish, distribute, and/or sell
copies of the Data Files or Software, and to permit persons to whom the
Data Files or Software are furnished to do so, provided that either (a)
this copyright and permission notice appear with all copies of the Data
Files or Software, or (b) this copyright and permission notice appear in
associated Documentation.

THE DATA FILES AND SOFTWARE ARE PROVIDED "AS IS", WITHOUT WARRANTY OF ANY
KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT OF
THIRD PARTY RIGHTS.

IN NO EVENT SHALL THE COPYRIGHT HOLDER OR HOLDERS INCLUDED IN THIS NOTICE
BE LIABLE FOR ANY CLAIM, OR ANY SPECIAL INDIRECT OR CONSEQUENTIAL DAMAGES,
OR ANY DAMAGES WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS,
WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION,
ARISING OUT OF OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THE DATA
FILES OR SOFTWARE.

Except as contained in this notice, the name of a copyright holder shall
not be used in advertising or otherwise to promote the sale, use or other
dealings in these Data Files or Software without prior written
authorization of the copyright holder.
```

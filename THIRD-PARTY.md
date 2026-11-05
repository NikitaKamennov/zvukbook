# Сторонние компоненты Zvukbook

Лицензия [MIT](LICENSE) относится к собственному коду Zvukbook. У модели,
библиотек и инструментов остаются собственные условия.

## Silero

Используется **Silero v5_6_ru** от Silero Team:

- [Исходный проект](https://github.com/snakers4/silero-models).
- [Официальные веса](https://models.silero.ai/models/tts/ru/v5_6_ru.pt).
- [Сохранённая лицензия используемой версии](licenses/silero-v5_6_ru/LICENSE):
  **CC BY-NC-SA 4.0**. Она не заменяется MIT-лицензией приложения.
- [Manifest с SHA-256](config/silero-v5_6_ru.json).

Веса не включены в Git. Подготовка скачивает их из официального источника,
проверяет SHA и размещает рядом сохранённую лицензию. Коммерческое использование
модели не разрешается одной лишь MIT-лицензией кода Zvukbook; учитывай условия
самой модели.

## Python, Go и комплектные инструменты

Версии Python-пакетов закреплены в `requirements.lock` (Linux),
`requirements/windows-app.lock` и `requirements/windows-probe/*.lock` (Windows).
Go-зависимости — `desktop/go.mod` и `desktop/go.sum`.

Windows-сборщик создаёт собственный `THIRD-PARTY.md` внутри готового комплекта
и сохраняет уведомления компонентов:

| Компонент | Где смотреть условия в собранном приложении |
| --- | --- |
| CPython | `runtime/*/LICENSE.txt` |
| PyTorch и Python-пакеты | `runtime/*/Lib/site-packages/`, лицензии в `*.dist-info` |
| Silero | `models/silero-v5_6_ru/LICENSE` и `manifest.json` |
| FFmpeg | `tools/ffmpeg/LICENSE` и README; текущая essentials-сборка — GPL |
| Tesseract и русский tessdata | `tools/tesseract/doc/` и уведомления пакета |
| DjVuLibre | `tools/djvulibre/COPYING.txt` и `doc/` |
| WebView2 Fixed Runtime | уведомления в `tools/webview2/` |
| Microsoft Visual C++ DLL | `licenses/msvc/LICENSE.rtf` |

Пакеты и бинарные зависимости не публикуются вместе с исходниками.
При самостоятельной передаче собранного приложения сохраняй комплект
уведомлений и соблюдай условия конкретных зависимостей.

# Стандарт и правила интернационализации (i18n) VoicePlayer

Данное руководство является **обязательным стандартом** при добавлении любых новых фичей, экранов, диалогов, кнопок и настроек в VoicePlayer.

---

## 1. Главные принципы

1. **Нулевой хардкод строк в UI (Zero Hardcoded Strings)**:
   - В коде интерфейса (`ttk.Label`, `ttk.Button`, `messagebox`, заголовках окон `title(...)`, контекстных меню, всплывающих подсказках) запрещено использовать сырые строки на русском, английском или любом другом языке.
   - Все строки должны получаться исключительно через `vp_i18n.t("key_name", **kwargs)`.

2. **Симметрия всех 8 поддерживаемых языков**:
   VoicePlayer официально поддерживает 8 языков:
   - `en`: English (базовый международный)
   - `ru`: Русский
   - `es`: Español
   - `zh`: 中文 (Упрощённый китайский)
   - `hi`: हिन्दी (Хинди)
   - `ja`: 日本語 (Японский)
   - `de`: Deutsch (Немецкий)
   - `fr`: Français (Французский)

   > [!IMPORTANT]
   > При добавлении любого нового ключа он **обязан** быть добавлен во все 8 секций `STRINGS` в `vp_i18n.py`. Тест `tests/test_i18n_deep.py` упадёт с ошибкой, если хотя бы в одном языке ключ отсутствует или оставлен пустым.

---

## 2. Стандарт именования ключей

Используйте префиксы, отражающие назначение элемента:

| Префикс | Назначение | Пример ключа |
|---|---|---|
| `tab_` | Вкладки (Notebook, разделы) | `tab_playback`, `tab_shortcuts` |
| `btn_` | Кнопки | `btn_speak`, `btn_paste` |
| `opt_` | Опции, флажки и радиокнопки | `opt_autoplay`, `opt_autoclip` |
| `msg_` | Сообщения диалогов (`messagebox`) | `msg_nothing_to_export`, `msg_save_failed` |
| `lbl_` | Текстовые метки и описания | `lbl_tempo`, `lbl_custom_text` |
| `tip_` | Всплывающие подсказки (тултипы) | `tip_play_pause`, `tip_dictate` |
| `cat_` | Категории фильтров | `cat_agents`, `cat_local_llm` |
| `guide_` | Инструкции и справочные тексты | `guide_btn`, `guide_title` |

---

## 3. Правила форматирования и плейсхолдеров

- **Никогда не используйте конкатенацию строк** вида:
  ```python
  # ❌ ЗАПРЕЩЕНО:
  text = "Найдено реплик: " + str(count) + " из " + str(total)
  ```
  В разных языках (особенно японском, немецком и хинди) порядок слов и грамматика кардинально отличаются от русского/английского.
- **Всегда используйте именованные плейсхолдеры**:
  ```python
  # ✅ ПРАВИЛЬНО:
  text = vp_i18n.t("msg_found_count", count=count, total=total)
  ```
  В шаблоне строки:
  - `ru`: `"Найдено реплик: {count} из {total}"`
  - `en`: `"Found {count} of {total} messages"`
  - `ja`: `"{total} 件中 {count} 件のメッセージが見つかりました"`

---

## 4. Диалоговые окна и оповещения (`messagebox`)

Все вызовы `messagebox.showinfo`, `messagebox.showerror`, `messagebox.showwarning`, `messagebox.askyesno`:
1. Должны передавать ключ или шаблон:
   ```python
   # ✅ ПРАВИЛЬНО:
   messagebox.showinfo(APP_NAME, vp_i18n.t("msg_select_or_paste_for_mp3"))
   ```
2. Для динамических ошибок:
   ```python
   # ✅ ПРАВИЛЬНО:
   messagebox.showerror(APP_NAME, vp_i18n.t("msg_save_failed", err=e))
   ```
3. В системе работает встроенный перехватчик `_install_i18n_messagebox()` и `_MESSAGE_FALLBACK_MAP`. Если в стороннем модуле случайно будет вызван русский текст, перехватчик попытается найти соответствие и перевести его, но разработчик обязан использовать явный `vp_i18n.t()`.

---

## 5. Всплывающие окна (`tk.Toplevel`)

Каждое новое окно должно поддерживать тему и локализацию:
1. Заголовок: `win.title(vp_i18n.t("your_window_title"))`.
2. Окрашивание заголовка Windows (DWM Dark Mode):
   ```python
   self._center_over_root(win, width, height)
   self._apply_window_titlebar_theme(win)
   win.after(30, lambda w=win: self._apply_window_titlebar_theme(w))
   win.bind("<Map>", lambda e, w=win: self._apply_window_titlebar_theme(w), add="+")
   ```

---

## 6. Проверка перед коммитом (CI Verification)

Перед отправкой любых изменений в репозиторий запустите:
```powershell
python tests/test_i18n_deep.py
```
Тест проверяет:
- [x] Регистрацию всех 8 языков
- [x] Симметрию словарей (отсутствие забытых ключей)
- [x] Отсутствие пустых строк
- [x] Работу плейсхолдеров
- [x] Покрытие диалоговых окон

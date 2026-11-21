import multiprocessing as mp
import os
import subprocess
import sys
import tkinter as tk
from collections import deque
from pathlib import Path
from queue import Empty
from tempfile import TemporaryDirectory
from tkinter import filedialog, messagebox, ttk

from tkinterdnd2 import COPY, DND_FILES, TkinterDnD

from transcriber import FORMATS, MODES, MODEL_DIR, Options, save_text, worker


class App(TkinterDnD.Tk):
    def __init__(self):
        super().__init__()
        self.title("Транскриптор")
        self.geometry("740x480")
        self.minsize(630, 360)
        self.configure(background="#f2f5fa")
        self.mode = tk.StringVar(value=next(iter(MODES)))
        self.timestamps = tk.BooleanVar()
        self.speakers = tk.BooleanVar()
        self.status = tk.StringVar(value="Ожидаю файлы")
        self.pending = deque()
        self.finished = {}
        self.active = None
        self.process = None
        self.jobs = None
        self.events = None
        self.download_scratch = None
        self.build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(100, self.poll)

    def build(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f2f5fa")
        style.configure("TLabel", background="#f2f5fa", foreground="#19243a")
        style.configure("TCheckbutton", background="#f2f5fa")
        style.map("TCheckbutton", background=[("active", "#f2f5fa")])
        style.configure("Accent.TButton", background="#2455cb", foreground="#ffffff", padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#1e44a3")])
        style.configure("Horizontal.TProgressbar", background="#2455cb", troughcolor="#e0e7f4")
        style.configure("Treeview", rowheight=28)

        content = ttk.Frame(self, padding=20)
        content.pack(fill="both", expand=True)
        drop = tk.Label(
            content, text="Перетащи аудио или видео сюда", height=4,
            font=("TkDefaultFont", 14, "bold"), bg="#e2ebff", fg="#18356c",
            highlightthickness=1, highlightbackground="#b5c8f6",
        )
        drop.pack(fill="x")

        controls = ttk.Frame(content)
        controls.pack(fill="x", pady=12)
        ttk.Button(controls, text="Добавить файлы", command=self.pick, style="Accent.TButton").pack(side="left")
        ttk.Combobox(
            controls, values=tuple(MODES), textvariable=self.mode, state="readonly", width=9,
        ).pack(side="left", padx=(14, 10))
        ttk.Checkbutton(controls, text="Таймкоды", variable=self.timestamps).pack(side="left", padx=4)
        ttk.Checkbutton(controls, text="Спикеры", variable=self.speakers).pack(side="left", padx=4)

        table = ttk.Frame(content)
        table.pack(fill="both", expand=True)
        self.rows = ttk.Treeview(table, columns=("file", "state"), show="headings", selectmode="browse")
        self.rows.heading("file", text="Файл", anchor="w")
        self.rows.heading("state", text="Состояние", anchor="w")
        self.rows.column("file", width=320, minwidth=185)
        self.rows.column("state", width=270, minwidth=180)
        scrollbar = ttk.Scrollbar(table, command=self.rows.yview)
        self.rows.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.rows.pack(side="left", fill="both", expand=True)
        self.rows.bind("<Double-1>", self.open_row)
        self.rows.bind("<<TreeviewSelect>>", self.update_open_button)
        for target in (drop, self.rows):
            target.drop_target_register(DND_FILES)
            target.dnd_bind("<<Drop>>", self.drop_files)

        footer = ttk.Frame(content)
        footer.pack(fill="x", pady=(12, 0))
        self.open_button = ttk.Button(footer, text="Открыть TXT", command=self.open_row, state="disabled")
        self.open_button.pack(side="right", padx=(12, 0))
        self.cancel_button = ttk.Button(
            footer, text="Отменить файл", command=self.cancel_current, state="disabled",
        )
        self.cancel_button.pack(side="right", padx=(12, 0))
        ttk.Label(footer, textvariable=self.status).pack(anchor="w")
        self.progress = ttk.Progressbar(footer)
        self.progress.pack(fill="x", pady=(6, 0))

    def pick(self):
        patterns = tuple(f"*{suffix}" for suffix in FORMATS)
        self.add_files(filedialog.askopenfilenames(
            parent=self, title="Выбери записи", filetypes=[("Аудио и видео", patterns), ("Все файлы", "*")],
        ))

    def drop_files(self, event):
        self.add_files(self.tk.splitlist(event.data))
        return COPY

    def add_files(self, filenames):
        options = Options(self.mode.get(), self.timestamps.get(), self.speakers.get())
        for filename in filenames:
            path = Path(filename).absolute()
            row = self.rows.insert("", "end", values=(path.name, "В очереди"))
            if path.suffix.lower() not in FORMATS or not path.is_file():
                self.finished[row] = "Выбери файл MP3, M4A, WAV, OGG, OPUS, FLAC или MP4."
                self.rows.set(row, "state", "Формат не поддерживается или файл недоступен")
            else:
                self.pending.append((row, path, options))
        self.start_next()
        self.update_open_button()

    def start_next(self):
        if self.active is not None or not self.pending:
            return
        self.active, source, options = self.pending.popleft()
        self.cancel_button.configure(state="normal")
        self.set_progress("Запускаю обработку", None)
        self.status.set(source.name)
        self.rows.see(self.active)
        try:
            if self.process is None:
                ctx = mp.get_context("spawn")
                self.jobs = ctx.Queue()
                self.events = ctx.Queue()
                self.process = ctx.Process(target=worker, args=(self.jobs, self.events), daemon=True)
                self.process.start()
            self.jobs.put((source, options))
        except (OSError, RuntimeError) as exc:
            self.stop_worker()
            self.finish("error", f"Не удалось запустить обработку: {exc}")

    def poll(self):
        if self.events is not None:
            # опрашиваю очередь без блокировки окна
            try:
                while True:
                    kind, result = self.events.get_nowait()
                    if kind == "progress":
                        stage, percent = result
                        self.set_progress(stage, percent)
                    elif kind == "download":
                        self.confirm_download(*result)
                    elif kind == "text":
                        try:
                            output = save_text(*result)
                        except OSError as exc:
                            self.finish("error", str(exc))
                        else:
                            self.finish("done", output)
                    else:
                        self.finish(kind, result)
            except Empty:
                pass
            if not self.process.is_alive():
                self.stop_worker()
                if self.active is not None:
                    self.finish("error", "Процесс обработки завершился со сбоем.")
        self.start_next()
        self.after(100, self.poll)

    def confirm_download(self, models, path):
        self.set_progress("Ожидаю подтверждения загрузки", None)
        items = "\n".join(f"{name} ≈ {size} МБ" for name, size in models)
        answer = messagebox.askokcancel(
            "Загрузка моделей", f"{items}\n\nПапка установки\n{path}\n\nСкачать и продолжить?",
            parent=self,
        )
        scratch = None
        if answer:
            try:
                if self.download_scratch is None:
                    MODEL_DIR.mkdir(parents=True, exist_ok=True)
                    self.download_scratch = TemporaryDirectory(dir=MODEL_DIR, prefix=".download-")
                scratch = self.download_scratch.name
            except OSError as exc:
                messagebox.showerror("Не удалось подготовить загрузку", str(exc), parent=self)
        self.jobs.put(scratch)

    def cancel_current(self):
        if self.active is not None:
            self.stop_worker()
            self.finish("cancelled", None)
            self.start_next()

    def set_progress(self, stage, percent):
        self.rows.set(self.active, "state", stage)
        if percent is None:
            if str(self.progress["mode"]) != "indeterminate":
                self.progress.configure(mode="indeterminate")
                self.progress.start(15)
        else:
            self.progress.stop()
            self.progress.configure(mode="determinate", value=percent)

    def finish(self, kind, result):
        row = self.active
        self.active = None
        self.cancel_button.configure(state="disabled")
        self.finished[row] = result
        if kind == "done":
            self.rows.set(row, "state", f"Готово  ·  {result.name}")
            self.status.set("TXT сохранён рядом с исходником")
        elif kind == "cancelled":
            self.rows.set(row, "state", "Отменено")
            self.status.set("Обработка файла отменена")
        else:
            self.rows.set(row, "state", "Ошибка  ·  открыть подробности")
            self.status.set("Не удалось обработать файл")
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0)
        self.update_open_button()

    def update_open_button(self, _event=None):
        selected = self.rows.selection()
        result = self.finished.get(selected[0]) if selected else None
        if isinstance(result, str):
            self.open_button.configure(text="Подробности", state="normal")
        else:
            self.open_button.configure(text="Открыть TXT", state="normal" if result else "disabled")

    def open_row(self, event=None):
        selected = self.rows.selection()
        row = self.rows.identify_row(event.y) if event else next(iter(selected), None)
        result = self.finished.get(row)
        if isinstance(result, str):
            messagebox.showerror("Ошибка обработки", result, parent=self)
        elif result:
            try:
                if os.name == "nt":
                    os.startfile(result)
                else:
                    command = "open" if sys.platform == "darwin" else "xdg-open"
                    subprocess.Popen([command, str(result)])
            except OSError as exc:
                messagebox.showerror("Не удалось открыть TXT", str(exc), parent=self)

    def stop_worker(self):
        if self.process is not None and self.process.pid is not None:
            if self.process.is_alive():
                self.process.terminate()
            self.process.join()
            self.process.close()
        self.process = None
        for queue in (self.jobs, self.events):
            if queue is not None:
                queue.close()
        self.jobs = self.events = None
        if self.download_scratch is not None:
            self.download_scratch.cleanup()
            self.download_scratch = None

    def close(self):
        if (self.active is not None or self.pending) and not messagebox.askyesno(
            "Закрыть транскриптор", "Прервать обработку и закрыть окно?", parent=self,
        ):
            return
        self.stop_worker()
        self.destroy()


if __name__ == "__main__":
    mp.freeze_support()
    App().mainloop()

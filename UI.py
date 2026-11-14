# -*- coding: utf-8 -*-
"""UI.py —— 翻转外语 自动化 小界面（tkinter，随包自带，不用装任何东西）

- 主界面：词数、开始/停止、进度条、已答/答错统计、当前题目、彩色日志
- 日志工具：清空、复制、导出；设置会记住上次词数
- 停止时用 taskkill /T 清掉整个子进程树（包括常驻 adb shell）
"""

import json
import os
import queue
import re
import subprocess
import threading
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

HERE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(HERE, "deps", "python", "python.exe")
_SCRIPT = os.path.join(HERE, "卡包.py")

CREATE_NO_WINDOW = 0x08000000
CONFIG_FILE = os.path.join(HERE, "界面配置.json")
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

BG = "#eef3f7"
CARD = "#ffffff"
PRIMARY = "#1fb6a6"
PRIMARY_D = "#0f9488"
TEXT = "#1f2937"
MUTED = "#64748b"
BORDER = "#e2e8f0"
DANGER = "#e5484d"
LOG_BG = "#0f172a"
LOG_FG = "#e2e8f0"


def _child_env():
    env = dict(os.environ)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    env["PATH"] = os.path.join(HERE, "deps", "adb") + os.pathsep + env.get("PATH", "")
    return env


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.lines = queue.Queue()
        self.words = tk.StringVar(value="25")
        self.status = tk.StringVar(value="空闲")
        self.prog_text = tk.StringVar(value="学习 0/25")
        self.count_text = tk.StringVar(value="已答 0")
        self.mistake_text = tk.StringVar(value="答错 0")
        self.current = tk.StringVar(value="等待开始")
        self.auto_scroll = tk.BooleanVar(value=True)
        self.answered = 0
        self.mistakes = 0
        self.total = 25
        self._stopping = False

        root.title("翻转外语 · 自动化")
        root.configure(bg=BG)
        root.geometry("1200x850")
        root.minsize(900, 620)
        self._setup_style()
        self._load_config()
        self._build_header()

        self.body = tk.Frame(root, bg=BG)
        self.body.pack(fill="both", expand=True)
        self.main_frame = tk.Frame(self.body, bg=BG)
        self._build_main(self.main_frame)
        self.main_frame.pack(fill="both", expand=True)

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(80, self._drain)
        self._center()

    # ------------------------------------------------------------------
    # 样式 / 窗口
    # ------------------------------------------------------------------
    def _setup_style(self):
        st = ttk.Style()
        try:
            st.theme_use("clam")
        except tk.TclError:
            pass
        st.configure(".", background=BG, foreground=TEXT,
                     font=("Microsoft YaHei UI", 11))
        st.configure("Card.TFrame", background=CARD)
        st.configure("Card.TLabel", background=CARD, foreground=TEXT)
        st.configure("Muted.TLabel", background=CARD, foreground=MUTED)
        st.configure("Stat.TLabel", background=CARD, foreground=PRIMARY_D,
                     font=("Consolas", 12, "bold"))
        st.configure("TButton", padding=(12, 7), borderwidth=0,
                     font=("Microsoft YaHei UI", 11))
        st.configure("Primary.TButton", background=PRIMARY, foreground="white",
                     font=("Microsoft YaHei UI", 11, "bold"))
        st.map("Primary.TButton",
               background=[("active", PRIMARY_D), ("disabled", "#a9dcd6")],
               foreground=[("disabled", "#f2fffd")])
        st.configure("Danger.TButton", background=DANGER, foreground="white",
                     font=("Microsoft YaHei UI", 11, "bold"))
        st.map("Danger.TButton",
               background=[("active", "#c93a3f"), ("disabled", "#f0b1b3")],
               foreground=[("disabled", "#fff5f5")])
        st.configure("Ghost.TButton", background=CARD, foreground=MUTED)
        st.map("Ghost.TButton", background=[("active", "#f1f5f9")])
        st.configure("TSpinbox", fieldbackground="white", padding=6)
        st.configure("TEntry", fieldbackground="white", padding=7)
        st.configure("TProgressbar", troughcolor="#e2e8f0", background=PRIMARY,
                     bordercolor="#e2e8f0", lightcolor=PRIMARY,
                     darkcolor=PRIMARY, thickness=12)
        st.configure("TCheckbutton", background=CARD, foreground=MUTED)

    def _center(self):
        self.root.update_idletasks()
        w, h = 1200, 850
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        w, h = min(w, max(900, sw - 60)), min(h, max(620, sh - 100))
        x, y = max(0, (sw - w) // 2), max(0, (sh - h) // 3)
        self.root.geometry("%dx%d+%d+%d" % (w, h, x, y))


    def _build_header(self):
        head = tk.Frame(self.root, bg=PRIMARY)
        head.pack(fill="x")
        left = tk.Frame(head, bg=PRIMARY)
        left.pack(side="left", padx=22, pady=14)
        tk.Label(left, text="翻转外语 · 自动化", bg=PRIMARY, fg="white",
                 font=("Microsoft YaHei UI", 20, "bold")).pack(anchor="w")
        tk.Label(left, text="RapidOCR + 离线词表 · 全离线运行", bg=PRIMARY,
                 fg="#d7fffa", font=("Microsoft YaHei UI", 10)).pack(anchor="w")
        right = tk.Frame(head, bg=PRIMARY)
        right.pack(side="right", padx=22)
        tk.Label(right, text="v1.1", bg=PRIMARY, fg="#d7fffa",
                 font=("Microsoft YaHei UI", 10)).pack()

    # ------------------------------------------------------------------
    # 主界面
    # ------------------------------------------------------------------
    def _build_main(self, frame):
        wrap = tk.Frame(frame, bg=BG)
        wrap.pack(fill="both", expand=True, padx=18, pady=14)

        # 控制卡片
        ctrl = tk.Frame(wrap, bg=CARD, highlightbackground=BORDER,
                        highlightthickness=1)
        ctrl.pack(fill="x")
        top = tk.Frame(ctrl, bg=CARD)
        top.pack(fill="x", padx=18, pady=(14, 8))
        tk.Label(top, text="词数", bg=CARD, fg=MUTED).pack(side="left")
        ttk.Spinbox(top, from_=1, to=999, width=7,
                    textvariable=self.words).pack(side="left", padx=(8, 8))
        tk.Label(top, text="25 词 ≈ 一整包（约 3 分钟）", bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 10)).pack(side="left")
        self.stop_btn = ttk.Button(top, text="停止", style="Danger.TButton",
                                   state="disabled", command=self.stop)
        self.stop_btn.pack(side="right")
        self.start_btn = ttk.Button(top, text="开始", style="Primary.TButton",
                                    command=self.start)
        self.start_btn.pack(side="right", padx=8)

        mid = tk.Frame(ctrl, bg=CARD)
        mid.pack(fill="x", padx=18, pady=(0, 6))
        self.progress = ttk.Progressbar(mid, maximum=25, value=0)
        self.progress.pack(side="left", fill="x", expand=True)
        tk.Label(mid, textvariable=self.prog_text, bg=CARD, fg=TEXT,
                 font=("Consolas", 10, "bold")).pack(side="left", padx=(10, 18))
        tk.Label(mid, textvariable=self.count_text, bg=CARD, fg=MUTED,
                 font=("Consolas", 10)).pack(side="left", padx=6)
        tk.Label(mid, textvariable=self.mistake_text, bg=CARD, fg="#b45309",
                 font=("Consolas", 10)).pack(side="left", padx=6)
        self.status_lbl = tk.Label(mid, textvariable=self.status, bg=CARD,
                                   fg=MUTED, font=("Microsoft YaHei UI", 10))
        self.status_lbl.pack(side="right")

        cur = tk.Frame(ctrl, bg=CARD)
        cur.pack(fill="x", padx=18, pady=(2, 14))
        tk.Label(cur, textvariable=self.current, bg=CARD, fg=MUTED,
                 font=("Microsoft YaHei UI", 10)).pack(anchor="w")

        # 日志卡片
        logcard = tk.Frame(wrap, bg=CARD, highlightbackground=BORDER,
                           highlightthickness=1)
        logcard.pack(fill="both", expand=True, pady=(12, 0))
        bar = tk.Frame(logcard, bg=CARD)
        bar.pack(fill="x", padx=16, pady=(10, 6))
        tk.Label(bar, text="运行日志", bg=CARD, fg=TEXT,
                 font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        ttk.Button(bar, text="导出", style="Ghost.TButton",
                   command=self.export_log).pack(side="right")
        ttk.Button(bar, text="复制", style="Ghost.TButton",
                   command=self.copy_log).pack(side="right", padx=4)
        ttk.Button(bar, text="清空", style="Ghost.TButton",
                   command=self.clear_log).pack(side="right")
        ttk.Checkbutton(bar, text="自动滚动", variable=self.auto_scroll,
                        style="TCheckbutton").pack(side="right", padx=10)

        self.log = scrolledtext.ScrolledText(
            logcard, state="disabled", bg=LOG_BG, fg=LOG_FG,
            insertbackground="white", relief="flat", font=("Consolas", 10),

            padx=12, pady=8, wrap="word")
        self.log.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        for tag, color in (("info", LOG_FG), ("accent", "#5eead4"),
                           ("success", "#4ade80"), ("warn", "#fbbf24"),
                           ("error", "#f87171"), ("muted", "#64748b")):
            self.log.tag_config(tag, foreground=color)

        # 底部
        foot = tk.Frame(wrap, bg=BG)
        foot.pack(fill="x", pady=(8, 0))
        ttk.Button(foot, text="打开程序目录", style="Ghost.TButton",
                   command=lambda: os.startfile(HERE)).pack(side="left")
        ttk.Button(foot, text="打开依赖目录", style="Ghost.TButton",
                   command=lambda: os.startfile(os.path.join(HERE, "deps"))
                   ).pack(side="left", padx=6)
        tk.Label(foot, text="v1.1 · 本地运行 · 不联网", bg=BG, fg=MUTED,
                 font=("Microsoft YaHei UI", 9)).pack(side="right")

    # ------------------------------------------------------------------
    # 运行
    # ------------------------------------------------------------------
    def _load_config(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            words = int(data.get("words", int(data.get("steps", 50)) // 2))
            if 1 <= words <= 999:
                self.words.set(str(words))
        except Exception:
            pass

    def _save_config(self):
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump({"words": int(self.words.get() or 25)}, f,
                          ensure_ascii=False, indent=2)
        except Exception:
            pass

    def start(self):
        if self.proc is not None:
            return
        if _SCRIPT is None or not os.path.isfile(_SCRIPT):
            self.status.set("找不到 卡包.py")
            return
        try:
            words = max(1, min(999, int(self.words.get())))
        except ValueError:
            self.status.set("词数要填数字")
            return
        steps = words * 2
        self._save_config()
        self.answered = self.mistakes = 0
        self.total = 25
        self._reset_view()
        self._append("开始运行（%d 词）…" % words, "accent")
        self.status.set("运行中")
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.progress.config(maximum=25, value=0)
        self.prog_text.set("学习 0/25")
        self.current.set("启动中…")
        self.proc = subprocess.Popen(
            [PY, _SCRIPT, str(steps)], cwd=HERE, env=_child_env(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=CREATE_NO_WINDOW)
        threading.Thread(target=self._reader, daemon=True).start()

    def stop(self):
        proc = self.proc
        if proc is None:
            return
        self._stopping = True
        self.status.set("正在停止…")
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, creationflags=CREATE_NO_WINDOW,
                           timeout=8)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    def on_close(self):
        self._save_config()
        self.stop()
        self.root.destroy()

    def _reader(self):
        proc = self.proc
        try:
            for raw in proc.stdout:
                text = ANSI_RE.sub("", raw.decode("utf-8", "replace")).rstrip()
                if text:
                    self.lines.put(text)
        finally:
            code = proc.wait()
            self.root.after(0, lambda: self._finished(code))

    def _finished(self, code):
        self.proc = None
        self.start_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        if self._stopping:
            self._stopping = False
            self.status.set("已停止")
            self._append("已手动停止", "muted")
            return
        if code == 0:
            self.status.set("已完成")
            self._append("运行结束（正常退出）", "success")
        else:
            self.status.set("已结束（退出码 %d）" % code)
            self._append("运行结束（退出码 %d，看看日志最后几行）" % code,
                         "error")

    # ------------------------------------------------------------------
    # 日志 / 统计
    # ------------------------------------------------------------------
    def _append(self, text, tag="info"):
        self.log.config(state="normal")
        self.log.insert("end", text + "\n", tag)
        self.log.config(state="disabled")
        if self.auto_scroll.get():
            self.log.see("end")

    def _reset_view(self):
        self.clear_log()
        self.count_text.set("已答 0")
        self.mistake_text.set("答错 0")
        self.prog_text.set("学习 0/25")
        self.progress.config(value=0)

    def clear_log(self):
        self.log.config(state="normal")
        self.log.delete("1.0", "end")
        self.log.config(state="disabled")

    def copy_log(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.log.get("1.0", "end-1c"))

    def export_log(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt", initialfile="运行日志.txt",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(self.log.get("1.0", "end-1c"))
            self.status.set("日志已导出")
        except OSError as exc:
            self.status.set("导出失败：%s" % exc)

    def _set_progress(self, done, total):
        if total > 0:
            self.total = total
            self.progress.config(maximum=total, value=min(done, total))
            self.prog_text.set("学习 %d/%d" % (done, total))

    def _update_stats(self):
        self.count_text.set("已答 %d" % self.answered)
        self.mistake_text.set("答错 %d" % self.mistakes)

    def _parse(self, text):
        tag = "info"
        m = re.search(r"\[(\d+)/(\d+)\]", text)
        if m:
            self._set_progress(int(m.group(1)), int(m.group(2)))

        m = re.search(r"题目:\s*(.+?)\s*->\s*选项([A-D])「(.+?)」\s*"
                      r"([\d.]+)（(.+?)）", text)
        if m:
            word, letter, opt, score, source = m.groups()
            self.answered += 1
            self._update_stats()
            try:
                ok = float(score) >= 0.20 and "没查到" not in source
            except ValueError:
                ok = False
            tag = "success" if ok else "warn"
            self.current.set("当前：%s · 选项%s「%s」 · %s · %s"
                             % (word[:24], letter, opt[:20], score, source))
        elif "单词卡:" in text:
            word = text.split("单词卡:", 1)[1].strip()
            self.current.set("当前：单词卡 %s" % word[:30])

        if "答错了" in text:
            self.mistakes += 1
            self._update_stats()
            tag = "warn"
        if "共作答" in text:
            m = re.search(r"共作答\s*(\d+)\s*题", text)
            if m:
                self.answered = int(m.group(1))
                self._update_stats()
        if "一包结束" in text or "已完成" in text:
            tag = "success"
            self.progress.config(value=self.total)
            self.prog_text.set("已完成 %d/%d" % (self.total, self.total))
        if any(k in text for k in ("没把握", "出错", "认不出", "不可用", "失败",
                                   "Error", "Traceback")):
            tag = "error"
        elif any(k in text for k in ("新手引导", "页面被滑出", "弹窗",
                                     "结算页", "完成页", "强化轮")):
            tag = "accent"
        return tag

    def _drain(self):
        try:
            while True:
                text = self.lines.get_nowait()
                tag = self._parse(text)
                self._append(text, tag)
        except queue.Empty:
            pass
        self.root.after(80, self._drain)


def main():
    # DPI 感知必须放在 Tk() 之前，否则窗口会被系统二次缩放、右侧控件被裁掉
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    root = tk.Tk()
    try:
        # 1.25 让 125% 缩放屏上的字号正常；窗口按 1200x850 设计，_center 会按屏幕再收一次
        root.tk.call("tk", "scaling", 1.25)
    except Exception:
        pass

    try:
        ico = os.path.join(HERE, "app.ico")
        if os.path.isfile(ico):
            root.iconbitmap(ico)
        else:
            icon = tk.PhotoImage(width=32, height=32)
            icon.put(PRIMARY, to=(0, 0, 32, 32))
            root.iconphoto(True, icon)
            root._icon = icon
    except Exception:
        pass

    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()


